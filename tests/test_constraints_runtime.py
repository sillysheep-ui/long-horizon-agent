"""运行时排期检查复用约束集。"""

from product.constraints.runtime import RUNTIME_SET, check_plan


def test_runtime_set_is_declarative():
    assert RUNTIME_SET.version
    ids = {constraint.id for constraint in RUNTIME_SET.constraints}
    assert {"too_many", "order", "overlap", "over_hours", "bad_time"} <= ids


def test_violation_carries_day_and_detail():
    days = [{"day": 2, "activities": [
        {"title": "A", "start": "09:00", "duration": "2 小时"},
        {"title": "B", "start": "10:00", "duration": "1 小时"},
        {"title": "C", "start": "12:00", "duration": "1 小时"},
        {"title": "D", "start": "14:00", "duration": "1 小时"},
        {"title": "E", "start": "16:00", "duration": "1 小时"},
    ]}]
    result = check_plan(days)
    kinds = {item["kind"] for item in result["violations"]}
    assert "too_many" in kinds
    assert all(item["day"] == 2 for item in result["violations"] if item["kind"] == "too_many")


def test_empty_plan_is_flagged():
    result = check_plan([])
    assert result["ok"] is False
    assert result["violations"][0]["kind"] == "empty"
