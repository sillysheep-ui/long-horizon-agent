"""产品侧确定性后处理：标题对证据、时间排布、预算冲突与每日收尾。"""

import re
from typing import Any

from product.names import clean_place_name, title_matches


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


def cities_fit_days(destinations: list[str], days: int) -> bool:
    """一天最多跨一个城市，城市数超过天数就认为约束矛盾。"""
    return len(destinations) <= max(1, int(days))


def _finalize_days(cleaned_days: list[dict[str, Any]], good: list[dict[str, Any]],
                   plan_days: list[dict[str, Any]],
                   keep_days: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
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
        if len(activities) < 2:
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
                    if len(activities) >= 2:
                        break
                if len(activities) >= 2:
                    break
        result.append({"day": planned["day"], "city": day.get("city") or planned.get("city"),
                       "activities": _fit_schedule(activities)[:4]})
    result.extend(kept)
    result.sort(key=lambda item: int(item.get("day") or 0))
    return result
