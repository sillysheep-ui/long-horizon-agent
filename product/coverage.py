"""按天检查证据覆盖度，并为没有来源的日期生成一次补查计划。

核验只做“有没有可追溯来源”这一件确定的事：某个日期没有成功证据，就补查一次；
补查仍为空就如实标注未核实，不编造内容。
"""

import re
from typing import Any

from inference.tool_dag import ToolDAGError, ToolPlan

REPAIR_LIMIT = 4
DAY_ID = re.compile(r"^d(\d+)-")


def _day_of(evidence: dict[str, Any]) -> int | None:
    matched = DAY_ID.match(str(evidence.get("id") or ""))
    return int(matched.group(1)) if matched else None


def assess_days(days: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """返回每天的来源覆盖情况与风险分，风险分只反映可证据化的问题。"""
    buckets: dict[int, dict[str, int]] = {
        day["day"]: {"ok": 0, "failed": 0, "empty": 0} for day in days if isinstance(day, dict)
    }
    for item in evidence:
        if not isinstance(item, dict):
            continue
        day_number = _day_of(item)
        if day_number not in buckets:
            continue
        status = item.get("status")
        if status == "ok" and item.get("items"):
            buckets[day_number]["ok"] += 1
        elif status == "failed":
            buckets[day_number]["failed"] += 1
        elif status == "empty":
            buckets[day_number]["empty"] += 1

    risk = 0.0
    reasons: list[str] = []
    uncovered_days: list[int] = []
    summary: list[dict[str, Any]] = []
    for day in days:
        if not isinstance(day, dict):
            continue
        bucket = buckets.get(day["day"], {"ok": 0, "failed": 0, "empty": 0})
        uncovered = bucket["ok"] == 0
        if uncovered:
            uncovered_days.append(day["day"])
            risk += 0.35
            reasons.append("day_uncovered")
        if bucket["failed"]:
            risk += min(0.3, 0.1 * bucket["failed"])
            reasons.append("tool_failure")
        summary.append({
            "day": day["day"],
            "city": day.get("city"),
            "covered": bucket["ok"],
            "failed": bucket["failed"],
            "empty": bucket["empty"],
            "uncovered": uncovered,
        })
    return {
        "risk": round(min(1.0, risk), 4),
        "reasons": list(dict.fromkeys(reasons)),
        "uncovered_days": uncovered_days,
        "days": summary,
    }


def build_repair_plan(
    days: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    *,
    limit: int = REPAIR_LIMIT,
    max_nodes: int = 18,
    node_timeout_seconds: float = 45.0,
) -> ToolPlan | None:
    """为未覆盖的日期生成“城市 + 景点”补查计划；没有缺口时返回 None。"""
    assessment = assess_days(days, evidence)
    missing = set(assessment["uncovered_days"])
    if not missing:
        return None
    done = {
        (item.get("city"), item.get("query"))
        for item in evidence
        if isinstance(item, dict) and item.get("status") == "ok"
    }
    nodes: list[dict[str, Any]] = []
    for day in days:
        if not isinstance(day, dict) or day["day"] not in missing:
            continue
        city = str(day.get("city") or "").strip()
        if not city:
            continue
        keyword = city + " 景点"
        if (city, keyword) in done:
            continue
        nodes.append({
            "id": "d%d-r1" % day["day"],
            "tool": "poi",
            "arguments": {"city": city, "keyword": keyword},
            "timeout_seconds": node_timeout_seconds,
        })
        if len(nodes) >= limit:
            break
    if not nodes:
        return None
    try:
        return ToolPlan.from_dict(
            {"goal": "补查没有可追溯来源的日期", "nodes": nodes},
            allowed_tools={"poi"},
            max_nodes=min(max_nodes, limit),
        )
    except ToolDAGError:
        return None
