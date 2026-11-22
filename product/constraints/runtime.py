"""运行时排期检查：复用评测同一套约束算子，只保留给模型的反馈格式。"""

from __future__ import annotations

from typing import Any

from product.constraints.engine import build_view, evaluate
from product.constraints.spec import SET_DIR, load_set

RUNTIME_SET = load_set(SET_DIR / "runtime.json")

_KINDS = {
    "too_many": "too_many",
    "order": "order",
    "overlap": "overlap",
    "over_hours": "over_hours",
    "bad_time": "bad_time",
}


def check_plan(days: Any, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回 {ok, violations}；violations 为 {day, kind, detail}，供重排提示使用。"""
    constraints = constraints or {}
    if not isinstance(days, list) or not days:
        return {
            "ok": False,
            "violations": [{"day": None, "kind": "empty", "detail": "没有输出任何一天的安排"}],
        }
    violations: list[dict[str, Any]] = []
    expected = constraints.get("days")
    if isinstance(expected, int) and expected > 0 and len(days) != expected:
        violations.append({
            "day": None,
            "kind": "day_count",
            "detail": f"应该是 {expected} 天，实际输出了 {len(days)} 天",
        })
    for day in days:
        if not isinstance(day, dict):
            continue
        view = build_view({"days": [day], "constraints": constraints, "status": "complete"})
        for finding in evaluate(view, RUNTIME_SET):
            if finding.ok:
                continue
            detail = finding.detail
            if finding.actual:
                detail = f"{detail}：{finding.actual}"
            violations.append({
                "day": day.get("day"),
                "kind": _KINDS.get(finding.id, finding.id),
                "detail": detail,
            })
    return {"ok": not violations, "violations": violations}
