"""约束算子：每个算子是 (view, constraint) -> Finding 的纯函数。

算子只判断，不修改行程；修复由运行时负责。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from product.constraints.model import (
    Constraint,
    Finding,
    ItineraryView,
    activity_window,
    duration_minutes,
    format_hhmm,
    parse_hhmm,
    squash,
)


def _examples(items: list[str], limit: int = 3) -> str:
    return "；".join(items[:limit])


def count_between(view: ItineraryView, constraint: Constraint) -> Finding:
    """每天活动数落在 [min, max]；bounds=day_windows 时按节奏动态取上下限。"""
    dynamic = constraint.params.get("bounds") == "day_windows"
    require_days = bool(constraint.params.get("require_days", False))
    low = int(constraint.params.get("min", 0))
    high = int(constraint.params.get("max", 99))
    skip_empty = bool(constraint.params.get("skip_empty", True))
    pace = requested = windows = None
    if dynamic:
        from product.pace import PACE_LIMITS, count_bounds, detect_pace

        pace = detect_pace(view.case.get("query"))
        requested = view.constraints.get("requested_count")
        windows = {
            int(window.get("day") or 0): float(window.get("hours") or 0)
            for window in view.constraints.get("day_windows") or []
            if isinstance(window, dict) and window.get("day") and window.get("hours")
        }
        if not windows:
            low, high = PACE_LIMITS[pace][:2]
    violations: list[str] = []
    expected_parts: list[str] = []
    if require_days and not view.days:
        violations.append("没有任何安排")
    for index, day in enumerate(view.days, start=1):
        count = len(view.activities(day))
        if count == 0 and skip_empty:
            continue
        day_low, day_high = low, high
        if dynamic and windows and index in windows:
            day_low, day_high = count_bounds(windows[index], pace, requested)
            expected_parts.append(f"第{index}天 {day_low}–{day_high}")
        if not day_low <= count <= day_high:
            violations.append(f"第{index}天 {count} 个（应 {day_low}–{day_high}）")
    ok = not violations
    expected = "；".join(expected_parts) if expected_parts else f"{low}–{high} 个/天"
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "每天活动数超出允许区间",
        expected=expected,
        actual=_examples(violations),
        category=constraint.category,
        severity=constraint.severity,
    )


def time_order(view: ItineraryView, constraint: Constraint) -> Finding:
    """同一天内按开始时间升序。"""
    violations: list[str] = []
    if constraint.params.get("require_days") and not view.days:
        violations.append("没有任何安排")
    for index, day in enumerate(view.days, start=1):
        starts = [(parse_hhmm(item.get("start")), item) for item in view.activities(day)]
        starts = [(value, item) for value, item in starts if value is not None]
        for (prev, _prev_item), (current, _) in zip(starts, starts[1:], strict=False):
            if current < prev:
                violations.append(f"第{index}天 {format_hhmm(current)} 早于 {format_hhmm(prev)}")
                break
    ok = not violations
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "同一天的活动应按时间升序",
        expected="时间升序",
        actual=_examples(violations),
        category=constraint.category,
        severity=constraint.severity,
    )


def no_overlap(view: ItineraryView, constraint: Constraint) -> Finding:
    """同一天的活动 / 用餐时间不重叠。"""
    include_meals = bool(constraint.params.get("include_meals", True))
    default_minutes = int(constraint.params.get("default_minutes", 60))
    require_times = bool(constraint.params.get("require_times", False))
    require_order = bool(constraint.params.get("require_order", False))
    violations: list[str] = []
    for index, day in enumerate(view.days, start=1):
        blocks: list[tuple[int, int, str]] = []
        if require_order:
            ordered = [parse_hhmm(item.get("start")) for item in view.activities(day)]
            ordered = [value for value in ordered if value is not None]
            if ordered != sorted(ordered) or len(ordered) != len(set(ordered)):
                violations.append(f"第{index}天开始时间未按顺序或存在重复")
                break
        for item in view.activities(day):
            window = activity_window(item, default_minutes)
            if window:
                blocks.append((window[0], window[1], str(item.get("title") or item.get("display_title") or "活动")))
            elif require_times:
                violations.append(f"第{index}天「{item.get('title') or '活动'}」缺少开始时间")
                break
        if include_meals:
            for meal in day.get("meals") or []:
                if not isinstance(meal, dict):
                    continue
                window = activity_window(meal, default_minutes)
                if window:
                    blocks.append((window[0], window[1], str(meal.get("title") or "用餐")))
        blocks.sort()
        for (_start, end, name), (next_start, _, next_name) in zip(blocks, blocks[1:], strict=False):
            if next_start < end:
                violations.append(f"第{index}天 {name}（至 {format_hhmm(end)}）与 {next_name}（{format_hhmm(next_start)}）重叠")
                break
    ok = not violations
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "同一时段只应安排一件活动",
        expected="活动不重叠",
        actual=_examples(violations),
        category=constraint.category,
        severity=constraint.severity,
    )


def time_window(view: ItineraryView, constraint: Constraint) -> Finding:
    """活动开始与结束都落在证据给出的营业时间内。"""
    violations: list[str] = []
    for index, day in enumerate(view.days, start=1):
        for item in view.activities(day):
            title = str(item.get("title") or "")
            window = activity_window(item)
            detail = view.evidence_item(title)
            if window is None or not detail:
                continue
            open_at = parse_hhmm(detail.get("opentime"))
            close_at = parse_hhmm(detail.get("endtime"))
            if open_at is None or close_at is None:
                continue
            if close_at <= open_at:
                close_at += 24 * 60
            start, end = window
            if start < open_at:
                violations.append(f"第{index}天「{title}」{format_hhmm(start)} 早于营业时间 {format_hhmm(open_at)}")
            elif end > close_at:
                violations.append(f"第{index}天「{title}」{format_hhmm(end)} 晚于结束时间 {format_hhmm(close_at)}")
    ok = not violations
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "活动应安排在营业时间内",
        expected="在营业时间内",
        actual=_examples(violations),
        category=constraint.category,
        severity=constraint.severity,
    )


def sum_le(view: ItineraryView, constraint: Constraint) -> Finding:
    """每天活动总时长落在 [min, 可用时长 + tolerance] 内。"""
    limit_param = constraint.params.get("limit")
    default_hours = float(constraint.params.get("default_hours", 10.0))
    min_hours = float(constraint.params.get("min_hours", 0.0))
    tolerance = float(constraint.params.get("tolerance", 0.0))
    violations: list[str] = []
    expected = "不超过当天可用时长"
    if constraint.params.get("require_days") and not view.days:
        violations.append("没有任何安排")
    for index, day in enumerate(view.days, start=1):
        activities = view.activities(day)
        if not activities:
            continue
        limit_minutes: int | None = None
        if limit_param is not None:
            limit_minutes = int(float(limit_param) * 60)
        else:
            for window in view.constraints.get("day_windows") or []:
                if isinstance(window, dict) and int(window.get("day") or 0) == index:
                    limit_minutes = int(float(window.get("hours") or 0) * 60)
                    break
        if not limit_minutes:
            limit_minutes = int(default_hours * 60)
        total = sum(duration_minutes(item.get("duration")) or 0 for item in activities)
        lower = min(min_hours, limit_minutes / 60) * 60
        upper = limit_minutes + tolerance * 60
        if total > upper or total < lower:
            violations.append(f"第{index}天 {total / 60:.1f} 小时")
        if limit_param is not None:
            expected = f"{min_hours:g}–{float(limit_param) + tolerance:g} 小时/天"
        else:
            expected = f"{min_hours:g} 小时–当天可用时长"
    ok = not violations
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "当天活动总时长超出允许区间",
        expected=expected,
        actual=_examples(violations),
        category=constraint.category,
        severity=constraint.severity,
    )


def exists_contains(view: ItineraryView, constraint: Constraint) -> Finding:
    """用户点名的地点必须出现在某天的安排里。"""
    names = view.constraints.get("named_places") or []
    if not names:
        return Finding(
            id=constraint.id,
            ok=True,
            detail="没有点名地点",
            expected="",
            actual="",
            category=constraint.category,
            severity=constraint.severity,
        )
    haystack = squash(
        " ".join(
            str(item.get(field) or "")
            for day in view.days
            for item in view.activities(day)
            for field in ("title", "display_title", "description")
        )
    )
    missing = [str(name) for name in names if squash(str(name)) not in haystack]
    ok = not missing
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "点名地点没有出现在行程里",
        expected="点名地点全部出现",
        actual=_examples(missing),
        category=constraint.category,
        severity=constraint.severity,
    )


def status_equals(view: ItineraryView, constraint: Constraint) -> Finding:
    """结果状态等于给定值（如 complete）。"""
    wanted = str(constraint.params.get("value") or "complete")
    actual = view.status
    ok = actual == wanted
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else f"状态应为 {wanted}",
        expected=wanted,
        actual=actual,
        category=constraint.category,
        severity=constraint.severity,
    )


def count_equals(view: ItineraryView, constraint: Constraint) -> Finding:
    """顶层数量等于期望值（天数）；期望来自用例。"""
    if constraint.params.get("target") == "days":
        wanted = view.case.get("days")
        actual = len(view.days)
        ok = bool(view.days) and (wanted is None or actual == int(wanted))
        return Finding(
            id=constraint.id,
            ok=ok,
            detail="" if ok else "天数与需求不一致",
            expected=str(wanted) if wanted is not None else "非空",
            actual=str(actual),
            category=constraint.category,
            severity=constraint.severity,
        )
    raise KeyError(f"count_equals 不支持 target={constraint.params.get('target')}")


def unique_values(view: ItineraryView, constraint: Constraint) -> Finding:
    """某字段在同一行程内不重复（默认活动标题）。"""
    field = str(constraint.params.get("field") or "title")
    values = [str(item.get(field) or "") for day in view.days for item in view.activities(day)]
    duplicates = sorted({value for value in values if value and values.count(value) > 1})
    ok = not duplicates
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "存在重复条目",
        expected="不重复",
        actual=_examples(duplicates),
        category=constraint.category,
        severity=constraint.severity,
    )


def text_clean(view: ItineraryView, constraint: Constraint) -> Finding:
    """标题长度与噪声词检查。"""
    max_len = int(constraint.params.get("max_len", 24))
    tokens = [str(token) for token in constraint.params.get("tokens") or []]
    titles = [str(item.get("title") or "") for day in view.days for item in view.activities(day)]
    bad = [title for title in titles if len(title) > max_len or any(token in title for token in tokens)]
    ok = bool(titles) and not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "标题过长或含网页噪声词",
        expected=f"≤{max_len} 字且无噪声词",
        actual=_examples([title[:20] for title in bad]),
        category=constraint.category,
        severity=constraint.severity,
    )


def city_coverage(view: ItineraryView, constraint: Constraint) -> Finding:
    """每天的城市都属于用例期望城市。"""
    wanted = [str(city).strip().rstrip("市") for city in view.case.get("cities") or [] if str(city).strip()]
    if not wanted:
        return Finding(id=constraint.id, ok=True, expected="", actual="",
                       category=constraint.category, severity=constraint.severity)
    actual = [str(day.get("city") or "").strip().rstrip("市") for day in view.days]
    bad = [city for city in actual if city not in wanted]
    ok = bool(view.days) and not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "行程城市与需求不一致",
        expected="/".join(wanted),
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
    )


def evidence_ok(view: ItineraryView, constraint: Constraint) -> Finding:
    """每个活动都绑定了 status=ok 的证据。"""
    evidence = {item.get("id"): item for item in view.evidence if isinstance(item, dict)}
    activities = [item for day in view.days for item in view.activities(day)]
    bad = [str(item.get("title") or "") for item in activities
           if (evidence.get(item.get("evidence_id")) or {}).get("status") != "ok"]
    ok = bool(activities) and not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "存在没有可追溯证据的活动",
        expected="全部绑定 ok 证据",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
    )


def valid_times(view: ItineraryView, constraint: Constraint) -> Finding:
    """每个活动的开始时间都是 HH:MM。"""
    import re

    pattern = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")
    bad = []
    for day in view.days:
        for item in view.activities(day):
            if not pattern.match(str(item.get("start") or "")):
                bad.append(f"第{day.get('day')}天「{item.get('title') or '活动'}」")
    ok = not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "开始时间不是 HH:MM",
        expected="HH:MM",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
    )


def distance_le(view: ItineraryView, constraint: Constraint) -> Finding:
    """当天用餐到最近活动的距离不超过阈值；缺坐标不算失败。"""
    from product.geo import haversine_km, parse_coord

    radius = float(constraint.params.get("radius_km", 1.5))
    worst = 0.0
    known = False
    for day in view.days:
        activities = [item for item in view.activities(day) if parse_coord(item)]
        meals = [item for item in day.get("meals") or [] if isinstance(item, dict) and parse_coord(item)]
        if not activities or not meals:
            continue
        for meal in meals:
            nearest = min(haversine_km(parse_coord(meal), parse_coord(activity)) for activity in activities)
            worst = max(worst, nearest)
            known = True
    ok = worst <= radius
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else f"有餐离当天活动超过 {radius:g} 公里",
        expected=f"≤ {radius:g} km",
        actual=f"{worst:.2f} km" if known else "",
        category=constraint.category,
        severity=constraint.severity,
        meta={"max_km": round(worst, 2), "known": known},
    )


def budget_sum_le(view: ItineraryView, constraint: Constraint) -> Finding:
    """把行程里写明的票价/人均加起来与预算比较；数字不全不算失败。"""
    import re

    budget_text = str(view.constraints.get("budget") or "")
    match = re.search(r"(\d{3,6})", budget_text)
    budget = float(match.group(1)) if match else 0.0
    numbers = re.compile(r"\d+(?:\.\d+)?")
    total = 0.0
    seen = False
    for day in view.days:
        for item in list(view.activities(day)) + [meal for meal in day.get("meals") or [] if isinstance(meal, dict)]:
            found = numbers.findall(str(item.get("cost") or ""))
            if found:
                seen = True
                total += float(found[0])
    known = bool(match) and budget > 0 and seen and total > 0
    ok = (total <= budget) if known else True
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "行程花费超过预算",
        expected=f"≤ {budget:.0f}" if budget else "",
        actual=f"{total:.0f}" if known else "",
        category=constraint.category,
        severity=constraint.severity,
        meta={"total": round(total, 1), "budget": budget, "known": known},
    )


def meal_conflicts(view: ItineraryView, constraint: Constraint) -> Finding:
    """用餐时间与活动重叠；没有 meals 视为不适用。"""
    from product.constraints.model import activity_window

    minutes = int(constraint.params.get("meal_minutes", 60))
    bad: list[str] = []
    known = False
    for day in view.days:
        meals = [meal for meal in day.get("meals") or [] if isinstance(meal, dict)]
        if not meals:
            continue
        known = True
        spans = []
        for activity in view.activities(day):
            window = activity_window(activity, 90)
            if window:
                spans.append(window)
        for meal in meals:
            start, _ = activity_window(meal, minutes) or (None, None)
            title = str(meal.get("title") or "用餐")
            if start is None:
                bad.append(title)
                continue
            end = start + minutes
            if any(start < span_end and end > span_start for span_start, span_end in spans):
                bad.append(title)
    ok = not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "用餐时间与活动重叠",
        expected="不重叠",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
        meta={"bad": bad, "known": known},
    )


_PERIODS = (("morning", 6 * 60, 12 * 60), ("afternoon", 12 * 60, 17 * 60 + 30),
            ("evening", 17 * 60 + 30, 24 * 60))
_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _clock_minutes(value: Any) -> int:
    match = _CLOCK.match(str(value or ""))
    if not match:
        return -1
    return int(match.group(1)) * 60 + int(match.group(2))


def _duration_or_default(value: Any, default: int = 90) -> int:
    minutes = duration_minutes(value)
    return minutes if minutes else default


def _evidence_map(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id") or ""): item for item in evidence if isinstance(item, dict)}


def _item_names(evidence_item: dict[str, Any]) -> list[str]:
    return [str(item.get("name") or "") for item in evidence_item.get("items") or []
            if isinstance(item, dict) and item.get("name")]


def _evidence_text(evidence_item: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in evidence_item.get("items") or []:
        if not isinstance(item, dict):
            continue
        for key in ("name", "snippet", "price", "opentime", "endtime", "recommendedfood", "featurehoteltype"):
            if item.get(key) is not None:
                parts.append(str(item.get(key)))
    return " ".join(parts)


def _day_hours(view: ItineraryView, number: int) -> float:
    for window in view.constraints.get("day_windows") or []:
        if isinstance(window, dict) and int(window.get("day") or 0) == number:
            return float(window.get("hours") or 10.0)
    return 10.0


def distinct_periods(view: ItineraryView, constraint: Constraint) -> Finding:
    """每天覆盖的时段数达到下限（上午/下午/晚上）。"""
    need = int(constraint.params.get("min_periods", 2))
    bad: list[str] = []
    for day in view.days:
        buckets = set()
        for item in view.activities(day):
            start = _clock_minutes(item.get("start"))
            if start < 0:
                continue
            end = start + _duration_or_default(item.get("duration"))
            for name, begin, finish in _PERIODS:
                if start < finish and end > begin:
                    buckets.add(name)
        if len(buckets) < need:
            bad.append(f"第{day.get('day')}天只覆盖 {len(buckets)} 个时段")
    ok = not bad and bool(view.days)
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else f"每天至少覆盖 {need} 个时段",
        expected=f"{need} 个时段",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
    )


def activity_length(view: ItineraryView, constraint: Constraint) -> Finding:
    """单个活动的停留时长落在节奏区间内。"""
    from product.pace import PACE_PLAN, detect_pace

    plan = PACE_PLAN[detect_pace(view.case.get("query"))]["activity"]
    low = float(constraint.params.get("lower_offset", 1.0))
    high = float(constraint.params.get("upper_offset", 3.5))
    bad: list[str] = []
    for day in view.days:
        day_cap = min(_day_hours(view, int(day.get("day") or 0)), plan[1] + high)
        for item in view.activities(day):
            hours = _duration_or_default(item.get("duration"), 0) / 60
            if hours < plan[0] - low or hours > day_cap:
                bad.append(f"第{day.get('day')}天「{item.get('title') or '活动'}」{hours:g} 小时")
    ok = not bad and bool(view.days)
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "单个活动时长超出节奏区间",
        expected=f"{plan[0] - low:g}–{plan[1] + high:g} 小时",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
    )


def free_time(view: ItineraryView, constraint: Constraint) -> Finding:
    """按当天可用时长计算留白比例。"""
    from product.pace import PACE_PLAN, detect_pace

    plan = PACE_PLAN[detect_pace(view.case.get("query"))]["free"]
    upper_slack = float(constraint.params.get("upper_slack", 0.07))
    bad: list[str] = []
    for day in view.days:
        activities = view.activities(day)
        number = int(day.get("day") or 0)
        if len(activities) < 2:
            ratio = 1.0
        else:
            ordered = sorted(activities, key=lambda item: _clock_minutes(item.get("start")))
            start = _clock_minutes(ordered[0].get("start"))
            end = max(_clock_minutes(item.get("start")) + _duration_or_default(item.get("duration"))
                      for item in ordered)
            span = max(end - start, 1)
            busy = sum(_duration_or_default(item.get("duration")) for item in ordered)
            window = max(_day_hours(view, number) * 60.0, span, 1.0)
            ratio = max(0.0, 1 - busy / window)
        if ratio < plan[0] or ratio > plan[1] + upper_slack:
            bad.append(f"第{number}天留白 {ratio:.2f}")
    ok = not bad and bool(view.days)
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "留白比例超出节奏区间",
        expected=f"{plan[0]:.2f}–{plan[1] + upper_slack:.2f}",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
    )


def cost_grounded(view: ItineraryView, constraint: Constraint) -> Finding:
    """行程里的价格数字必须能在证据原文或描述里找到。"""
    evidence = _evidence_map(view.evidence)
    bad: list[str] = []
    checked = 0
    for day in view.days:
        for item in view.activities(day):
            numbers = _DIGITS.findall(str(item.get("cost") or ""))
            if not numbers:
                continue
            checked += 1
            source = _evidence_text(evidence.get(str(item.get("evidence_id") or "")) or {}) + str(item.get("description") or "")
            if any(number not in source for number in numbers):
                bad.append(f"第{day.get('day')}天「{item.get('title') or '活动'}」{item.get('cost')}")
    if not checked:
        return Finding(id=constraint.id, ok=True, detail="没有可核验的价格数字", category=constraint.category,
                       severity=constraint.severity, meta={"skip": True, "known": 0})
    ok = not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "价格没有来源",
        expected="数字必须出现在证据里",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
        meta={"known": checked},
    )


def fields_filled(view: ItineraryView, constraint: Constraint) -> Finding:
    """每个活动都必须有开始时间、时长、交通和费用。"""
    fields = [str(field) for field in constraint.params.get("fields") or ("start", "duration", "transport", "cost")]
    activities = [item for day in view.days for item in view.activities(day)]
    missing: list[str] = []
    for item in activities:
        absent = [field for field in fields if not item.get(field)]
        if absent:
            missing.append(f"「{item.get('title') or '活动'}」缺 {','.join(absent)}")
    ok = bool(activities) and not missing
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else ("行程为空" if not activities else "活动字段不完整"),
        expected="开始时间/时长/交通/费用",
        actual=_examples(missing),
        category=constraint.category,
        severity=constraint.severity,
    )


def title_matches(view: ItineraryView, constraint: Constraint) -> Finding:
    """活动标题必须落在对应证据的地点名上；没有证据名的不判。"""
    from product.names import title_matches as matches

    evidence = _evidence_map(view.evidence)
    bad: list[str] = []
    for day in view.days:
        for item in view.activities(day):
            names = _item_names(evidence.get(str(item.get("evidence_id") or "")) or {})
            if names and not matches(str(item.get("title") or ""), names):
                bad.append(f"「{item.get('title') or '活动'}」不在证据里")
    ok = not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "活动标题与证据地点名不一致",
        expected="标题来自证据",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
    )


def _match_item(evidence_item: dict[str, Any], title: Any) -> dict[str, Any] | None:
    from product.names import title_matches as matches

    items = [item for item in evidence_item.get("items") or [] if isinstance(item, dict)] if evidence_item else []
    wanted = str(title or "")
    for item in items:
        name = str(item.get("name") or "")
        if name and (name == wanted or wanted in name or name in wanted):
            return item
    for item in items:
        name = str(item.get("name") or "")
        if name and matches(wanted, [name]):
            return item
    return None


def opening_hours(view: ItineraryView, constraint: Constraint) -> Finding:
    """活动必须落在证据给出的营业时间窗内；无时间数据的不判。"""
    evidence = _evidence_map(view.evidence)
    checked = 0
    bad: list[str] = []
    for day in view.days:
        for item in view.activities(day):
            match = _match_item(evidence.get(str(item.get("evidence_id") or "")) or {}, item.get("title"))
            if not match:
                continue
            start = _clock_minutes(item.get("start"))
            open_at = _clock_minutes(match.get("opentime"))
            close_at = _clock_minutes(match.get("endtime"))
            if start < 0 or open_at < 0 or close_at < 0 or close_at <= open_at:
                continue  # 00:00–00:00 这类占位数据按“时间未知”处理
            checked += 1
            duration = _duration_or_default(item.get("duration"))
            if not (open_at <= start and start + duration <= close_at):
                bad.append(f"「{item.get('title') or '活动'}」{item.get('start')} 不在 {match.get('opentime')}–{match.get('endtime')}")
    if not checked:
        return Finding(id=constraint.id, ok=True, detail="无营业时间数据", category=constraint.category,
                       severity=constraint.severity, meta={"skip": True, "known": 0})
    ok = not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "活动落在营业时间之外",
        expected="在营业时间内",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
        meta={"known": checked},
    )


def meals_grounded(view: ItineraryView, constraint: Constraint) -> Finding:
    """每顿饭的名字必须能在证据里找到；没有餐饮槽位时不判。"""
    from product.names import title_matches as matches

    evidence = _evidence_map(view.evidence)
    bad: list[str] = []
    total = 0
    for day in view.days:
        for meal in day.get("meals") or []:
            if not isinstance(meal, dict):
                continue
            total += 1
            names = _item_names(evidence.get(str(meal.get("evidence_id") or "")) or {})
            if not names or not matches(str(meal.get("title") or ""), names):
                bad.append(f"「{meal.get('title') or '用餐'}」没有来源")
    if not total:
        return Finding(id=constraint.id, ok=True, detail="没有餐饮槽位", category=constraint.category,
                       severity=constraint.severity, meta={"skip": True})
    ok = not bad
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "餐饮没有证据来源",
        expected="每顿都有来源",
        actual=_examples(bad),
        category=constraint.category,
        severity=constraint.severity,
    )


def anchor_satisfied(view: ItineraryView, constraint: Constraint) -> Finding:
    """锚定饮食/住宿时，每天都要有对应槽位；其他锚定不判。"""
    anchor = str(view.case.get("anchor") or "")
    if anchor not in ("food", "stay"):
        return Finding(id=constraint.id, ok=True, detail="非饮食/住宿锚定", category=constraint.category,
                       severity=constraint.severity, meta={"skip": True})
    key = "meals" if anchor == "food" else "lodging_area"
    missing = [f"第{day.get('day')}天" for day in view.days if not day.get(key)]
    ok = bool(view.days) and not missing
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else f"锚定 {anchor} 但缺少 {key}",
        expected="每天都有槽位",
        actual=_examples(missing),
        category=constraint.category,
        severity=constraint.severity,
    )


def named_places_covered(view: ItineraryView, constraint: Constraint) -> Finding:
    """用户点名地点必须出现在行程标题里；没点名时不判。"""
    wanted = [str(place) for place in view.case.get("named_places") or [] if str(place).strip()]
    if not wanted:
        return Finding(id=constraint.id, ok=True, detail="没有点名地点", category=constraint.category,
                       severity=constraint.severity, meta={"skip": True})
    titles = [_squash(item.get("title")) for day in view.days for item in view.activities(day)]
    missing = []
    for place in wanted:
        target = _squash(place)
        if not target or not any(target in title or title in target for title in titles if title):
            missing.append(place)
    ok = not missing
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "有点名地点没有排进行程",
        expected=",".join(wanted),
        actual=_examples(missing),
        category=constraint.category,
        severity=constraint.severity,
    )


def _squash(value: Any) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or ""))


OPERATORS: dict[str, Callable[[ItineraryView, Constraint], Finding]] = {
    "count_between": count_between,
    "time_order": time_order,
    "no_overlap": no_overlap,
    "time_window": time_window,
    "sum_le": sum_le,
    "exists_contains": exists_contains,
    "status_equals": status_equals,
    "count_equals": count_equals,
    "unique_values": unique_values,
    "text_clean": text_clean,
    "city_coverage": city_coverage,
    "evidence_ok": evidence_ok,
    "valid_times": valid_times,
    "distance_le": distance_le,
    "budget_sum_le": budget_sum_le,
    "meal_conflicts": meal_conflicts,
    "distinct_periods": distinct_periods,
    "activity_length": activity_length,
    "free_time": free_time,
    "cost_grounded": cost_grounded,
    "fields_filled": fields_filled,
    "title_matches": title_matches,
    "opening_hours": opening_hours,
    "meals_grounded": meals_grounded,
    "anchor_satisfied": anchor_satisfied,
    "named_places_covered": named_places_covered,
}


def operator_names() -> tuple[str, ...]:
    return tuple(sorted(OPERATORS))
