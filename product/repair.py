"""确定性的餐饮补位与时间修复：只做选择和时间安排，不新增事实。"""

from typing import Any

from product.crosscheck import MEAL_MINUTES
from product.names import clean_place_name
from product.postprocess import _duration_minutes, _hhmm, _minutes_of

RESTAURANT_WORDS = (
    "火锅", "餐厅", "小吃", "馆", "菜", "面", "粉", "烧烤", "咖啡", "茶",
    "米线", "串", "饭", "食府", "酒楼", "食", "烘焙", "甜品",
)
LUNCH = "12:30"
DINNER = "18:30"


def _intervals(day: dict[str, Any]) -> list[tuple[int, int]]:
    spans = []
    for activity in day.get("activities") or []:
        start = _minutes_of(activity.get("start"))
        spans.append((start, start + _duration_minutes(activity.get("duration"))))
    return sorted(spans)


def _free(start: int, intervals: list[tuple[int, int]]) -> bool:
    end = start + MEAL_MINUTES
    return not any(start < span_end and end > span_start for span_start, span_end in intervals)


def _gaps(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """一天里能放下 60 分钟吃饭的空档（07:00–23:00）。"""
    gaps = []
    cursor = 7 * 60
    for span_start, span_end in intervals:
        if span_start - cursor >= MEAL_MINUTES:
            gaps.append((cursor, span_start))
        cursor = max(cursor, span_end)
    if 23 * 60 - cursor >= MEAL_MINUTES:
        gaps.append((cursor, 23 * 60))
    return gaps


def repair_meal_times(day: dict[str, Any]) -> bool:
    """把与活动重叠的餐挪到最近的空档，返回是否改动过。"""
    meals = day.get("meals") or []
    if not meals:
        return False
    intervals = _intervals(day)
    gaps = _gaps(intervals)
    used: list[tuple[int, int]] = []
    changed = False
    for meal in meals:
        start = _minutes_of(meal.get("start"))
        if start >= 0 and _free(start, intervals) and _free(start, used):
            used.append((start, start + MEAL_MINUTES))
            continue
        preferred = 18 * 60 if meal.get("type") == "dinner" else 12 * 60 + 30
        best = None
        for gap_start, gap_end in gaps:
            if _free(gap_start, used) is False:
                continue
            slot = min(max(preferred, gap_start), gap_end - MEAL_MINUTES)
            if _free(slot, used):
                distance = abs(slot - preferred)
                if best is None or distance < best[0]:
                    best = (distance, slot)
        if best is not None:
            slot = best[1]
            meal["start"] = _hhmm(slot)
            used.append((slot, slot + MEAL_MINUTES))
            changed = True
    return changed


def _candidates(day: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prefix = "d%s-" % day.get("day")
    found = []
    for source in evidence:
        if not str(source.get("id") or "").startswith(prefix) or source.get("status") != "ok":
            continue
        for item in source.get("items") or []:
            name = clean_place_name(item.get("name"))
            if not name or not any(word in name for word in RESTAURANT_WORDS):
                continue
            found.append({"title": name, "evidence_id": source["id"],
                          "coordinates": item.get("coordinates")})
    return found


def pick_meals(day: dict[str, Any], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """锚定为饮食/住宿且模型没给餐时，从证据里挑最近的餐馆补位。"""
    if day.get("meals"):
        return day["meals"]
    candidates = _candidates(day, evidence)
    if not candidates:
        return []
    activities = [item for item in day.get("activities") or [] if item.get("coordinates")]
    if activities:
        from product.geo import haversine_km, parse_coord

        def _distance(item: dict[str, Any]) -> float:
            coord = parse_coord(item)
            if not coord:
                return 999.0
            return min(haversine_km(coord, parse_coord(activity)) for activity in activities)

        candidates.sort(key=_distance)
    intervals = _intervals(day)
    picked = []
    for start_text in (LUNCH, DINNER):
        start = _minutes_of(start_text)
        if not _free(start, intervals):
            continue
        candidate = candidates[len(picked)] if len(candidates) > len(picked) else None
        if not candidate:
            break
        meal = {"type": "lunch" if start_text == LUNCH else "dinner",
                "title": candidate["title"], "start": start_text,
                "evidence_id": candidate["evidence_id"]}
        if candidate.get("coordinates"):
            meal["coordinates"] = candidate["coordinates"]
        picked.append(meal)
    return picked

