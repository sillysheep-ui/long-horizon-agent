"""产品行程的确定性检查：不依赖裁判模型，先卡结构与可追溯性。

规则定义在 `product/constraints/sets/v1.json`，这里只负责把结果转成报告用的字典。
这一层对应 TravelPlanner（arXiv:2402.01622）里的 constraint pass rate：
能程序化验证的约束就不交给模型判，既省钱，也能直接当 RL 的 dense reward。
"""

from typing import Any

from product.constraints.engine import build_view, evaluate
from product.constraints.spec import load_set


def hard_pass_rate(checks: dict[str, bool]) -> float:
    """确定性检查的通过比例，供打分与 RL 奖励使用。"""
    if not checks:
        return 0.0
    return sum(1 for value in checks.values() if value) / len(checks)


CONSTRAINT_SET = load_set()


def engine_checks(case: dict[str, Any], result: dict[str, Any]) -> dict[str, bool]:
    """按约束集求解，返回逐项通过与否。"""
    findings = evaluate(build_view(result, case), CONSTRAINT_SET)
    return {finding.id: finding.ok for finding in findings}


def check_result(case: dict[str, Any], result: dict[str, Any]) -> dict[str, bool]:
    """返回逐项检查结果；追问类用例只看是否真的追问了。"""
    if case.get("expect") == "needs_clarification":
        return {"clarification": result.get("status") == "needs_clarification"}
    return engine_checks(case, result)


def combined_score(checks: dict[str, bool], rubric: Any, fabrication: bool = False) -> Any:
    """0–1 综合分：硬约束 60% + rubric 40%，编造再扣 0.2。"""
    if rubric is None:
        return None
    score = 0.6 * hard_pass_rate(checks) + 0.4 * float(rubric) - (0.2 if fabrication else 0.0)
    return round(max(0.0, min(1.0, score)), 3)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐条记录汇总成通过率、平均分与延迟。"""
    check_names = sorted({name for r in records for name in r["checks"]})
    passed = {name: sum(1 for r in records if r["checks"].get(name)) for name in check_names}
    applicable = {name: sum(1 for r in records if name in r["checks"]) for name in check_names}
    total = len(records)
    rubrics = [(r.get("judge") or {}).get("rubric") for r in records]
    rubrics = [float(value) for value in rubrics if value is not None]
    scores = [r["score"] for r in records if r.get("score") is not None]
    deliverable = sum(1 for r in records
                      if r.get("score") is not None
                      and all(r["checks"].values())
                      and (r.get("judge") or {}).get("rubric", 0) >= 0.7
                      and not (r.get("judge") or {}).get("fabrication"))
    micro = (sum(sum(1 for value in r["checks"].values() if value) / max(1, len(r["checks"]))
                 for r in records) / total) if total else None
    return {
        "cases": total,
        "check_pass_rate": {name: round(passed[name] / applicable[name], 3) for name in check_names},
        "checks_micro": round(micro, 3) if micro is not None else None,
        "checks_macro": round(sum(1 for r in records if all(r["checks"].values())) / total, 3) if total else None,
        "all_checks_passed": sum(1 for r in records if all(r["checks"].values())),
        "hard_mean": round(sum(hard_pass_rate(r["checks"]) for r in records) / total, 3) if total else None,
        "rubric_mean": round(sum(rubrics) / len(rubrics), 3) if rubrics else None,
        "score_mean": round(sum(scores) / len(scores), 3) if scores else None,
        "deliverable": deliverable,
        "latency_mean": round(sum(r["elapsed"] for r in records) / total, 2) if total else None,
        "stage_latency_mean": _stage_latency_mean(records),
    }


def _stage_latency_mean(records: list[dict[str, Any]]) -> dict[str, float]:
    """按阶段汇总平均耗时，便于对比两次回归是哪一段变慢。"""
    buckets: dict[str, list[float]] = {}
    for record in records:
        latency = (record.get("trajectory") or {}).get("latency_ms") or {}
        for name, value in latency.items():
            buckets.setdefault(name, []).append(float(value or 0) / 1000)
    return {name: round(sum(values) / len(values), 2) for name, values in buckets.items() if values}
