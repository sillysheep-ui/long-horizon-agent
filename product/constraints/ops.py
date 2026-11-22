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
    """每天活动数落在 [min, max]。"""
    low = int(constraint.params.get("min", 0))
    high = int(constraint.params.get("max", 99))
    skip_empty = bool(constraint.params.get("skip_empty", True))
    violations: list[str] = []
    for index, day in enumerate(view.days, start=1):
        count = len(view.activities(day))
        if count == 0 and skip_empty:
            continue
        if not low <= count <= high:
            violations.append(f"第{index}天 {count} 个")
    ok = not violations
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else f"每天活动数应为 {low}–{high} 个",
        expected=f"{low}–{high} 个/天",
        actual=_examples(violations),
        category=constraint.category,
        severity=constraint.severity,
    )


def time_order(view: ItineraryView, constraint: Constraint) -> Finding:
    """同一天内按开始时间升序。"""
    violations: list[str] = []
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
    violations: list[str] = []
    for index, day in enumerate(view.days, start=1):
        blocks: list[tuple[int, int, str]] = []
        for item in view.activities(day):
            window = activity_window(item)
            if window:
                blocks.append((window[0], window[1], str(item.get("title") or item.get("display_title") or "活动")))
        for meal in day.get("meals") or []:
            if not isinstance(meal, dict):
                continue
            window = activity_window(meal)
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
    """每天活动总时长不超过当天可用时长（或固定上限）。"""
    limit_param = constraint.params.get("limit")
    violations: list[str] = []
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
            continue
        total = sum(duration_minutes(item.get("duration")) or 0 for item in activities)
        if total > limit_minutes:
            violations.append(f"第{index}天 {total / 60:.1f} 小时")
    ok = not violations
    expected = f"≤ {int(float(limit_param) * 60) / 60:.1f} 小时/天" if limit_param is not None else "不超过当天可用时长"
    return Finding(
        id=constraint.id,
        ok=ok,
        detail="" if ok else "当天活动总时长超出可用时间",
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


OPERATORS: dict[str, Callable[[ItineraryView, Constraint], Finding]] = {
    "count_between": count_between,
    "time_order": time_order,
    "no_overlap": no_overlap,
    "time_window": time_window,
    "sum_le": sum_le,
    "exists_contains": exists_contains,
}


def operator_names() -> tuple[str, ...]:
    return tuple(sorted(OPERATORS))
