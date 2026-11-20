"""模型日程的硬伤检查：只看时间与数量，不评判好坏。"""

import re
from typing import Any

from product.pace import DEFAULT_DAY_HOURS
from product.postprocess import _duration_minutes, _minutes_of, day_hours

_TAG = re.compile(r"[^\u4e00-\u9fffA-Za-z0-9]")


def _label(value: Any) -> str:
    return str(value or "").strip()[:16]


def check_day_plan(days: Any, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回 {ok, violations}；violations 是给模型的可读反馈。"""
    constraints = constraints or {}
    violations: list[dict[str, Any]] = []
    if not isinstance(days, list) or not days:
        return {"ok": False, "violations": [{"day": None, "kind": "empty", "detail": "没有输出任何一天的安排"}]}
    expected = constraints.get("days")
    if isinstance(expected, int) and expected > 0 and len(days) != expected:
        violations.append({"day": None, "kind": "day_count",
                           "detail": "应该是 %d 天，实际输出了 %d 天" % (expected, len(days))})
    windows = constraints.get("day_windows")
    for day in days:
        if not isinstance(day, dict):
            continue
        number = day.get("day")
        activities = [item for item in day.get("activities") or [] if isinstance(item, dict)]
        if len(activities) > 4:
            violations.append({"day": number, "kind": "too_many",
                               "detail": "第%s天有 %d 个活动，超过 4 个" % (_label(number), len(activities))})
        starts = []
        total = 0
        for item in activities:
            text = str(item.get("start") or "")
            if not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", text):
                violations.append({"day": number, "kind": "bad_time",
                                   "detail": "第%s天「%s」的开始时间不是 HH:MM" % (_label(number), _label(item.get("title")))})
                continue
            starts.append(_minutes_of(text))
            total += _duration_minutes(item.get("duration"))
        if starts and starts != sorted(starts):
            violations.append({"day": number, "kind": "order",
                               "detail": "第%s天的活动开始时间不是递增的" % _label(number)})
        cursor = None
        for item in activities:
            start = _minutes_of(item.get("start"))
            if cursor is not None and start < cursor:
                violations.append({"day": number, "kind": "overlap",
                                   "detail": "第%s天「%s」和上一个活动时间重叠" % (_label(number), _label(item.get("title")))})
                break
            cursor = start + _duration_minutes(item.get("duration"))
        hours = day_hours(windows, number) or DEFAULT_DAY_HOURS
        if total > hours * 60:
            violations.append({"day": number, "kind": "over_hours",
                               "detail": "第%s天活动共 %.1f 小时，超过当天可用 %.1f 小时"
                                         % (_label(number), total / 60, hours)})
    return {"ok": not violations, "violations": violations}
