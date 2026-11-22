"""证据来源的统一标记。

每一条查询结果都必须能回答四个问题：来自哪里、什么时候查的、成功还是失败、
是真实数据还是模拟数据。这里集中定义字段和默认值，避免各个工具各写一套。
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

STATUSES = ("ok", "empty", "failed", "unavailable")
SOURCE_KINDS = ("map_poi", "sandbox", "web_search", "simulated", "none")
VERIFICATION_LEVELS = ("address_verified", "source_linked", "unverified", "none")

# 来源类型到默认核实级别的映射：只有地图 POI 才带地址与坐标。
_DEFAULT_LEVELS = {"map_poi": "address_verified", "sandbox": "source_linked",
                   "web_search": "source_linked"}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def build_evidence(
    evidence_id: str,
    kind: str,
    query: str,
    city: str,
    status: str,
    *,
    items: list[dict[str, Any]] | None = None,
    source: str = "",
    source_kind: str = "none",
    simulated: bool = False,
    verification_level: str = "none",
    reason: str = "",
    fetched_at: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造一条字段齐全的证据；非法状态直接报错，避免静默写坏数据。"""
    if status not in STATUSES:
        raise ValueError("未知的证据状态: " + str(status))
    if source_kind not in SOURCE_KINDS:
        raise ValueError("未知的来源类型: " + str(source_kind))
    if verification_level not in VERIFICATION_LEVELS:
        raise ValueError("未知的核实级别: " + str(verification_level))
    if simulated:
        verification_level = "unverified"
    elif verification_level == "none" and status == "ok":
        verification_level = _DEFAULT_LEVELS.get(source_kind, "unverified")
    if status != "ok" and verification_level not in ("none", "unverified"):
        # 没有成功结果时不存在“已核实”的地址或来源。
        verification_level = "unverified" if verification_level == "source_linked" else "none"
    evidence = {
        "id": evidence_id,
        "kind": kind,
        "query": query,
        "city": city,
        "status": status,
        "source": source,
        "source_kind": source_kind,
        "verification_level": verification_level,
        "simulated": bool(simulated),
        "fetched_at": fetched_at or utc_now(),
        "reason": reason,
        "items": [item for item in items or [] if isinstance(item, dict)],
        "reused": bool(extra.pop("reused", False)),
    }
    evidence.update(extra)
    return evidence


def normalize_evidence(item: dict[str, Any]) -> dict[str, Any]:
    """补齐历史数据或测试桩里缺失的字段，已有值保持不变。"""
    evidence = dict(item)
    kind = str(evidence.get("kind") or "")
    evidence.setdefault("source", "")
    evidence.setdefault("source_kind", "map_poi" if kind == "poi" else "web_search" if kind == "web" else "none")
    evidence.setdefault("simulated", False)
    evidence["simulated"] = bool(evidence["simulated"])
    evidence.setdefault("fetched_at", "")
    evidence["reused"] = bool(evidence.get("reused"))
    evidence.setdefault("reason", "")
    level = evidence.get("verification_level")
    if not level or level not in VERIFICATION_LEVELS:
        status = evidence.get("status")
        level = _DEFAULT_LEVELS.get(evidence["source_kind"], "unverified") if status == "ok" else "none"
        if evidence["simulated"]:
            level = "unverified"
    evidence["verification_level"] = level
    if not isinstance(evidence.get("items"), list):
        evidence["items"] = []
    return evidence


def normalize_all(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_evidence(item) for item in items]
