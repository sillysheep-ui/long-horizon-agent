"""餐饮补位与用餐时间修复。"""

from product.repair import pick_meals, repair_meal_times


def _day(meals=None):
    return {
        "day": 1,
        "activities": [
            {"title": "西湖", "start": "09:30", "duration": "2 小时", "coordinates": "120.15,30.25"},
            {"title": "灵隐寺", "start": "14:00", "duration": "1.5 小时", "coordinates": "120.14,30.24"},
        ],
        "meals": meals or [],
    }


def test_repair_moves_overlapping_meal():
    day = _day([{"type": "lunch", "title": "楼外楼", "start": "10:00", "evidence_id": "d1-p2"}])
    assert repair_meal_times(day) is True
    assert day["meals"][0]["start"] in ("12:00", "12:30")


def test_repair_keeps_valid_meal():
    day = _day([{"type": "lunch", "title": "楼外楼", "start": "12:30", "evidence_id": "d1-p2"}])
    assert repair_meal_times(day) is False


def test_pick_meals_from_restaurant_evidence():
    evidence = [
        {"id": "d1-p1", "status": "ok", "items": [{"name": "西湖", "coordinates": "120.15,30.25"}]},
        {"id": "d1-p2", "status": "ok", "items": [{"name": "蜀大侠火锅", "coordinates": "120.16,30.26"}]},
    ]
    meals = pick_meals(_day(), evidence)
    assert meals and meals[0]["title"] == "蜀大侠火锅" and meals[0]["evidence_id"] == "d1-p2"


def test_pick_meals_keeps_existing():
    existing = [{"type": "lunch", "title": "楼外楼", "start": "12:30", "evidence_id": "d1-p1"}]
    assert pick_meals(_day(existing), []) == existing
