"""多轮修改检查的单元测试。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.revision_checks import check_revision, summarize_revision


def make_result(day_titles, applied=None, evidence_status="ok"):
    days = []
    evidence = []
    for index, titles in enumerate(day_titles, 1):
        activities = []
        for position, title in enumerate(titles, 1):
            evidence_id = f"d{index}-p{position}"
            activities.append({"title": title, "start": f"{8 + position:02d}:00", "evidence_id": evidence_id})
            evidence.append({"id": evidence_id, "status": evidence_status})
        days.append({"day": index, "activities": activities})
    result = {"status": "complete", "days": days, "evidence": evidence}
    if applied is not None:
        result["revision_applied"] = applied
    return result


def test_targeted_revision_passes_when_only_target_day_changes():
    case = {"target_days": [2], "expect": "targeted"}
    before = make_result([["西湖", "灵隐寺"], ["九溪", "龙井村"]])
    after = make_result([["西湖", "灵隐寺"], ["云栖竹径", "梅家坞"]], applied=True)
    checks = check_revision(case, before, after)
    assert all(value for value in checks.values())


def test_targeted_revision_fails_when_target_day_unchanged():
    case = {"target_days": [2], "expect": "targeted"}
    before = make_result([["西湖", "灵隐寺"], ["九溪", "龙井村"]])
    after = make_result([["西湖", "灵隐寺"], ["九溪", "龙井村"]], applied=False)
    checks = check_revision(case, before, after)
    assert checks["target_changed"] is False
    assert checks["applied"] is False


def test_kept_day_must_not_change():
    case = {"target_days": [2], "expect": "targeted"}
    before = make_result([["西湖", "灵隐寺"], ["九溪"]])
    after = make_result([["西湖", "雷峰塔"], ["云栖竹径"]], applied=True)
    checks = check_revision(case, before, after)
    assert checks["kept_days"] is False
    assert checks["target_changed"] is True


def test_full_replan_should_not_be_marked_as_targeted():
    case = {"target_days": [], "expect": "full"}
    before = make_result([["西湖"], ["九溪"]])
    ok = check_revision(case, before, make_result([["西湖"], ["云栖竹径"]], applied=None))
    wrong = check_revision(case, before, make_result([["西湖"], ["云栖竹径"]], applied=True))
    assert ok["applied"] is True
    assert wrong["applied"] is False


def test_failed_evidence_breaks_check():
    case = {"target_days": [1], "expect": "targeted"}
    before = make_result([["西湖"]])
    after = make_result([["灵隐寺"]], applied=True, evidence_status="failed")
    checks = check_revision(case, before, after)
    assert checks["evidence_ok"] is False


def test_summary_counts_applicable_cases():
    records = [
        {"id": "a", "checks": {"applied": True, "target_changed": True}, "changed_days": [2]},
        {"id": "b", "checks": {"applied": False, "target_changed": False}, "changed_days": []},
        {"id": "c", "checks": {}, "error": "boom"},
    ]
    summary = summarize_revision(records)
    assert summary["cases"] == 3
    assert summary["all_passed"] == 1
    assert summary["all_passed_rate"] == 0.5
    assert summary["pass_rate"]["applied"] == 0.5
    assert summary["targeted_with_change"] == "1/2"


def test_clarify_expectation_only_checks_clarification():
    case = {"target_days": [], "expect": "clarify"}
    before = make_result([["西湖"], ["九溪"]])
    after = {"status": "needs_clarification", "question": "现在的行程是 2 天，第 5 天超出了范围", "days": []}
    checks = check_revision(case, before, after)
    assert checks == {"clarified": True, "applied": True, "days_empty": True}
