"""信号分：门槛检查只封顶，不参与计分。"""

from evaluation.product_checks import combined_score_v2, signal_pass_rate


def _checks(**extra):
    base = {"complete": True, "day_count": True, "evidence_ok": True, "unique_titles": True}
    base.update(extra)
    return base


def test_gates_only_returns_none():
    assert signal_pass_rate(_checks()) is None
    assert combined_score_v2(_checks(), 0.9) is None


def test_signal_rate_ignores_gates():
    checks = _checks(density=False, free_time=True)
    assert signal_pass_rate(checks) == 0.5


def test_gate_failure_caps_score():
    checks = _checks(complete=False, density=True, free_time=True)
    assert combined_score_v2(checks, 1.0) == 0.2


def test_score_combines_signal_and_rubric():
    checks = _checks(density=True, free_time=False)
    assert combined_score_v2(checks, 0.5) == 0.5
