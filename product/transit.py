"""跨天衔接：把“第 N 天结束点 → 第 N+1 天起点”的移动显式化。"""

from typing import Any

from product.geo import parse_coord, route_km
from product.postprocess import DEFAULT_STARTS, _fit_schedule, _hhmm, _minutes_of

EARLIEST_START = 7 * 60
DEFAULT_CROSS_CITY_MINUTES = 180
SAME_CITY_KM = 5.0


def estimate_transfer(prev_day: dict[str, Any], next_day: dict[str, Any]) -> dict[str, Any] | None:
    """估算前一天结束点到后一天起点的时间；同城且很近时返回 None。"""
    previous = prev_day.get("activities") or []
    following = next_day.get("activities") or []
    if not previous or not following:
        return None
    prev_city = str(prev_day.get("city") or "")
    next_city = str(next_day.get("city") or "")
    same_city = bool(prev_city and next_city and prev_city == next_city)
    prev_coord = parse_coord(previous[-1])
    next_coord = parse_coord(following[0])
    km = route_km(prev_coord, next_coord) if prev_coord and next_coord else None
    if same_city:
        if km is None or km <= SAME_CITY_KM:
            return None
        minutes = max(15, int(km / 40 * 60 + 10))
        mode = "drive"
    elif km is None:
        minutes, mode = DEFAULT_CROSS_CITY_MINUTES, "unknown"
    else:
        minutes = max(60, int(km / 160 * 60 + 45))
        mode = "train"
    first_start = _minutes_of(following[0].get("start") or DEFAULT_STARTS[0])
    start = max(EARLIEST_START, first_start - minutes)
    return {"from": prev_city or "上一站", "to": next_city or "下一站",
            "km": round(km, 1) if km is not None else None,
            "minutes": minutes, "mode": mode, "start": _hhmm(start)}


def add_transfers(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """给跨天行程补 transfer 字段，并把当天活动顺延到抵达之后。"""
    result = [dict(day) for day in days]
    for index in range(1, len(result)):
        current = result[index]
        if current.get("transfer"):
            continue
        transfer = estimate_transfer(result[index - 1], current)
        if transfer is None:
            continue
        arrival = _minutes_of(transfer["start"]) + int(transfer["minutes"])
        transfer["arrival"] = _hhmm(arrival)
        activities = [{**activity, "start": _hhmm(max(arrival, _minutes_of(activity.get("start"))))}
                      for activity in current.get("activities", [])]
        result[index] = {**current, "transfer": transfer, "activities": _fit_schedule(activities)}
    return result
