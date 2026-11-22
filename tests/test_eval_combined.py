"""联合报告：排序、区间展示与“不可比”声明。"""

from __future__ import annotations

from evaluation.combined import markdown, rows


def _report(suite, value, main="score_mean", interval=None):
    metrics = {"score_ci95": interval} if interval else {}
    return {"suite": suite, "cases": 3, "main_metric": main, "main_value": value,
            "dataset_version": "abc12345", "evaluator_version": "v1",
            "metrics": metrics, "labels": {"comparable": False}}


def test_rows_follow_fixed_order():
    table = rows([_report("intent", 1.0, "final_accuracy"),
                  _report("single_turn", 0.9, interval=[0.88, 0.92])])
    assert [row["suite"] for row in table] == ["single_turn", "intent"]


def test_markdown_shows_interval_and_disclaimer():
    text = markdown(rows([_report("single_turn", 0.9, interval=[0.88, 0.92])]))
    assert "[0.88, 0.92]" in text
    assert "不可横向比较" in text
    assert "| single_turn | 3 |" in text
