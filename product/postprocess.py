"""产品侧确定性后处理：标题对证据、时间排布、预算冲突与每日收尾。"""

import re
from typing import Any

from product.destinations import known_destination
from product.geo import order_nearest, parse_coord, transit_minutes
from product.names import clean_place_name, title_matches
from product.pace import DEFAULT_DAY_HOURS, PACE_WORDS, count_bounds, detect_pace, hours_from_text


def _as_list(value: Any, limit: int) -> list[str]:
    return [str(x).strip()[:80] for x in value[:limit] if str(x).strip()] if isinstance(value, list) else []


_TITLE_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)


def _norm_title(text: str) -> str:
    """去掉空白、标点和大小写差异，便于和证据里的地点名比对。"""
    return _TITLE_NOISE.sub("", str(text)).casefold()


def match_activity(activity: dict[str, Any], valid_names: dict[str, set]) -> tuple | None:
    """把模型给出的活动标题对回证据里的地点名；对不上返回 None。

    只放宽空白/标点/大小写和包含关系，不会接受证据里没有的地点。
    """
    evidence_id = activity.get("evidence_id")
    if evidence_id not in valid_names:
        return None
    title = _norm_title(activity.get("title") or "")
    if not title:
        return None
    for name in valid_names[evidence_id]:
        if _norm_title(name) == title:
            return evidence_id, name
    for name in valid_names[evidence_id]:
        normalized = _norm_title(name)
        if len(normalized) >= 4 and (normalized in title or title in normalized):
            return evidence_id, name
    return None


DEFAULT_STARTS = ("09:30", "13:30", "16:30")
_DURATION_HOURS = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*小时")
_DURATION_MINUTES = re.compile(r"([0-9]+)\s*分钟")


def _duration_minutes(value: Any) -> int:
    """把“2.5 小时”“90 分钟”转成分钟数，识别不了按 90 分钟算。"""
    text = str(value or "")
    match = _DURATION_HOURS.search(text)
    if match:
        return max(15, int(float(match.group(1)) * 60))
    match = _DURATION_MINUTES.search(text)
    if match:
        return max(15, int(match.group(1)))
    return 90


def _minutes_of(start: str) -> int:
    hour, _, minute = str(start or "").partition(":")
    try:
        return int(hour) * 60 + int(minute)
    except ValueError:
        return 0


def _hhmm(minutes: int) -> str:
    minutes = max(0, min(23 * 60 + 59, minutes))
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def _schedule_day(activities: list[dict[str, Any]], hours: float,
                  keep_order: bool = False, preserve_times: bool = False) -> list[dict[str, Any]]:
    """插入交通时间并裁掉超时活动；keep_order=True 时保留模型给的顺序。"""
    if not activities:
        return []
    if preserve_times:
        return sorted((dict(activity) for activity in activities),
                      key=lambda item: _minutes_of(item.get("start")))
    if not any(parse_coord(activity) for activity in activities):
        return _fit_schedule(activities)
    ordered = (sorted(activities, key=lambda item: _minutes_of(item.get("start")))
               if keep_order else order_nearest(activities))
    start_minutes = _minutes_of(ordered[0].get("start") or DEFAULT_STARTS[0])
    started_at = start_minutes
    cursor = start_minutes
    previous = None
    result = []
    for activity in ordered:
        coord = parse_coord(activity)
        if previous and coord:
            minutes = transit_minutes(previous, coord)
            cursor += minutes
            if str(activity.get("transport") or "") in ("", "交通未核实"):
                activity = {**activity, "transport": "打车约 %d 分钟（估算）" % minutes}
        start = max(cursor, _minutes_of(activity.get("start")))
        cursor = start + _duration_minutes(activity.get("duration"))
        result.append({**activity, "start": _hhmm(start)})
        previous = coord or previous
    while len(result) > 1 and (cursor - started_at) > hours * 60:
        result.pop()
        cursor = _minutes_of(result[-1]["start"]) + _duration_minutes(result[-1].get("duration"))
    return result


def _fit_schedule(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按开始时间排序，并把与前一项重叠的开始时间顺延到前一项结束。"""
    ordered = sorted(activities, key=lambda item: _minutes_of(item.get("start")))
    result = []
    previous_end = -1
    for activity in ordered:
        start = _minutes_of(activity.get("start"))
        if start < previous_end:
            start = previous_end
        previous_end = start + _duration_minutes(activity.get("duration"))
        result.append({**activity, "start": _hhmm(start)})
    return result


_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _clip(value: Any, limit: int, fallback: str) -> str:
    text = str(value or "").strip()
    return text[:limit] if text else fallback


def _clean_activities(activities: list[dict[str, Any]], limit: int = 4,
                      evidence: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """统一活动字段：时间缺失就按顺序给默认值，标题必须落在证据地点名上。"""
    result = []
    for position, activity in enumerate(activities[:limit]):
        start = str(activity.get("start") or "").strip()
        if not _TIME_PATTERN.match(start):
            start = DEFAULT_STARTS[min(position, len(DEFAULT_STARTS) - 1)]
        source = (evidence or {}).get(activity.get("evidence_id")) or {}
        names = [str(item.get("name") or "") for item in source.get("items", []) if item.get("name")]
        display = str(activity.get("display_title") or "").strip()[:24]
        if display and names and not title_matches(display, names):
            display = ""
        title = display or (clean_place_name(names[0]) if names else clean_place_name(activity.get("title")) )
        result.append({
            "title": title,
            "start": start,
            "duration": _clip(activity.get("duration"), 20, "1.5 小时"),
            "description": _clip(activity.get("description"), 400, "请通过来源页面核对细节。"),
            "transport": _clip(activity.get("transport"), 60, "交通未核实"),
            "cost": _clip(activity.get("cost"), 40, "未核实"),
            "evidence_id": activity.get("evidence_id"),
        })
    return result


DURATION_PATTERN = re.compile(r"(\d+|[一二两三四五六七八九十])\s*(天|日|晚)|周末|半天|一日游")
BUDGET_PATTERN = re.compile(r"(预算|人均)[^\d一二三四五六七八九十]{0,6}(\d+(?:\.\d+)?|[一二三四五六七八九十百千]+)\s*(万|千|w|k)?")


def _declares_duration(text: str) -> bool:
    return bool(DURATION_PATTERN.search(str(text or "")))


_CN_DIGITS = "零一二三四五六七八九"


def chinese_number(text: str) -> int:
    """把“五百”“一千二”这类写法转成整数，失败返回 0。"""
    total, section, number = 0, 0, 0
    for char in text or "":
        if char in _CN_DIGITS:
            number = _CN_DIGITS.index(char)
        elif char == "十":
            section += (number or 1) * 10
            number = 0
        elif char == "百":
            section += (number or 1) * 100
            number = 0
        elif char == "千":
            section += (number or 1) * 1000
            number = 0
        else:
            return 0
    return total + section + number


DAY_MARKER = re.compile(r"第([一二三四五六七八九十\d]+)天[^。；\n]{0,8}?(半天|大半天|([0-9一二三四五六七八九十]+)\s*(?:个)?\s*小时)")
CN_DAY = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def day_hours(windows: list[dict[str, Any]] | None, day: Any) -> float:
    """取某天的可用时长；缺省按整天。"""
    for window in windows or []:
        if int(window.get("day") or 0) == int(day or 0):
            return float(window.get("hours") or DEFAULT_DAY_HOURS)
    return DEFAULT_DAY_HOURS


def _cn_number(raw: str) -> int | None:
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    return CN_DAY.get(raw)


def detect_day_windows(*texts: Any, days: int = 1) -> list[dict[str, Any]]:
    """每天可用时长：用户点名某天半天/N 小时就覆盖那天，否则用全局值。"""
    joined = " ".join(str(text) for text in texts if text)
    default = hours_from_text(joined) or DEFAULT_DAY_HOURS
    windows = [{"day": day, "hours": default} for day in range(1, max(1, days) + 1)]
    for match in DAY_MARKER.finditer(joined):
        day = _cn_number(match.group(1))
        if not day or day > len(windows):
            continue
        hours = hours_from_text(match.group(2)) or DEFAULT_DAY_HOURS
        windows[day - 1]["hours"] = hours
    return windows


_MEAL_TYPES = ("breakfast", "lunch", "dinner", "snack")


def clean_meals(meals: Any, evidence: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """校验餐饮槽位：名字必须落在证据里，时间合法，最多三条。"""
    result = []
    for meal in meals[:3] if isinstance(meals, list) else []:
        if not isinstance(meal, dict):
            continue
        evidence_id = str(meal.get("evidence_id") or "").strip()
        source = (evidence or {}).get(evidence_id) or {}
        names = [str(item.get("name") or "") for item in source.get("items", []) if item.get("name")]
        if not evidence_id or not names:
            continue
        title = str(meal.get("title") or "").strip()[:40]
        if title and not title_matches(title, names):
            title = ""
        title = title or clean_place_name(names[0])
        if not title:
            continue
        start = str(meal.get("start") or "").strip()
        if not _TIME_PATTERN.match(start):
            start = "12:30"
        kind = str(meal.get("type") or "meal").strip().lower()
        duration = str(meal.get("duration") or "").strip()
        if not duration or not re.search(r"\d", duration):
            duration = "1 小时"
        entry = {"type": kind if kind in _MEAL_TYPES else "meal",
                 "title": title, "start": start, "duration": duration,
                 "evidence_id": evidence_id}
        coordinates = next((item.get("coordinates") for item in source.get("items", [])
                            if title_matches(title, [str(item.get("name") or "")])), None)
        if coordinates:
            entry["coordinates"] = coordinates
        result.append(entry)
    return result


def clean_lodging(area: Any) -> str:
    """住宿区域只保留一个短名；没有就返回空字符串。"""
    text = str(area or "").strip()
    if not text or text in ("未核实", "无", "None"):
        return ""
    return text[:40]


def clean_named_places(values: Any, text: str, limit: int = 6) -> list[str]:
    """只保留用户原话里逐字出现的地点名，模型编的删掉。"""
    if not isinstance(values, list):
        return []
    source = str(text or "")
    result: list[str] = []
    for value in values[:limit]:
        name = str(value).strip()[:40]
        if name and name in source and name not in result:
            result.append(name)
    return result


HARD_CONFLICT_WORDS = ("预算", "超支", "五星", "豪华", "高档", "别墅", "住不起", "不够")
TIME_CONFLICT_WORDS = ("没说明玩几天", "没说几天", "玩几天", "天数不够")


def _blocking_conflict(text: str) -> bool:
    """只有“预算和住宿档位对不上”“没说玩几天”这类才值得打断用户。"""
    value = str(text or "")
    return any(word in value for word in HARD_CONFLICT_WORDS + TIME_CONFLICT_WORDS)


def budget_conflict(query: str, days: int) -> bool:
    """预算与“五星/豪华”明显不匹配时算冲突。"""
    match = BUDGET_PATTERN.search(query or "")
    if not match or not any(word in query for word in ("五星", "豪华", "高档", "别墅")):
        return False
    raw = match.group(2)
    amount = chinese_number(raw) if raw[0] in _CN_DIGITS else float(raw)
    unit = match.group(3) or ""
    if unit in ("万", "w"):
        amount *= 10000
    elif unit in ("千", "k"):
        amount *= 1000
    return amount / max(1, days) < 600


LATIN_CITY = re.compile(r"\b(?:in|to|at)\s+([A-Z][A-Za-z]{2,})")
LATIN_DAYS = re.compile(r"\b(\d+)\s*day", re.IGNORECASE)
LATIN_BUDGET = re.compile(r"\bbudget\s*[:：]?\s*(\d{3,6})", re.IGNORECASE)


def latin_hints(text: str) -> dict[str, Any]:
    """英文请求的兜底：从 “3 days in Hangzhou, budget 1000” 里补出关键字段。"""
    value = str(text or "")
    hints: dict[str, Any] = {}
    match = LATIN_CITY.search(value)
    if match:
        hints["destinations"] = [match.group(1)]
    match = LATIN_DAYS.search(value)
    if match:
        hints["days"] = int(match.group(1))
    match = LATIN_BUDGET.search(value)
    if match:
        hints["budget"] = match.group(1)
    return hints


BUDGET_UPDATE = re.compile(
    r"预算[^。；;\n]{0,10}?([0-9]{3,6}|[零一二三四五六七八九十百千两]{2,8})\s*(?:元|块|以内|左右)?")
ADD_DAYS = re.compile(r"(?:再|多|加)\s*([一二三四五六七八九两]|\d)\s*天")
SET_DAYS = re.compile(r"(?:改成|换成|改为|变成|调整为)\s*([一二三四五六七八九十两]|\d)\s*天")


def _amount(raw: str) -> float:
    """把“2000”“两千”这类写法转成数字，失败返回 0。"""
    value = str(raw or "").strip()
    if not value:
        return 0.0
    if value[0] in "零一二三四五六七八九":
        return float(chinese_number(value))
    if value[0] == "两":
        return float(chinese_number("二" + value[1:]))
    try:
        return float(value)
    except ValueError:
        return 0.0


CITY_CHANGE = re.compile(
    r"(?:换到|改到|改成去|换成去|换去|改去|换成|改成|改为|"
    r"目的地(?:改成|换成)|城市(?:改成|换成))\s*([\u4e00-\u9fff]{2,6})")


def update_constraints(constraints: dict[str, Any], text: str) -> dict[str, Any]:
    """识别修改句里的整体条件变化（预算/天数/节奏/目的地），命中就应升级为整单重排。"""
    changes: dict[str, Any] = {}
    value = str(text or "")
    match = BUDGET_UPDATE.search(value)
    if match:
        amount = _amount(match.group(1))
        current = constraints.get("budget")
        if amount >= 100 and amount != current:
            changes["budget"] = int(amount)
    current_days = constraints.get("days")
    match = ADD_DAYS.search(value)
    if match:
        extra = int(_amount(match.group(1)))
        if extra >= 1:
            changes["days"] = (int(current_days) if current_days else 1) + extra
    else:
        match = SET_DAYS.search(value)
        if match:
            total = int(_amount(match.group(1)))
            if total >= 1 and total != current_days:
                changes["days"] = total
    if any(word in value for words in PACE_WORDS.values() for word in words):
        pace = detect_pace(value)
        if pace != constraints.get("pace"):
            changes["pace"] = pace
    match = CITY_CHANGE.search(value)
    if match:
        city = re.sub(r"(市|地区|自治州|盟|县|区)$", "", match.group(1))
        if known_destination(city) and city != (constraints.get("destinations") or [None])[0]:
            changes["destinations"] = [city]
    return changes


def cities_fit_days(destinations: list[str], days: int) -> bool:
    """一天最多跨一个城市，城市数超过天数就认为约束矛盾。"""
    return len(destinations) <= max(1, int(days))


def _finalize_days(cleaned_days: list[dict[str, Any]], good: list[dict[str, Any]],
                   plan_days: list[dict[str, Any]],
                   keep_days: list[dict[str, Any]] | None = None,
                   windows: list[dict[str, Any]] | None = None,
                   pace: str = "normal",
                   requested: int | None = None,
                   keep_order: bool = False,
                   preserve_times: bool = False,
                   backfill: bool = True) -> list[dict[str, Any]]:
    """按天去重、补足到 2 个活动并按时间排序，避免出现空天或倒序。

    keep_days 是本次不动、原样保留的日期；只对 plan_days 里的日期做整理。
    """
    kept = [dict(day) for day in (keep_days or [])]
    by_day = {day.get("day"): day for day in cleaned_days if day.get("day")}
    used = {str(activity.get("title") or "").strip()
            for day in kept for activity in day.get("activities", [])}
    result = []
    for planned in plan_days:
        day = by_day.get(planned.get("day")) or {"day": planned.get("day"), "city": planned.get("city")}
        activities = []
        for activity in day.get("activities", []):
            title = str(activity.get("title") or "").strip()
            if not title or title in used:
                continue
            used.add(title)
            activities.append({**activity, "title": title})
        hours = day_hours(windows, planned.get("day"))
        low, high = count_bounds(hours, pace, requested)
        floor = max(1, min(requested or 2, high))
        if backfill and len(activities) < floor:
            for evidence in good:
                if not evidence["id"].startswith("d%d-" % planned["day"]):
                    continue
                for item in evidence.get("items", []):
                    name = clean_place_name(item.get("name"))
                    if not name or name in used:
                        continue
                    used.add(name)
                    activities.append({
                        "title": name,
                        "start": DEFAULT_STARTS[min(len(activities), len(DEFAULT_STARTS) - 1)],
                        "duration": "1.5 小时",
                        "description": "作为当天的备选安排，建议停留约 1.5 小时；开放时间与交通以来源页面为准。",
                        "transport": "交通未核实",
                        "cost": "未核实",
                        "evidence_id": evidence["id"],
                    })
                    if len(activities) >= floor:
                        break
                if len(activities) >= floor:
                    break
        scheduled = (activities if preserve_times else
                     _schedule_day(activities, hours, keep_order=keep_order))
        entry = {"day": planned["day"], "city": day.get("city") or planned.get("city"),
                 "activities": scheduled[:high]}
        if day.get("meals"):
            entry["meals"] = day["meals"]
        if day.get("lodging_area"):
            entry["lodging_area"] = day["lodging_area"]
        result.append(entry)
    result.extend(kept)
    result.sort(key=lambda item: int(item.get("day") or 0))
    return result


ROOM_TYPES = ("大床房", "双床房", "标间", "标准间", "家庭房", "单人房", "多人间")
TRANSPORT_WORDS = {
    "高铁": ("高铁", "动车", "g字头"),
    "飞机": ("飞机", "航班", "机票", "飞过去", "坐飞机"),
    "火车": ("火车", "卧铺", "普快"),
    "自驾": ("自驾", "开车", "租车"),
}


def clean_people(value: Any) -> int:
    """同行人数：只接受 1–30 的整数，模型给字符串也能转。"""
    match = re.search(r"\d+", str(value))
    if not match:
        return 0
    count = int(match.group())
    return count if 1 <= count <= 30 else 0


def clean_rooms(value: Any) -> int:
    """房间数：1–10 的整数；没提就是 0。"""
    match = re.search(r"\d+", str(value))
    if not match:
        return 0
    count = int(match.group())
    return count if 1 <= count <= 10 else 0


def clean_room_type(text: Any) -> str:
    source = str(text or "").strip()
    canonical = {"标准间": "标间", "双床间": "双床房", "大床": "大床房", "家庭间": "家庭房"}
    for name in ROOM_TYPES:
        if name and name in source:
            return canonical.get(name, name)
    return ""


def clean_transport(text: Any) -> str:
    source = str(text or "").strip().lower()
    for name, words in TRANSPORT_WORDS.items():
        if any(word in source for word in words):
            return name
    return ""
