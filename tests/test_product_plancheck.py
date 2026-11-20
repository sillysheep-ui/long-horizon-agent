"""模型排期的硬伤检查。"""

from product.plancheck import check_day_plan


def _plan(*days):
    return list(days)


def test_valid_plan_passes():
    days = _plan({"day": 1, "activities": [
        {"title": "A", "start": "09:30", "duration": "2 小时"},
        {"title": "B", "start": "13:30", "duration": "2 小时"},
    ]})
    assert check_day_plan(days, {"days": 1})["ok"] is True


def test_overlap_is_flagged():
    days = _plan({"day": 1, "activities": [
        {"title": "A", "start": "09:30", "duration": "2 小时"},
        {"title": "B", "start": "10:30", "duration": "1 小时"},
    ]})
    result = check_day_plan(days)
    assert result["ok"] is False
    assert any(item["kind"] == "overlap" for item in result["violations"])


def test_over_hours_is_flagged():
    days = _plan({"day": 1, "activities": [
        {"title": "A", "start": "09:00", "duration": "5 小时"},
        {"title": "B", "start": "14:30", "duration": "5 小时"},
    ]})
    result = check_day_plan(days, {"day_windows": [{"day": 1, "hours": 8}]})
    assert any(item["kind"] == "over_hours" for item in result["violations"])


def test_day_count_and_too_many():
    days = _plan({"day": 1, "activities": [
        {"title": str(index), "start": "%02d:00" % (9 + index), "duration": "1 小时"}
        for index in range(5)
    ]})
    result = check_day_plan(days, {"days": 2})
    kinds = {item["kind"] for item in result["violations"]}
    assert {"day_count", "too_many"} <= kinds
