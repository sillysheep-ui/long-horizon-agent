"""统一报告信封与指标的最小校验。"""

from __future__ import annotations

import json

from evaluation.contract import CheckRecord, SuiteReport, dataset_version
from evaluation.metrics import bootstrap_ci, macro, micro


def _checks(*values):
    return [CheckRecord(id=f"c{index}", ok=value) for index, value in enumerate(values)]


def test_micro_ignores_unknown():
    assert micro(_checks(True, False, None)) == 0.5
    assert micro([]) is None


def test_macro_counts_whole_cases():
    assert macro([_checks(True, True), _checks(True, False)]) == 0.5


def test_bootstrap_is_deterministic_and_brackets_mean():
    values = [0.8, 0.9, 0.85, 0.95, 0.7]
    low, high = bootstrap_ci(values, seed=7)
    assert (low, high) == bootstrap_ci(values, seed=7)
    assert low <= sum(values) / len(values) <= high


def test_suite_report_roundtrip(tmp_path):
    report = SuiteReport(
        suite="single_turn", main_metric="score_mean", main_value=0.9,
        cases=1, dataset_version=dataset_version(__file__), checks=_checks(True),
        metrics={"checks_micro": 1.0},
    )
    target = tmp_path / "report.json"
    report.write(target)
    payload = json.loads(target.read_text())
    assert payload["suite"] == "single_turn"
    assert payload["schema_version"] == 1
    assert payload["checks"][0]["ok"] is True
