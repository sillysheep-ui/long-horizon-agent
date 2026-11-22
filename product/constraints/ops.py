"""约束算子：每个算子是 (view, constraint) -> Finding 的纯函数。

算子只判断，不修改行程；修复由运行时负责。
"""

from __future__ import annotations

from collections.abc import Callable

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
}


def operator_names() -> tuple[str, ...]:
    return tuple(sorted(OPERATORS))
