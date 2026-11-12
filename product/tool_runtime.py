"""把 ResearchTools 适配成 ToolDAG 调度器的执行器，并把每个节点的结果回收成证据。"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from inference.tool_dag import ToolPlan
from product.provenance import build_evidence

REUSE_SECONDS = 86400


def _fresh(fetched_at: str, now: Optional[datetime] = None) -> bool:
    if not fetched_at:
        return False
    try:
        age = (now or datetime.now(timezone.utc)) - datetime.fromisoformat(fetched_at)
    except (TypeError, ValueError):
        return False
    return age.total_seconds() < REUSE_SECONDS


class ResearchExecutor:
    """ToolExecutor 的实现：节点参数 -> 地点查询 -> 完整证据 JSON。"""

    def __init__(self, tools: Any, previous_evidence: Optional[List[Dict[str, Any]]] = None):
        self.tools = tools
        self.previous = {
            (item.get("city"), item.get("query")): item
            for item in (previous_evidence or [])
            if isinstance(item, dict) and item.get("status") == "ok"
        }

    async def __call__(self, tool_name: str, arguments: Dict[str, Any], node_id: str) -> str:
        if tool_name != "poi":
            raise ValueError("未知的查询工具: " + str(tool_name))
        city = str(arguments.get("city") or "").strip()
        keyword = str(arguments.get("keyword") or "").strip()
        if not city or not keyword:
            raise ValueError("节点 %s 缺少 city 或 keyword" % node_id)

        old = self.previous.get((city, keyword))
        if old and _fresh(str(old.get("fetched_at") or "")):
            return json.dumps({**old, "id": node_id, "reused": True}, ensure_ascii=False)

        evidence = await self.tools.poi(keyword, city, node_id)
        return json.dumps(evidence, ensure_ascii=False)

    def collect(self, dag_result: Dict[str, Any], plan: ToolPlan) -> List[Dict[str, Any]]:
        """按计划顺序回收证据；失败或跳过的节点也要留下可展示的失败记录。"""
        nodes = {node.id: node for node in plan.nodes}
        collected: List[Dict[str, Any]] = []
        for item in dag_result.get("results", []):
            node_id = str(item.get("node_id") or "")
            node = nodes.get(node_id)
            arguments = (node.arguments if node else {}) or {}
            parsed = None
            if item.get("status") == "success":
                try:
                    parsed = json.loads(item.get("output") or "")
                except (TypeError, ValueError):
                    parsed = None
            if isinstance(parsed, dict):
                if parsed.get("id") != node_id:
                    # 调度器缓存命中：同一组参数复用了前一个节点的结果。
                    parsed = {**parsed, "id": node_id, "reused": parsed.get("status") == "ok"}
                collected.append(parsed)
                continue
            collected.append(
                build_evidence(
                    node_id,
                    "poi",
                    str(arguments.get("keyword") or ""),
                    str(arguments.get("city") or ""),
                    "failed",
                    source="地点查询",
                    source_kind="none",
                    reason=str(item.get("error") or "查询失败"),
                )
            )
        return collected
