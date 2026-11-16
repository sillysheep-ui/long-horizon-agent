"""执行轨迹（trace span）构造与派生指标：只记录事实，不做判定。"""

import json
import re
from typing import Any, Dict, List, Optional


def _span(name: str, kind: str, status: str, latency_ms: float = 0.0,
          attributes: Optional[Dict[str, Any]] = None,
          events: Optional[List[Dict[str, Any]]] = None,
          children: Optional[List[Dict[str, Any]]] = None,
          error_type: Optional[str] = None) -> Dict[str, Any]:
    """构造一个执行轨迹节点（trace span），只记录事实，不做判定。"""
    span: Dict[str, Any] = {"name": name, "kind": kind, "status": status,
                            "latency_ms": int(latency_ms), "attributes": attributes or {}}
    if events:
        span["events"] = events
    if children:
        span["children"] = children
    if error_type:
        span["error_type"] = error_type
    return span


def _tool_spans(dag_result: Dict[str, Any], plan: Any) -> List[Dict[str, Any]]:
    """把调度器的节点结果转成工具子 span。"""
    nodes = {node.id: node for node in plan.nodes}
    spans: List[Dict[str, Any]] = []
    for item in dag_result.get("results", []):
        node = nodes.get(str(item.get("node_id")))
        day = None
        if node is not None:
            matched = re.match(r"^d(\d+)-", node.id)
            day = int(matched.group(1)) if matched else None
        parsed: Any = None
        try:
            parsed = json.loads(item.get("output") or "")
        except (TypeError, ValueError):
            parsed = None
        evidence_status = parsed.get("status") if isinstance(parsed, dict) else None
        status, error_type = "ok", None
        if item.get("status") == "skipped":
            status, error_type = "blocked", "tool_error"
        elif item.get("status") != "success":
            status, error_type = "failed", "tool_error"
        elif evidence_status == "unavailable":
            status, error_type = "failed", "auth_missing"
        elif evidence_status == "failed":
            status, error_type = "failed", "tool_error"
        elif evidence_status == "empty":
            status, error_type = "warn", "empty_result"
        if isinstance(parsed, dict) and parsed.get("reused"):
            status = "degraded"
        attributes = {"day": day, "cache_hit": bool(item.get("cache_hit")),
                      "attempts": int(item.get("attempts") or 0),
                      "reused": bool(parsed.get("reused")) if isinstance(parsed, dict) else False}
        events = [{"name": error_type, "reason": str(item.get("error") or "")[:120]}] if error_type else None
        spans.append(_span(node.tool if node is not None else "tool", "tool", status,
                           float(item.get("latency_ms") or 0.0), attributes,
                           events=events, error_type=error_type))
    return spans


def _critical_path_ms(plan: Any, dag_result: Dict[str, Any]) -> float:
    """按依赖关系计算最长路径耗时。"""
    latency = {str(item.get("node_id")): float(item.get("latency_ms") or 0.0)
               for item in dag_result.get("results", [])}
    nodes = {node.id: node for node in plan.nodes}
    memo: Dict[str, float] = {}

    def longest(node_id: str) -> float:
        if node_id in memo:
            return memo[node_id]
        node = nodes.get(node_id)
        upstream = max((longest(dep) for dep in node.depends_on if dep in nodes), default=0.0) if node else 0.0
        memo[node_id] = latency.get(node_id, 0.0) + upstream
        return memo[node_id]

    return max((longest(node_id) for node_id in nodes), default=0.0)


def _fabricated_costs(days: List[Dict[str, Any]], evidence: List[Dict[str, Any]]) -> int:
    """活动费用里出现、但证据文本中没有的数字，算编造。"""
    corpus = " ".join(
        " ".join(str(entry.get(part) or "") for part in ("name", "snippet", "address"))
        for evidence_item in evidence for entry in evidence_item.get("items", [])
    )
    count = 0
    for day in days:
        for activity in day.get("activities", []):
            cost = str(activity.get("cost") or "")
            for number in re.findall(r"\d+(?:\.\d+)?", cost):
                if number not in corpus:
                    count += 1
                    break
    return count


def _time_order_ok(days: List[Dict[str, Any]]) -> bool:
    for day in days:
        starts = [str(activity.get("start") or "") for activity in day.get("activities", [])]
        if starts != sorted(starts):
            return False
    return True
