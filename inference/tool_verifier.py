# -*- coding: utf-8 -*-
"""Tool DAG 的低成本风险验证、候选选择与增量合并。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

from inference.tool_dag import ToolNode, ToolPlan, _collect_reference_nodes


EMPTY_MARKERS = (
    "no poi data available",
    "未查询到",
    "未检索到",
    "无法定位",
    "no result",
)
BUSINESS_FAILURE_MARKERS = ("路线地点无法定位", "无法定位:", "地点无法定位")


def _duration_minutes(text: str) -> int | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:小时|时)(?:\s*(\d+)\s*分)?", text)
    if not match:
        return None
    return round(float(match.group(1)) * 60) + int(match.group(2) or 0)


def assess_answer_consistency(query: str, answer: str) -> VerificationResult:
    """检测比较题中已陈述时间与最终推荐相互矛盾的明显错误。"""
    if not (any(term in query for term in ("哪个更快", "谁更快", "哪个快"))
            and "高铁" in query and any(term in query for term in ("飞机", "航班"))):
        return VerificationResult(0.0)
    def latest_labeled_duration(labels: str) -> int | None:
        windows = re.findall(rf"(?:{labels})[^\n。]{{0,100}}", answer)
        durations = [_duration_minutes(window) for window in windows]
        valid = [value for value in durations if value is not None]
        return valid[-1] if valid else None

    train_minutes = latest_labeled_duration("高铁")
    flight_minutes = latest_labeled_duration("飞机|航班")
    recommends_train = bool(re.search(r"(?:推荐|选择|建议).{0,12}高铁|高铁比(?:飞机|航班)更快", answer))
    recommends_flight = bool(re.search(r"(?:推荐|选择|建议).{0,12}(?:飞机|航班)|(?:飞机|航班)比高铁更快", answer))
    if train_minutes and flight_minutes:
        if recommends_train and train_minutes > flight_minutes + 15:
            return VerificationResult(0.7, ["answer_time_recommendation_conflict"], ["高铁耗时大于飞机但推荐高铁更快"])
        if recommends_flight and flight_minutes > train_minutes + 15:
            return VerificationResult(0.7, ["answer_time_recommendation_conflict"], ["飞机耗时大于高铁但推荐飞机更快"])
    return VerificationResult(0.0)


@dataclass
class VerificationResult:
    risk: float
    reasons: List[str] = field(default_factory=list)
    missing_requirements: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "risk": round(self.risk, 4),
            "reasons": self.reasons,
            "missing_requirements": self.missing_requirements,
        }


def _plan_text(plan: ToolPlan) -> str:
    return json.dumps({
        "goal": plan.goal,
        "nodes": [node.__dict__ for node in plan.nodes],
    }, ensure_ascii=False)


def extract_route_entities(query: str) -> List[str]:
    """从多途经点路线请求中提取保持用户顺序的地点链。"""
    multi_stop = any(term in query for term in ("途径", "经停", "经过", "再去", "顺路", "怎么走"))
    multi_stop = multi_stop or bool(re.search(r"从.+经.*?(?:到|再去)", query))
    if "从" not in query or not multi_stop:
        return []
    text = query.split("从", 1)[1]
    text = re.split(r"(?:怎么走|路线(?:要|，|,)|这几地|一路游玩|有没有方便)", text, maxsplit=1)[0]
    text = re.sub(r"[？?。]", "", text)
    text = re.sub(r"(?:出发|经停|途径|经过|最后到|再去|到达|到|经)", "|", text)
    if re.search(r"[、，,]", text) and "和" in text:
        prefix, suffix = text.rsplit("和", 1)
        text = prefix + "|" + suffix
    parts = re.split(r"[|、，,]", text)
    stopwords = ("请帮我", "规划", "路线", "比较方便", "顺路")
    entities = []
    for part in parts:
        item = part.strip(" ：:；;的")
        for word in stopwords:
            item = item.replace(word, "")
        item = item.strip().rstrip("的").strip()
        if 2 <= len(item) <= 32 and not any(word in item for word in ("怎么", "路线", "一下")):
            entities.append(item)
    return list(dict.fromkeys(entities))


def assess_plan(query: str, plan: ToolPlan, max_nodes: int = 8) -> VerificationResult:
    rendered = _plan_text(plan)
    risk = 0.0
    reasons: List[str] = []
    missing: List[str] = []

    entities = extract_route_entities(query)
    for entity in entities:
        if entity not in rendered:
            missing.append(entity)
    if missing:
        ratio = len(missing) / max(1, len(entities))
        risk += min(0.55, 0.2 + ratio * 0.35)
        reasons.append("route_entity_coverage")

    if len(entities) >= 2:
        route_nodes = [node for node in plan.nodes if node.tool == "route_planning"]
        missing_edges = []
        for origin, destination in zip(entities, entities[1:]):
            matched = any(
                str(node.arguments.get("origin", "")).strip() == origin
                and str(node.arguments.get("destination", "")).strip() == destination
                for node in route_nodes
            )
            if not matched:
                missing_edges.append(f"{origin}->{destination}")
        if missing_edges:
            missing.extend(f"路线段:{edge}" for edge in missing_edges)
            ratio = len(missing_edges) / (len(entities) - 1)
            risk += min(0.7, 0.3 + ratio * 0.4)
            reasons.append("route_edge_mismatch")

    if entities:
        poi_nodes = sum(node.tool == "poi_search" for node in plan.nodes)
        route_nodes = [node for node in plan.nodes if node.tool == "route_planning"]
        literal_coordinate_routes = sum(
            bool(re.search(r"\d{2,3}\.\d+\s*,\s*\d{2}\.\d+", json.dumps(node.arguments)))
            for node in route_nodes
        )
        if route_nodes and literal_coordinate_routes and poi_nodes == 0:
            risk += 0.5
            reasons.append("unresolved_named_locations")

    food_requested = any(term in query for term in ("美食", "餐厅", "餐馆", "寻味", "小吃", "吃"))
    destination_match = re.search(r"去([^，,。？?]{2,16})", query)
    if food_requested and destination_match:
        destination = re.split(r"(?:寻味|求|吃|美食|旅游|游玩)", destination_match.group(1))[0].strip()
        food_nodes = [node for node in plan.nodes if node.tool in {"catering_search", "around_search"}]
        if destination and food_nodes and not any(
            destination in str(node.arguments.get("location", ""))
            or destination in str(node.arguments.get("region", ""))
            for node in food_nodes
        ):
            missing.append(f"目的地区域:{destination}")
            risk += 0.45
            reasons.append("destination_mismatch")
        route_nodes = [node for node in plan.nodes if node.tool == "route_planning"]
        if destination and route_nodes and not any(
            destination in str(node.arguments.get("destination", "")) for node in route_nodes
        ):
            missing.append(f"路线目的地:{destination}")
            risk += 0.35
            reasons.append("route_destination_mismatch")

    requested_tools = []
    if any(term in query for term in ("飞机", "航班")):
        requested_tools.append("flights_search")
    if any(term in query for term in ("高铁", "火车", "动车")):
        requested_tools.append("train_tickets_search")
    if any(term in query for term in ("天气", "气温", "下雨", "温度")):
        requested_tools.append("weather_search")
    for tool in requested_tools:
        if not any(node.tool == tool for node in plan.nodes):
            missing.append(tool)
            risk += 0.3
            reasons.append(f"missing_tool:{tool}")

    if any(action.startswith(("drop_", "fill_", "add_reference")) for action in plan.repair_actions):
        risk += 0.15
        reasons.append("planner_auto_repair")
    if any(action.startswith("intent_prune") for action in plan.repair_actions):
        risk += 0.1
        reasons.append("intent_pruned")
    if len(plan.nodes) >= max_nodes:
        risk += 0.1
        reasons.append("node_budget_saturated")
    if any(
        node.tool == "around_search"
        and str(node.arguments.get("location", "")).strip() in {"中国", "全国"}
        for node in plan.nodes
    ):
        risk += 0.35
        reasons.append("overbroad_location")

    return VerificationResult(min(1.0, risk), list(dict.fromkeys(reasons)), missing)


def assess_evidence(query: str, plan: ToolPlan, dag_result: Dict[str, Any]) -> VerificationResult:
    risk = 0.0
    reasons: List[str] = []
    missing: List[str] = []
    results = dag_result.get("results", [])
    failed = [item for item in results if item.get("status") != "success"]
    empty = [
        item for item in results
        if any(marker in str(item.get("output", "")).lower() for marker in EMPTY_MARKERS)
    ]
    if failed:
        risk += min(0.6, 0.2 + len(failed) / max(1, len(results)) * 0.5)
        reasons.append("tool_failure")
        missing.extend(
            f"{item.get('tool', '')}:{json.dumps(item.get('arguments', {}), ensure_ascii=False)}"
            for item in failed
        )
    def has_same_context_success(item: Dict[str, Any]) -> bool:
        """同一工具、同一中心点的并行近义检索可互为部分证据。"""
        arguments = item.get("arguments", {})
        context = str(arguments.get("location") or arguments.get("region") or "").strip()
        if not context:
            return False
        return any(
            other is not item
            and other.get("status") == "success"
            and other.get("tool") == item.get("tool")
            and str((other.get("arguments") or {}).get("location")
                    or (other.get("arguments") or {}).get("region") or "").strip() == context
            and not any(marker in str(other.get("output", "")).lower() for marker in EMPTY_MARKERS)
            for other in results
        )

    hard_empty = [item for item in empty if not has_same_context_success(item)]
    partial_empty = [item for item in empty if has_same_context_success(item)]
    if hard_empty:
        risk += min(0.45, 0.15 + len(hard_empty) / max(1, len(results)) * 0.35)
        reasons.append("empty_evidence")
        missing.extend(
            f"{item.get('tool', '')}:{json.dumps(item.get('arguments', {}), ensure_ascii=False)}"
            for item in hard_empty
        )
    if partial_empty:
        risk += min(0.1, 0.03 * len(partial_empty))
        reasons.append("partial_empty_evidence")
    business_failures = [
        item for item in results
        if any(marker in str(item.get("output", "")) for marker in BUSINESS_FAILURE_MARKERS)
    ]
    if business_failures:
        risk += min(0.65, 0.3 + len(business_failures) / max(1, len(results)) * 0.5)
        reasons.append("business_evidence_failure")
        missing.extend(
            f"{item.get('tool', '')}:{json.dumps(item.get('arguments', {}), ensure_ascii=False)}"
            for item in business_failures
        )

    if len(extract_route_entities(query)) >= 3:
        geographic_jumps = []
        for item in results:
            if item.get("tool") != "route_planning":
                continue
            output = str(item.get("output", ""))
            locations = re.findall(r"(?:起点|终点)解析为：([^（\n]+)", output)
            distances = [int(value) for value in re.findall(r"distance:\s*(\d+)", output)]
            if len(locations) >= 2 and locations[0][:3] != locations[1][:3] and max(distances or [0]) >= 300000:
                geographic_jumps.append(item)
        if geographic_jumps:
            risk += min(0.7, 0.35 + len(geographic_jumps) / max(1, len(results)) * 0.5)
            reasons.append("route_geographic_jump")
            missing.extend(
                f"route:{item.get('arguments', {})}" for item in geographic_jumps
            )
    plan_check = assess_plan(query, plan, max(len(plan.nodes), 1))
    if plan_check.missing_requirements:
        risk += 0.35
        reasons.append("uncovered_requirement")
        missing.extend(plan_check.missing_requirements)
    return VerificationResult(min(1.0, risk), list(dict.fromkeys(reasons)), list(dict.fromkeys(missing)))


def build_replan_feedback(stage: str, verification: VerificationResult) -> str:
    return (
        f"上一候选在{stage}验证中风险为{verification.risk:.2f}；"
        f"问题：{verification.reasons}；缺失项：{verification.missing_requirements}。"
        "请只修复这些问题，优先补齐缺失证据，禁止增加无关工具，并控制总节点数。"
    )


def should_attempt_evidence_repair(verification: VerificationResult) -> bool:
    """仅对可能通过补充/替换工具调用改善的证据风险触发第二候选。"""
    actionable = {"tool_failure", "uncovered_requirement"}
    return bool(actionable.intersection(verification.reasons))


def build_targeted_evidence_repair(
    plan: ToolPlan,
    dag_result: Dict[str, Any],
    max_nodes: int,
) -> ToolPlan | None:
    """为“附近检索为空”构建确定性半径扩展，不调用第二次 Planner。"""
    if len(plan.nodes) >= max_nodes:
        return None
    existing = {
        (node.tool, json.dumps(node.arguments, ensure_ascii=False, sort_keys=True))
        for node in plan.nodes
    }
    additions: List[ToolNode] = []
    existing_ids = {node.id for node in plan.nodes}
    for result in dag_result.get("results", []):
        if result.get("tool") != "around_search":
            continue
        if not any(marker in str(result.get("output", "")).lower() for marker in EMPTY_MARKERS):
            continue
        arguments = dict(result.get("arguments") or {})
        try:
            radius = int(float(arguments.get("radius", 5000)))
        except (TypeError, ValueError):
            radius = 5000
        expanded_radius = min(10000, max(8000, radius * 2))
        if expanded_radius <= radius:
            continue
        arguments["radius"] = expanded_radius
        signature = ("around_search", json.dumps(arguments, ensure_ascii=False, sort_keys=True))
        if signature in existing:
            continue
        node_id = f"verifier_expand_{result.get('node_id', 'around')}"
        suffix = 2
        while node_id in existing_ids:
            node_id = f"verifier_expand_{result.get('node_id', 'around')}_{suffix}"
            suffix += 1
        additions.append(ToolNode(
            id=node_id,
            tool="around_search",
            arguments=arguments,
            timeout_seconds=15.0,
            retries=1,
        ))
        existing.add(signature)
        existing_ids.add(node_id)
        if len(plan.nodes) + len(additions) >= max_nodes:
            break
    if not additions:
        return None
    return ToolPlan(goal=plan.goal, nodes=additions)


def merge_plans(base: ToolPlan, candidate: ToolPlan, max_nodes: int) -> ToolPlan:
    """将候选中的新调用增量并入原计划；同工具同参数节点不会重复执行。"""
    nodes = list(base.nodes)
    signatures = {
        (node.tool, json.dumps(node.arguments, ensure_ascii=False, sort_keys=True))
        for node in nodes
    }
    existing_ids = {node.id for node in nodes}
    added = 0
    for node in candidate.nodes:
        signature = (node.tool, json.dumps(node.arguments, ensure_ascii=False, sort_keys=True))
        if signature in signatures or len(nodes) >= max_nodes:
            continue
        if not _collect_reference_nodes(node.arguments).issubset(existing_ids):
            continue
        node_id = node.id
        suffix = 2
        while node_id in existing_ids:
            node_id = f"{node.id}_{suffix}"
            suffix += 1
        # 增量候选不能依赖未合并的候选节点；保留已存在依赖。
        depends_on = [dep for dep in node.depends_on if dep in existing_ids]
        nodes.append(ToolNode(
            id=node_id,
            tool=node.tool,
            arguments=dict(node.arguments),
            depends_on=depends_on,
            timeout_seconds=node.timeout_seconds,
            retries=node.retries,
        ))
        existing_ids.add(node_id)
        signatures.add(signature)
        added += 1
    actions = list(base.repair_actions)
    if added:
        actions.append(f"verifier_merge:+{added}")
    merged = ToolPlan(goal=base.goal, nodes=nodes, repair_actions=actions)
    merged.validate()
    return merged
