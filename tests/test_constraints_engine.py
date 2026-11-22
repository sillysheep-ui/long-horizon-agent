"""约束引擎与算子测试。"""

from __future__ import annotations

from product.constraints.engine import build_view, evaluate, failed_ids, passed, summary
from product.constraints.spec import load_set


def _activity(title: str, start: str, duration: str = "1 小时", **extra):
    return {"title": title, "start": start, "duration": duration, **extra}


def _view(days, constraints=None, evidence=None):
    return build_view(
        {"days": days, "evidence": evidence or [], "constraints": constraints or {}},
    )


def _finding(findings, constraint_id):
    for item in findings:
        if item.id == constraint_id:
            return item
    raise AssertionError(f"找不到约束 {constraint_id}")


def test_density_flags_day_with_one_activity():
    view = _view([{"day": 1, "activities": [_activity("西湖", "09:00")]}])
    result = _finding(evaluate(view, load_set()), "density")
    assert result.ok is False
    assert "第1天 1 个" in result.actual


def test_time_order_reports_out_of_order():
    view = _view(
        [
            {
                "day": 1,
                "activities": [
                    _activity("A", "10:00"),
                    _activity("B", "09:00"),
                ],
            }
        ]
    )
    result = _finding(evaluate(view, load_set()), "time_order")
    assert result.ok is False
    assert "09:00 早于 10:00" in result.actual


def test_no_overlap_detects_overlap():
    view = _view(
        [
            {
                "day": 1,
                "activities": [
                    _activity("A", "09:00", "2 小时"),
                    _activity("B", "10:00", "1 小时"),
                ],
            }
        ]
    )
    result = _finding(evaluate(view, load_set()), "no_overlap")
    assert result.ok is False
    assert "重叠" in result.actual


def test_time_window_uses_opening_hours():
    view = _view(
        [{"day": 1, "activities": [_activity("陶陶居", "09:30", "1.5 小时")]}],
        evidence=[{"id": "d1-p1", "items": [{"name": "陶陶居", "opentime": "10:00", "endtime": "22:00"}]}],
    )
    result = _finding(evaluate(view, load_set()), "opening_hours")
    assert result.ok is False
    assert "早于营业时间 10:00" in result.actual


def test_sum_le_uses_day_window():
    view = _view(
        [
            {
                "day": 1,
                "activities": [
                    _activity("A", "09:00", "2 小时"),
                    _activity("B", "12:00", "2 小时"),
                ],
            }
        ],
        constraints={"day_windows": [{"day": 1, "hours": 3}]},
    )
    result = _finding(evaluate(view, load_set()), "day_load")
    assert result.ok is False
    assert "4.0 小时" in result.actual


def test_exists_contains_reports_missing_named_place():
    view = _view(
        [{"day": 1, "activities": [_activity("灵隐寺", "09:00"), _activity("九溪", "13:00")]}],
        constraints={"named_places": ["西湖"]},
    )
    result = _finding(evaluate(view, load_set()), "named_places_covered")
    assert result.ok is False
    assert "西湖" in result.actual


def test_all_constraints_pass_on_clean_day():
    day = [
        _activity("西湖", "09:00", "2 小时"),
        _activity("灵隐寺", "13:00", "2 小时"),
    ]
    view = _view([{"day": 1, "activities": day}], constraints={"named_places": ["西湖", "灵隐寺"]})
    findings = evaluate(view, load_set())
    assert passed(findings) is True
    assert failed_ids(findings) == []
    counts = summary(findings)
    assert counts["total"] >= 6
    assert counts["hard_passed"] == counts["hard_total"]
