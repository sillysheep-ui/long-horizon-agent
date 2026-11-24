"""多次采样指标：pass@k 与 pass^k。"""

from evaluation.metrics import pass_metrics


def test_pass_metrics_two_trials():
    result = pass_metrics([[True, True], [False, True], [False, False]])
    assert result["trials"] == 2
    assert result["pass_at_1"] == 0.333
    assert result["pass_at_k"] == 0.667
    assert result["pass_hat_k"] == 0.333
    assert result["per_trial_pass"] == [0.333, 0.667]


def test_pass_metrics_single_trial():
    result = pass_metrics([[True], [False]])
    assert result["trials"] == 1
    assert result["pass_at_1"] == 0.5
    assert result["pass_at_k"] == 0.5
    assert result["pass_hat_k"] == 0.5


def test_pass_metrics_ragged_rows_use_shortest():
    result = pass_metrics([[True, True, True], [False, True]])
    assert result["trials"] == 2
    assert result["pass_hat_k"] == 0.5


def test_pass_metrics_empty():
    assert pass_metrics([]) == {}
