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


# 检查项的分类：static 只依赖行程本身；snapshot 需要快照里的可选字段；
# case 是“这个用例本来就不适用”（没锚定、没点名），不算缺数据。
CHECK_CLASSES: dict[str, str] = {
    "complete": "static",
    "day_count": "static",
    "density": "static",
    "time_order": "static",
    "no_overlap": "static",
    "day_load": "static",
    "unique_titles": "static",
    "title_clean": "static",
    "city_coverage": "static",
    "evidence_ok": "static",
    "periods": "static",
    "activity_length": "static",
    "free_time": "static",
    "fields_filled": "static",
    "title_matches_evidence": "static",
    "no_fabricated_cost": "snapshot",
    "opening_hours": "snapshot",
    "meal_grounded": "case",
    "anchor_satisfied": "case",
    "named_places_covered": "case",
    "clarification": "case",
}

# 快照类检查依赖的字段，缺这些字段时检查会记为 unknown 而不是通过。
CHECK_SNAPSHOT_NEEDS: dict[str, tuple[str, ...]] = {
    "no_fabricated_cost": ("price",),
    "opening_hours": ("opentime",),
}


def check_details(case: dict[str, Any], result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """逐项检查明细：判了 / 未知（缺快照字段）/ 不适用（用例本来不涉及）。"""
    if case.get("expect") == "needs_clarification":
        ok = result.get("status") == "needs_clarification"
        return {"clarification": {"class": "case", "status": "judged", "ok": ok, "detail": ""}}
    findings = evaluate(build_view(result, case), CONSTRAINT_SET, include_skipped=True)
    details: dict[str, dict[str, Any]] = {}
    for finding in findings:
        cls = CHECK_CLASSES.get(finding.id, "static")
        skipped = bool(finding.meta.get("skip"))
        if skipped and cls == "case":
            status = "not_applicable"
        elif skipped:
            status = "unknown"
        else:
            status = "judged"
        details[finding.id] = {
            "class": cls,
            "status": status,
            "ok": finding.ok if status == "judged" else None,
            "detail": finding.detail,
        }
    return details


GATE_CHECKS = frozenset({"complete", "day_count", "evidence_ok", "unique_titles"})


def signal_checks(checks: dict[str, bool]) -> dict[str, bool]:
    """去掉门槛检查后剩下的信号检查。"""
    return {name: value for name, value in checks.items() if name not in GATE_CHECKS}


def signal_pass_rate(checks: dict[str, bool]) -> float | None:
    """信号检查的通过比例；没有信号检查时返回 None。"""
    values = list(signal_checks(checks).values())
    if not values:
        return None
    return sum(1 for value in values if value) / len(values)


def combined_score_v2(checks: dict[str, bool], rubric: Any, fabrication: bool = False) -> Any:
    """信号分：门槛只做封顶，分数由信号检查与 rubric 决定。"""
    if rubric is None:
        return None
    rate = signal_pass_rate(checks)
    if rate is None:
        return None
    score = 0.6 * rate + 0.4 * float(rubric) - (0.2 if fabrication else 0.0)
    if not all(checks.get(gate, True) for gate in GATE_CHECKS):
        score = min(score, 0.2)
    return round(max(0.0, min(1.0, score)), 3)


def combined_score(checks: dict[str, bool], rubric: Any, fabrication: bool = False) -> Any:
    """0–1 综合分：硬约束 60% + rubric 40%，编造再扣 0.2。"""
    if rubric is None:
        return None
    score = 0.6 * hard_pass_rate(checks) + 0.4 * float(rubric) - (0.2 if fabrication else 0.0)
    return round(max(0.0, min(1.0, score)), 3)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐条记录汇总成通过率、平均分与延迟。"""
    details = [record.get("check_details") or {} for record in records]
    check_names = sorted({name for detail in details for name in detail}
                         | {name for record in records for name in record["checks"]})
    passed = {name: sum(1 for detail in details if (detail.get(name) or {}).get("ok"))
              for name in check_names}
    examined = {name: sum(1 for detail in details
                          if (detail.get(name) or {}).get("status") == "judged")
                for name in check_names}
    unknown = {name: sum(1 for detail in details
                         if (detail.get(name) or {}).get("status") == "unknown")
               for name in check_names}
    applicable = {name: sum(1 for record in records if name in record["checks"])
                  for name in check_names}
    if not any(details):
        # 没有明细的旧记录：退回“出现在 checks 里就算判了”。
        examined = dict(applicable)
        passed = {name: sum(1 for record in records if record["checks"].get(name))
                  for name in check_names}
    total = len(records)
    judged_cells = sum(1 for detail in details for item in detail.values()
                       if item["status"] == "judged" and item["class"] in ("static", "snapshot"))
    unknown_cells = sum(1 for detail in details for item in detail.values()
                        if item["status"] == "unknown")
    rubrics = [(r.get("judge") or {}).get("rubric") for r in records]
    rubrics = [float(value) for value in rubrics if value is not None]
    scores = [r["score"] for r in records if r.get("score") is not None]
    deliverable = sum(1 for r in records
                      if r.get("score") is not None
                      and all(r["checks"].values())
                      and (r.get("judge") or {}).get("rubric", 0) >= 0.7
                      and not (r.get("judge") or {}).get("fabrication")) if scores else None
    micro = (sum(sum(1 for value in r["checks"].values() if value) / max(1, len(r["checks"]))
                 for r in records) / total) if total else None
    return {
        "cases": total,
        "check_pass_rate": {name: (round(passed[name] / examined[name], 3) if examined[name] else None)
                            for name in check_names},
        "check_examined": examined,
        "check_unknown": unknown,
        "check_class": {name: CHECK_CLASSES.get(name, "static") for name in check_names},
        "static_coverage": (round(judged_cells / (judged_cells + unknown_cells), 3)
                            if judged_cells + unknown_cells else None),
        "static_unknown": unknown_cells,
        "checks_micro": round(micro, 3) if micro is not None else None,
        "checks_macro": round(sum(1 for r in records if all(r["checks"].values())) / total, 3) if total else None,
        "all_checks_passed": sum(1 for r in records if all(r["checks"].values())),
        "hard_mean": round(sum(hard_pass_rate(r["checks"]) for r in records) / total, 3) if total else None,
        "rubric_mean": round(sum(rubrics) / len(rubrics), 3) if rubrics else None,
        "score_mean": round(sum(scores) / len(scores), 3) if scores else None,
        "deliverable": deliverable,
        "signal_hard_mean": round(sum(signal_pass_rate(r["checks"]) or 0 for r in records) / total, 3) if total else None,
        "gates_passed": sum(1 for r in records if all(r["checks"].get(g, True) for g in GATE_CHECKS)),
        "score_signal_mean": round(sum(r["score_signal"] for r in records if r.get("score_signal") is not None)
                                   / max(1, sum(1 for r in records if r.get("score_signal") is not None)), 3)
        if any(r.get("score_signal") is not None for r in records) else None,
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
