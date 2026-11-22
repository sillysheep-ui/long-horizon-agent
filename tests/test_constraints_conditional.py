"""条件键：数据不足时约束不出现，而不是判失败。"""

from __future__ import annotations

from evaluation.product_checks import check_result, engine_checks

DAY = {
    "day": 1,
    "city": "杭州",
    "activities": [
        {"title": "西湖", "start": "09:30", "duration": "2 小时", "transport": "步行", "cost": "免费",
         "evidence_id": "d1-p1"},
    ],
    "meals": [],
}

RESULT = {
    "status": "complete",
    "days": [DAY],
    "constraints": {"days": 1},
    "evidence": [{"id": "d1-p1", "status": "ok", "items": [{"name": "西湖"}]}],
}


def test_conditional_checks_absent_when_data_missing():
    checks = engine_checks({"query": "杭州一天"}, RESULT)
    assert "opening_hours" not in checks
    assert "meal_grounded" not in checks
    assert "anchor_satisfied" not in checks
    assert "named_places_covered" not in checks
    assert checks["fields_filled"] is True


def test_opening_hours_present_when_times_known():
    result = {
        **RESULT,
        "evidence": [{"id": "d1-p1", "status": "ok",
                      "items": [{"name": "西湖", "opentime": "10:00", "endtime": "18:00"}]}],
    }
    checks = engine_checks({"query": "杭州一天"}, result)
    assert checks["opening_hours"] is False  # 09:30 早于 10:00 开门


def test_anchor_and_named_places_appear_with_case():
    result = {
        **RESULT,
        "days": [{**DAY, "meals": [{"title": "西湖", "start": "12:30", "evidence_id": "d1-p1"}]}],
    }
    case = {"query": "杭州一天", "anchor": "food", "named_places": ["西湖", "灵隐寺"]}
    checks = engine_checks(case, result)
    assert checks["meal_grounded"] is True
    assert checks["anchor_satisfied"] is True
    assert checks["named_places_covered"] is False


def test_check_result_keeps_same_keys_as_legacy():
    case = {"query": "杭州一天", "anchor": "stay", "named_places": ["西湖"]}
    checks = check_result(case, RESULT)
    assert "anchor_satisfied" in checks
    assert "named_places_covered" in checks
    assert "opening_hours" not in checks
