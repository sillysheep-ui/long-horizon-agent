"""模型排期的规则校验：只判断对错，不修改模型给的顺序和时间。

校验范围是“照着走会不会出事”：地点是否来自证据、时间说不说得通、
开门没有、两站之间来不来得及、餐食是否落在营业时间。
"""

from __future__ import annotations

import re
from typing import Any

from product.global_plan import coord_of, haversine_km, open_window
from product.names import clean_place_name

DAY_START_MINUTES = 9 * 60
DAY_END_MINUTES = 21 * 60
MEAL_MINUTES = 60
SPEED_KMH = 20.0
TRANSFER_BUFFER_MINUTES = 10
MAX_ACTIVITIES = 4

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_time(value: Any) -> int | None:
    """把 "9:30" / "09:30" 转成分钟；不合法返回 None。"""
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def duration_minutes(activity: dict[str, Any]) -> int:
    """优先读 duration_minutes，其次解析 "2.5 小时" / "90 分钟" 这类文本。"""
    raw = activity.get("duration_minutes")
    if isinstance(raw, int | float) and raw > 0:
        return int(raw)
    text = str(activity.get("duration") or "")
    match = _DURATION_RE.search(text)
    if not match:
        return 90
    number = float(match.group(1))
    if "小时" in text or "h" in text.lower():
        return max(15, int(number * 60))
    return max(15, int(number))


def _place_index(evidence: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    places: dict[str, dict[str, Any]] = {}
    for entry in evidence:
        for item in entry.get("items", []):
            name = clean_place_name(str(item.get("name") or ""))
            if name:
                places.setdefault(name, {**item, "_evidence_id": entry.get("id")})
    return places


def _timeline(day: dict[str, Any], places: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for kind, key in (("activity", "activities"), ("meal", "meals")):
        for entry in day.get(key, []) or []:
            if not isinstance(entry, dict):
                continue
            name = clean_place_name(str(entry.get("title") or ""))
            place = places.get(name) or {}
            items.append({**place, **entry, "_kind": kind})
    return items


def _detail(code: str, day: int, name: str, message: str) -> dict[str, Any]:
    return {"day": day, "code": code, "name": name, "detail": message}


def validate_plan(days: Any, evidence: list[dict[str, Any]],
                  constraints: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """返回问题列表；空列表表示这份排期可以直接执行。"""
    constraints = constraints or {}
    places = _place_index(evidence)
    issues: list[dict[str, Any]] = []
    for day in days if isinstance(days, list) else []:
        if not isinstance(day, dict):
            continue
        number = int(day.get("day") or 0)
        activities = [item for item in day.get("activities", []) or [] if isinstance(item, dict)]
        if len(activities) > MAX_ACTIVITIES:
            issues.append(_detail("too_many", number, "",
                                  "一天排了 %d 个活动，超过 %d 个" % (len(activities), MAX_ACTIVITIES)))
        entries = _timeline(day, places)
        for item in entries:
            if clean_place_name(str(item.get("title") or "")) not in places:
                issues.append(_detail("unknown_place", number, str(item.get("title") or ""),
                                      "不在本次核实到的地点里"))
        timed: list[tuple[int, int, dict[str, Any]]] = []
        for item in entries:
            start = parse_time(item.get("start"))
            if start is None:
                issues.append(_detail("bad_time", number, str(item.get("title") or ""),
                                      "开始时间不是 HH:MM：%r" % (item.get("start"),)))
                continue
            length = duration_minutes(item) if item.get("_kind") == "activity" else MEAL_MINUTES
            timed.append((start, start + length, item))
        timed.sort(key=lambda row: row[0])
        previous: tuple[int, int, dict[str, Any]] | None = None
        for start, end, item in timed:
            name = str(item.get("title") or "")
            if start < DAY_START_MINUTES:
                issues.append(_detail("too_early", number, name, "开始时间早于 09:00"))
            if end > DAY_END_MINUTES:
                issues.append(_detail("day_end", number, name, "结束时间晚于 21:00"))
            if previous and start < previous[1]:
                issues.append(_detail("overlap", number, name,
                                      "与上一项（%s，%s 结束）时间重叠" % (
                                          previous[2].get("title"), _hhmm(previous[1]))))
            place = places.get(name)
            if place is not None:
                window = open_window(place)
                if window and (start < window[0] or end > window[1]):
                    issues.append(_detail("closed", number, name,
                                          "营业时间 %s–%s，排期 %s–%s 不在其中" % (
                                              _hhmm(window[0]), _hhmm(window[1]),
                                              _hhmm(start), _hhmm(end))))
                if previous:
                    gap = start - previous[1]
                    need = _transfer_minutes(previous[2], item)
                    if gap < need:
                        issues.append(_detail("transit_short", number, name,
                                              "上一项 %s 结束后只留了 %d 分钟，估算需要 %d 分钟" % (
                                                  previous[2].get("title"), gap, need)))
            previous = (start, end, item)
        if not any(item.get("_kind") == "meal" for item in entries) and constraints.get("day_span") != "half":
            issues.append(_detail("missing_meal", number, "", "一整天没有安排任何餐食"))
    return issues


def _transfer_minutes(origin: dict[str, Any], destination: dict[str, Any]) -> int:
    start = coord_of(origin)
    end = coord_of(destination)
    if not start or not end:
        return 0
    return int(haversine_km(start, end) / SPEED_KMH * 60) + TRANSFER_BUFFER_MINUTES


def _hhmm(minutes: int) -> str:
    return "%02d:%02d" % (minutes // 60, minutes % 60)
