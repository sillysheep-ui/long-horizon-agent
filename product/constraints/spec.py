"""约束集的加载与校验。

约束集是数据，不是代码：新增一条约束只改 JSON，不改引擎。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from product.constraints.model import CATEGORIES, SEVERITIES, Constraint, ConstraintSet
from product.constraints.ops import OPERATORS

SET_DIR = Path(__file__).resolve().with_name("sets")
DEFAULT_SET = SET_DIR / "v1.json"


def validate(raw: dict[str, Any]) -> Constraint:
    """校验单条约束；任一字段不合法直接抛 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("约束必须是对象")
    constraint_id = str(raw.get("id") or "").strip()
    if not constraint_id:
        raise ValueError("约束缺少 id")
    category = str(raw.get("category") or "").strip()
    if category not in CATEGORIES:
        raise ValueError(f"约束 {constraint_id} 的 category 非法：{category}")
    severity = str(raw.get("severity") or "").strip()
    if severity not in SEVERITIES:
        raise ValueError(f"约束 {constraint_id} 的 severity 非法：{severity}")
    operator = str(raw.get("type") or "").strip()
    if operator not in OPERATORS:
        raise ValueError(f"约束 {constraint_id} 的算子未知：{operator}")
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError(f"约束 {constraint_id} 的 params 必须是对象")
    select = str(raw.get("select") or "").strip()
    return Constraint(
        id=constraint_id,
        category=category,
        severity=severity,
        type=operator,
        params=params,
        select=select,
    )


def load_set(path: str | Path | None = None) -> ConstraintSet:
    """从 JSON 文件加载约束集，并校验 id 唯一、算子已知。"""
    source = Path(path) if path else DEFAULT_SET
    payload = json.loads(source.read_text(encoding="utf-8"))
    version = str(payload.get("version") or "").strip()
    if not version:
        raise ValueError("约束集缺少 version")
    raw_items = payload.get("constraints")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("约束集 constraints 不能为空")
    constraints: list[Constraint] = []
    seen: set[str] = set()
    for raw in raw_items:
        constraint = validate(raw)
        if constraint.id in seen:
            raise ValueError(f"约束 id 重复：{constraint.id}")
        seen.add(constraint.id)
        constraints.append(constraint)
    return ConstraintSet(version=version, constraints=tuple(constraints))
