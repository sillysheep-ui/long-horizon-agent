from product.postprocess import resolve_meal_overlaps


def day_with(activities, meals):
    return {"day": 1, "city": "杭州", "activities": activities, "meals": meals}


def activity(start, duration):
    return {"title": "景点", "start": start, "duration": duration, "evidence_id": "d1-p1"}


def meal(start, kind="lunch", duration="1 小时"):
    return {"type": kind, "title": "餐厅", "start": start, "duration": duration,
            "evidence_id": "d1-p2"}


def test_moves_lunch_out_of_activity():
    days = [day_with([activity("11:30", "2 小时")], [meal("12:30")])]
    assert resolve_meal_overlaps(days) == 1
    assert days[0]["meals"][0]["start"] == "13:30"


def test_moves_dinner_after_late_activity():
    days = [day_with([activity("18:30", "1.5 小时")], [meal("18:30", kind="dinner")])]
    assert resolve_meal_overlaps(days) == 1
    assert days[0]["meals"][0]["start"] == "20:00"


def test_drops_meal_when_day_is_full():
    days = [day_with([activity("09:00", "12 小时")], [meal("12:30")])]
    assert resolve_meal_overlaps(days) == 1
    assert "meals" not in days[0]


def test_keeps_meal_that_does_not_overlap():
    days = [day_with([activity("14:00", "2 小时")], [meal("12:30")])]
    assert resolve_meal_overlaps(days) == 0
    assert days[0]["meals"][0]["start"] == "12:30"


def test_two_meals_do_not_land_on_same_slot():
    days = [day_with([activity("11:30", "3 小时")],
                     [meal("12:30"), meal("13:00", kind="dinner")])]
    resolve_meal_overlaps(days)
    starts = [item["start"] for item in days[0]["meals"]]
    assert len(starts) == len(set(starts))
    assert all(start >= "14:30" for start in starts)
