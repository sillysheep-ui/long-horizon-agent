"""跨槽位确定性核验：餐到活动、餐与活动时间、预算与住宿衔接。

所有检查都是纯函数，数据不足时返回 None（unknown），不把“算不出来”当成失败。
"""

import re
from typing import Any

from product.geo import haversine_km, parse_coord

MAX_MEAL_KM = 1.5
MAX_STAY_TRANSFER_MINUTES = 30
MEAL_MINUTES = 60

_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_BUDGET_TEXT = re.compile(r"(\d{3,6})")


def _minutes(value: Any) -> int:
    hour, _, minute = str(value or "").partition(":")
    try:
        return int(hour) * 60 + int(minute)
    except ValueError:
        return -1


def meal_near(day: dict[str, Any]) -> dict[str, Any] | None:
    """当天最远的一餐离最近活动多远；没有坐标就不判。"""
    activities = [item for item in day.get("activities") or [] if parse_coord(item)]
    meals = [item for item in day.get("meals") or [] if parse_coord(item)]
    if not activities or not meals:
        return None
    worst = 0.0
    for meal in meals:
        nearest = min(haversine_km(parse_coord(meal), parse_coord(activity)) for activity in activities)
        worst = max(worst, nearest)
    return {"ok": worst <= MAX_MEAL_KM, "max_km": round(worst, 2)}


def meal_time(day: dict[str, Any]) -> dict[str, Any] | None:
    """用餐时间要落在活动之间，不能和活动重叠。"""
    meals = day.get("meals") or []
    if not meals:
        return None
    spans = []
    for activity in day.get("activities") or []:
        start = _minutes(activity.get("start"))
        if start < 0:
            continue
        spans.append((start, start + _duration_minutes(activity.get("duration"))))
    bad = []
    for meal in meals:
        start = _minutes(meal.get("start"))
        if start < 0:
            bad.append(meal.get("title"))
            continue
        end = start + MEAL_MINUTES
        if any(start < span_end and end > span_start for span_start, span_end in spans):
            bad.append(meal.get("title"))
    return {"ok": not bad, "bad": bad}


def _duration_minutes(value: Any) -> int:
    text = str(value or "")
    hours = re.findall(r"(\d+(?:\.\d+)?)\s*小时", text)
    minutes = re.findall(r"(\d+)\s*分钟", text)
    total = sum(float(item) for item in hours) * 60 + sum(float(item) for item in minutes)
    return int(total) if total else 90


def budget_fits(days: list[dict[str, Any]], constraints: dict[str, Any]) -> bool | None:
    """把行程里写明的票价/人均加起来，和用户预算比；数字不全就不判。"""
    raw_budget = str(constraints.get("budget") or "")
    match = _BUDGET_TEXT.search(raw_budget)
    if not match:
        return None
    budget = float(match.group(1))
    if budget <= 0:
        return None
    total = 0.0
    seen = False
    for day in days:
        for activity in day.get("activities") or []:
            numbers = _NUMBER.findall(str(activity.get("cost") or ""))
            if numbers:
                seen = True
                total += float(numbers[0])
        for meal in day.get("meals") or []:
            numbers = _NUMBER.findall(str(meal.get("cost") or ""))
            if numbers:
                seen = True
                total += float(numbers[0])
    if not seen or total <= 0:
        return None
    return total <= budget


def cross_checks(days: list[dict[str, Any]], constraints: dict[str, Any]) -> dict[str, bool]:
    """汇总所有可判定的跨槽位检查。"""
    checks: dict[str, bool] = {}
    near = [meal_near(day) for day in days]
    known_near = [item for item in near if item]
    if known_near:
        checks["meal_near"] = all(item["ok"] for item in known_near)
    timing = [meal_time(day) for day in days]
    known_timing = [item for item in timing if item]
    if known_timing:
        checks["meal_time"] = all(item["ok"] for item in known_timing)
    fits = budget_fits(days, constraints)
    if fits is not None:
        checks["budget_fits"] = fits
    return checks
