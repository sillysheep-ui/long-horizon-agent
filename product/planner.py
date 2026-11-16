"""把规划 Agent 的草案编译成可校验、可调度的 ToolPlan。

规划模型只负责决定“哪天查什么”，这里负责把它变成结构化的 DAG：
补齐城市、生成稳定节点 id、限制查询数量，并交给 ToolPlan 做白名单和环校验。
"""

import json
from typing import Any

from inference.tool_dag import ToolDAGError, ToolPlan

PLANNER_SYSTEM_PROMPT = (
    "你负责国内多日旅行的计划草案。只输出 JSON："
    '{"days":[{"day":1,"city":"城市","poi_queries":["准确地点或类型关键词"]}]}。'
    "规则：1) days 数量严格等于用户天数；2) 每天 2-3 个 poi_queries，"
    "写成具体地点名或明确类别（如“西湖”“龙井村”“博物馆”），不要写“景点”“好玩的地方”；"
    "3) 同一天尽量覆盖不同类型，同一地点只查一次；4) 地点必须位于对应城市；"
    "5) 不要编造价格、班次或经纬度；6) constraints.named_places 里的地点必须出现在某天的 poi_queries 里，优先安排在前几天；"
    "7) constraints.day_span 为 half 时，每天最多 2 个 poi_queries。"
    "若出现 revision 字段，只重写 revision.days 这些天的 poi_queries 以体现修改意见；其他天原样照抄 previous_plan。"
)

MAX_QUERIES_PER_DAY = 3
MAX_NODES = 18
NODE_TIMEOUT_SECONDS = 45.0


def _clean_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:80] for item in value[:limit] if str(item).strip()]


def build_planner_prompt(
    query: str,
    constraints: dict[str, Any],
    previous_plan: dict[str, Any] | None = None,
    revision: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "query": query, "constraints": constraints, "previous_plan": previous_plan,
    }
    if revision:
        payload["revision"] = revision
    return json.dumps(payload, ensure_ascii=False)


def queries_for_day(plan: Any, day: int) -> list[str]:
    """取出某一天的 poi_queries；没有该天时返回空列表。"""
    items = plan.get("days") if isinstance(plan, dict) else None
    for item in items or []:
        try:
            number = int(item.get("day"))
        except (TypeError, ValueError):
            continue
        if number == day:
            return [str(query) for query in (item.get("poi_queries") or [])]
    return []


def days_to_tool_plan(
    days: list[dict[str, Any]],
    days_count: int,
    destinations: list[str],
    *,
    max_nodes: int = MAX_NODES,
    node_timeout_seconds: float = NODE_TIMEOUT_SECONDS,
) -> ToolPlan:
    """把每天的 poi_queries 展开成 poi 节点；非法草案直接报错，不做静默修补。"""
    if not destinations or not str(destinations[0]).strip():
        raise ValueError("缺少目的地，无法生成查询计划")
    if not isinstance(days, list) or len(days) != days_count:
        raise ValueError("规划 Agent 返回的天数与需求不一致")

    nodes: list[dict[str, Any]] = []
    for day_index, day in enumerate(days, 1):
        if not isinstance(day, dict):
            raise ValueError("规划 Agent 返回了非法日期节点")
        city = str(day.get("city") or destinations[0]).strip()[:40] or destinations[0][:40]
        queries = _clean_list(day.get("poi_queries"), MAX_QUERIES_PER_DAY)
        if not queries:
            queries = [city + " 景点"]
        day["day"] = day_index
        day["city"] = city
        day["poi_queries"] = queries
        for query_index, keyword in enumerate(queries, 1):
            nodes.append(
                {
                    "id": "d%d-p%d" % (day_index, query_index),
                    "tool": "poi",
                    "arguments": {"city": city, "keyword": keyword},
                    "timeout_seconds": node_timeout_seconds,
                }
            )

    if len(nodes) > max_nodes:
        raise ValueError("计划超过工具调用预算")
    try:
        return ToolPlan.from_dict(
            {"goal": "查询 %d 天行程的地点来源" % days_count, "nodes": nodes},
            allowed_tools={"poi"},
            max_nodes=max_nodes,
        )
    except ToolDAGError as exc:
        raise ValueError("计划未通过校验: %s" % exc) from exc

