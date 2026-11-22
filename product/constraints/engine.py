"""约束引擎：把声明式约束求值成 Finding 列表。"""

from __future__ import annotations

from typing import Any

from product.constraints.model import ConstraintSet, Finding, ItineraryView
from product.constraints.ops import OPERATORS


def build_view(result: dict[str, Any], case: dict[str, Any] | None = None) -> ItineraryView:
    """从一次行程结果构造求值所需的视图。"""
    result = result or {}
    return ItineraryView(
        days=[day for day in result.get("days") or [] if isinstance(day, dict)],
        evidence=[item for item in result.get("evidence") or [] if isinstance(item, dict)],
        constraints=result.get("constraints") or {},
        case=case or {},
        status=str(result.get("status") or ""),
    )


def evaluate(view: ItineraryView, constraint_set: ConstraintSet) -> list[Finding]:
    """按约束集顺序求值；未知算子直接报错，不静默跳过。"""
    findings: list[Finding] = []
    for constraint in constraint_set.constraints:
        operator = OPERATORS.get(constraint.type)
        if operator is None:
            raise KeyError(f"未知约束算子：{constraint.type}（约束 {constraint.id}）")
        finding = operator(view, constraint)
        if finding.meta.get("skip"):
            continue
        findings.append(finding)
    return findings


def passed(findings: list[Finding]) -> bool:
    """全部约束通过才算通过（软约束也只在这里汇总，严重度由调用方决定）。"""
    return all(item.ok for item in findings)


def failed_ids(findings: list[Finding]) -> list[str]:
    return [item.id for item in findings if not item.ok]


def summary(findings: list[Finding]) -> dict[str, Any]:
    """报告用的 micro / macro 两口径。"""
    hard = [item for item in findings if item.severity == "hard"]
    return {
        "total": len(findings),
        "passed": sum(1 for item in findings if item.ok),
        "hard_total": len(hard),
        "hard_passed": sum(1 for item in hard if item.ok),
        "failed": failed_ids(findings),
    }
