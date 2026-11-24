"""配对对比工具：对齐、区间与改善/回退计数。"""


from evaluation.compare import paired_rows, summarize_pairs


def _row(case_id, score, hard=True):
    return {"id": case_id, "score": score, "checks": {"complete": hard}}


def test_paired_rows_aligns_by_id_and_skips_missing_scores():
    a = [_row("a", 0.8), _row("b", 0.9), _row("c", None)]
    b = [_row("a", 0.7), _row("b", 0.95), _row("c", 0.5)]
    pairs = paired_rows(a, b, "score")
    assert [key for key, _, _ in pairs] == ["a", "b"]


def test_summarize_counts_improved_and_regressed():
    pairs = [("a", 0.8, 0.7), ("b", 0.9, 0.95), ("c", 1.0, 1.0)]
    summary = summarize_pairs(pairs, "score")
    assert summary["cases"] == 3
    assert summary["improved"] == 1
    assert summary["regressed"] == 1
    assert summary["same"] == 1
    assert summary["ci_low"] <= summary["mean_diff"] <= summary["ci_high"]


def test_hard_pass_metric_detects_flip():
    a = [{"id": "a", "checks": {"x": True}}]
    b = [{"id": "a", "checks": {"x": False}}]
    pairs = paired_rows(a, b, "hard_pass")
    assert pairs == [("a", True, False)]
