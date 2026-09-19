# -*- coding: utf-8 -*-
"""Tool DAG 的数据模型、校验、参数解析与异步调度器。"""

from __future__ import annotations

import asyncio
import ast
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Set


REFERENCE_RE = re.compile(r"^\{\{\s*([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_.,-]+))?\s*\}\}$")
ANY_REFERENCE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_-]+)(?:\.[A-Za-z0-9_.,-]+)?\s*\}\}")


class ToolDAGError(ValueError):
    """Tool DAG 的构建或执行错误。"""


@dataclass
class ToolNode:
    id: str
    tool: str
    arguments: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)
    timeout_seconds: float = 15.0
    retries: int = 0

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ToolNode":
        node_id = str(raw.get("id", "")).strip()
        tool = str(raw.get("tool", "")).strip()
        arguments = raw.get("arguments", {})
        depends_on = raw.get("depends_on", [])
        if not node_id or not tool:
            raise ToolDAGError("每个节点必须包含非空 id 和 tool")
        if not isinstance(arguments, dict):
            raise ToolDAGError(f"节点 {node_id} 的 arguments 必须是对象")
        if not isinstance(depends_on, list) or not all(isinstance(x, str) for x in depends_on):
            raise ToolDAGError(f"节点 {node_id} 的 depends_on 必须是字符串数组")
        try:
            timeout_seconds = max(0.1, float(raw.get("timeout_seconds") or 15.0))
        except (TypeError, ValueError):
            timeout_seconds = 15.0
        try:
            retries = max(0, int(raw.get("retries") or 0))
        except (TypeError, ValueError):
            retries = 0
        # 票务工具内部包含检索与 LLM 结构化，15 秒通常不足；采用较长单次
        # 超时且关闭节点级重试，避免一次样本最坏阻塞 90 秒。
        if tool in {"flights_search", "train_tickets_search"}:
            timeout_seconds = max(timeout_seconds, 45.0)
            retries = 0
        return cls(
            id=node_id,
            tool=tool,
            arguments=dict(arguments),
            depends_on=list(dict.fromkeys(depends_on)),
            timeout_seconds=timeout_seconds,
            retries=retries,
        )


@dataclass
class ToolPlan:
    goal: str
    nodes: List[ToolNode]
    repair_actions: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls,
        raw: Mapping[str, Any],
        allowed_tools: Optional[Set[str]] = None,
        max_nodes: int = 12,
    ) -> "ToolPlan":
        if not isinstance(raw, Mapping):
            raise ToolDAGError("DAG 根节点必须是 JSON 对象")
        raw_nodes = raw.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ToolDAGError("DAG 必须包含非空 nodes 数组")
        if len(raw_nodes) > max_nodes:
            raise ToolDAGError(f"DAG 节点数 {len(raw_nodes)} 超过限制 {max_nodes}")
        nodes = [ToolNode.from_dict(item) for item in raw_nodes]
        plan = cls(goal=str(raw.get("goal", "")).strip(), nodes=nodes)
        plan.validate(allowed_tools=allowed_tools)
        return plan

    def validate(self, allowed_tools: Optional[Set[str]] = None) -> None:
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ToolDAGError("DAG 节点 id 不能重复")
        for node in self.nodes:
            if allowed_tools is not None and node.tool not in allowed_tools:
                raise ToolDAGError(f"节点 {node.id} 使用了未注册工具 {node.tool}")
            if node.id in node.depends_on:
                raise ToolDAGError(f"节点 {node.id} 不能依赖自身")
            missing = [dep for dep in node.depends_on if dep not in by_id]
            if missing:
                raise ToolDAGError(f"节点 {node.id} 依赖不存在节点: {missing}")
            referenced = _collect_reference_nodes(node.arguments)
            undeclared = referenced.difference(node.depends_on)
            if undeclared:
                raise ToolDAGError(
                    f"节点 {node.id} 的参数引用了 {sorted(undeclared)}，"
                    "但未在 depends_on 中声明"
                )

        visiting: Set[str] = set()
        visited: Set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ToolDAGError(f"DAG 存在环，涉及节点 {node_id}")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dep in by_id[node_id].depends_on:
                visit(dep)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)

    def prune_semantic_redundancy(self) -> "ToolPlan":
        """删除常见的“伪工具回答”叶节点和可被宽泛周边检索覆盖的重复叶节点。"""
        nodes = list(self.nodes)
        original_count = len(nodes)

        def depended_ids(items: List[ToolNode]) -> Set[str]:
            return {dep for item in items for dep in item.depends_on}

        depended = depended_ids(nodes)
        synthesis_terms = ("生成", "总结", "制定", "行程", "建议", "最终回答")
        nodes = [
            node for node in nodes
            if not (
                node.tool == "visit"
                and node.id not in depended
                and any(term in json.dumps(node.arguments, ensure_ascii=False) for term in synthesis_terms)
            )
        ]

        depended = depended_ids(nodes)
        broad_groups = {
            (
                json.dumps(node.arguments.get("location"), ensure_ascii=False, sort_keys=True),
                str(node.arguments.get("radius", "")),
            )
            for node in nodes
            if node.tool == "around_search" and str(node.arguments.get("keyword", "")).strip() in {"景点", "旅游景点"}
        }
        specific_terms = {"公园", "博物馆", "文化景点", "名胜古迹", "旅游景点"}
        nodes = [
            node for node in nodes
            if not (
                node.tool == "around_search"
                and node.id not in depended
                and str(node.arguments.get("keyword", "")).strip() in specific_terms
                and (
                    json.dumps(node.arguments.get("location"), ensure_ascii=False, sort_keys=True),
                    str(node.arguments.get("radius", "")),
                ) in broad_groups
            )
        ]
        actions = list(self.repair_actions)
        if len(nodes) != original_count:
            actions.append(f"semantic_prune:{original_count}->{len(nodes)}")
        pruned = ToolPlan(goal=self.goal, nodes=nodes, repair_actions=actions)
        pruned.validate()
        return pruned

    def prune_irrelevant_to_query(self, query: str) -> "ToolPlan":
        """按用户显式意图删除 Planner 擅自扩展的工具，并保持依赖闭包合法。"""
        text = str(query or "")
        food_terms = ("美食", "餐厅", "餐馆", "餐饮", "吃", "寻味", "小吃", "菜馆", "饭店")
        hotel_terms = ("酒店", "住宿", "住哪", "民宿", "宾馆")
        weather_terms = ("天气", "气温", "下雨", "晴天", "温度")
        food_requested = any(term in text for term in food_terms)
        hotel_requested = any(term in text for term in hotel_terms)
        weather_requested = any(term in text for term in weather_terms)
        bus_only = (
            any(term in text for term in ("大巴", "公交", "巴士"))
            and not any(term in text for term in ("比较", "对比", "高铁", "火车", "飞机", "航班"))
        )

        def is_irrelevant(node: ToolNode) -> bool:
            arguments = json.dumps(node.arguments, ensure_ascii=False)
            if node.tool == "catering_search" and not food_requested:
                return True
            if node.tool == "hotel_search" and not hotel_requested:
                return True
            if node.tool == "weather_search" and not weather_requested:
                return True
            if node.tool == "around_search" and not food_requested:
                if any(term in arguments for term in food_terms):
                    return True
            if bus_only and node.tool in {"flights_search", "train_tickets_search"}:
                return True
            if node.tool == "around_search" and str(node.arguments.get("location", "")).strip() in {"中国", "全国"}:
                return True
            return False

        original_count = len(self.nodes)
        removed = {node.id for node in self.nodes if is_irrelevant(node)}
        changed = True
        while changed:
            changed = False
            for node in self.nodes:
                if node.id not in removed and any(dep in removed for dep in node.depends_on):
                    removed.add(node.id)
                    changed = True
        nodes = [node for node in self.nodes if node.id not in removed]
        if not nodes:
            raise ToolDAGError("意图剪枝后没有可执行工具节点")
        actions = list(self.repair_actions)
        if removed:
            actions.append(f"intent_prune:{original_count}->{len(nodes)}")
        pruned = ToolPlan(goal=self.goal, nodes=nodes, repair_actions=actions)
        pruned.validate()
        return pruned

    def fit_max_nodes(self, max_nodes: int) -> "ToolPlan":
        """通过逐步删除低优先级叶节点压缩 DAG，同时保持依赖闭包合法。"""
        if max_nodes < 1:
            raise ToolDAGError("max_nodes 必须大于 0")
        nodes = list(self.nodes)
        original_count = len(nodes)
        while len(nodes) > max_nodes:
            depended = {dep for node in nodes for dep in node.depends_on}
            leaves = [node for node in nodes if node.id not in depended]
            if not leaves:
                raise ToolDAGError("DAG 无法压缩：没有可删除的叶节点")
            # Planner 通常把补充性调用放在后面；优先删除最靠后的叶节点。
            remove_id = leaves[-1].id
            nodes = [node for node in nodes if node.id != remove_id]
        actions = list(self.repair_actions)
        if len(nodes) != original_count:
            actions.append(f"budget_prune:{original_count}->{len(nodes)}")
        fitted = ToolPlan(goal=self.goal, nodes=nodes, repair_actions=actions)
        fitted.validate()
        return fitted


def _collect_reference_nodes(value: Any) -> Set[str]:
    if isinstance(value, Mapping):
        found: Set[str] = set()
        for item in value.values():
            found.update(_collect_reference_nodes(item))
        return found
    if isinstance(value, list):
        found = set()
        for item in value:
            found.update(_collect_reference_nodes(item))
        return found
    if isinstance(value, str):
        return {match.group(1) for match in ANY_REFERENCE_RE.finditer(value)}
    return set()


@dataclass
class NodeResult:
    node_id: str
    tool: str
    status: str
    arguments: Dict[str, Any]
    output: str = ""
    error: str = ""
    latency_ms: float = 0.0
    attempts: int = 0
    cache_hit: bool = False


def extract_json_object(text: str) -> Dict[str, Any]:
    """从模型输出中提取 JSON；兼容 markdown code fence 与前后解释。"""
    cleaned = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.S | re.I)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(cleaned)
    first = cleaned.find("{")
    last = cleaned.rfind("}")
    if first >= 0 and last > first:
        candidates.append(cleaned[first:last + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except Exception:
            value = None
        if isinstance(value, dict):
            return value
        try:
            value = ast.literal_eval(candidate)
        except Exception:
            value = None
        if isinstance(value, dict):
            return value
        try:
            from json_repair import repair_json
            value = repair_json(candidate, return_objects=True)
        except Exception:
            value = None
        if isinstance(value, dict):
            return value
    raise ToolDAGError("Planner 未输出可解析的 JSON 对象")


def _parse_possible_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _lookup_path(value: Any, path: str) -> Any:
    current = _parse_possible_json(value)
    if not path:
        return current
    # 现有 POI 工具返回 Markdown，而非结构化 JSON。DAG 下游最常需要的
    # 是其中第一个经纬度，因此对常见位置字段做受限、确定性的兼容解析。
    if isinstance(current, str) and path.lower().replace("_", "") in {
        "location", "lnglat", "lng,lat", "coordinates", "coordinate"
    }:
        match = re.search(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?", current)
        if match:
            return match.group(0).replace(" ", "")
    for part in path.split("."):
        current = _parse_possible_json(current)
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ToolDAGError(f"无法从上游结果解析字段路径 {path}")
    return current


def resolve_references(value: Any, outputs: Mapping[str, Any]) -> Any:
    """递归解析参数中的 ``{{node_id.path}}`` 引用。"""
    if isinstance(value, dict):
        return {key: resolve_references(item, outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_references(item, outputs) for item in value]
    if not isinstance(value, str):
        return value

    exact = REFERENCE_RE.match(value)
    if exact:
        node_id, path = exact.groups()
        if node_id not in outputs:
            raise ToolDAGError(f"引用了尚无结果的节点 {node_id}")
        return _lookup_path(outputs[node_id], path or "")

    def replace(match: re.Match[str]) -> str:
        node_id, path = match.groups()
        if node_id not in outputs:
            raise ToolDAGError(f"引用了尚无结果的节点 {node_id}")
        resolved = _lookup_path(outputs[node_id], path or "")
        if isinstance(resolved, (dict, list)):
            return json.dumps(resolved, ensure_ascii=False)
        return str(resolved)

    return re.sub(r"\{\{\s*([A-Za-z0-9_-]+)(?:\.([A-Za-z0-9_.,-]+))?\s*\}\}", replace, value)


ToolExecutor = Callable[[str, Dict[str, Any], str], Awaitable[str]]


BUSINESS_ERROR_PATTERNS = (
    re.compile(r"^\s*API response error\s*:", re.I),
    re.compile(r"^\s*(?:TOOL_ERROR|ERROR)\s*:", re.I),
    re.compile(r"\b(?:INVALID_USER_KEY|USERKEY_PLAT_NOMATCH|CUQPS_HAS_EXCEEDED_THE_LIMIT)\b", re.I),
)


def detect_tool_business_error(output: Any) -> str:
    """识别工具以普通字符串返回的业务错误，避免把 HTTP 200 误计为成功。"""
    text = str(output or "").strip()
    for pattern in BUSINESS_ERROR_PATTERNS:
        if pattern.search(text):
            return text[:500]
    return ""


class ToolDAGScheduler:
    def __init__(
        self,
        executor: ToolExecutor,
        max_concurrency: int = 4,
        fail_fast: bool = False,
        enable_cache: bool = True,
    ) -> None:
        self.executor = executor
        self.max_concurrency = max(1, max_concurrency)
        self.fail_fast = fail_fast
        self.enable_cache = enable_cache
        self._cache: Dict[str, str] = {}
        self._semaphore = asyncio.Semaphore(self.max_concurrency)

    @staticmethod
    def _cache_key(tool: str, arguments: Dict[str, Any]) -> str:
        payload = json.dumps(
            {"tool": tool, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _run_node(
        self,
        node: ToolNode,
        outputs: Mapping[str, Any],
    ) -> NodeResult:
        started = time.perf_counter()
        try:
            arguments = resolve_references(node.arguments, outputs)
        except Exception as exc:
            return NodeResult(
                node_id=node.id,
                tool=node.tool,
                status="failed",
                arguments={},
                error=f"参数解析失败: {exc}",
            )

        cache_key = self._cache_key(node.tool, arguments)
        if self.enable_cache and cache_key in self._cache:
            return NodeResult(
                node_id=node.id,
                tool=node.tool,
                status="success",
                arguments=arguments,
                output=self._cache[cache_key],
                latency_ms=(time.perf_counter() - started) * 1000,
                attempts=0,
                cache_hit=True,
            )

        last_error = ""
        for attempt in range(1, node.retries + 2):
            try:
                async with self._semaphore:
                    output = await asyncio.wait_for(
                        self.executor(node.tool, arguments, node.id),
                        timeout=node.timeout_seconds,
                    )
                output_text = str(output)
                business_error = detect_tool_business_error(output_text)
                if business_error:
                    raise RuntimeError(business_error)
                if self.enable_cache:
                    self._cache[cache_key] = output_text
                return NodeResult(
                    node_id=node.id,
                    tool=node.tool,
                    status="success",
                    arguments=arguments,
                    output=output_text,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    attempts=attempt,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"

        return NodeResult(
            node_id=node.id,
            tool=node.tool,
            status="failed",
            arguments=arguments,
            error=last_error,
            latency_ms=(time.perf_counter() - started) * 1000,
            attempts=node.retries + 1,
        )

    async def run(self, plan: ToolPlan) -> Dict[str, Any]:
        plan.validate()
        pending = {node.id: node for node in plan.nodes}
        results: Dict[str, NodeResult] = {}
        outputs: Dict[str, Any] = {}
        levels: List[List[str]] = []
        started = time.perf_counter()

        while pending:
            blocked = [
                node for node in pending.values()
                if any(results.get(dep) and results[dep].status != "success" for dep in node.depends_on)
            ]
            for node in blocked:
                results[node.id] = NodeResult(
                    node_id=node.id,
                    tool=node.tool,
                    status="skipped",
                    arguments={},
                    error="上游依赖执行失败",
                )
                pending.pop(node.id)

            ready = [
                node for node in pending.values()
                if all(dep in results and results[dep].status == "success" for dep in node.depends_on)
            ]
            if not ready:
                if pending:
                    raise ToolDAGError(f"DAG 无可执行节点: {sorted(pending)}")
                break

            levels.append([node.id for node in ready])
            snapshot = dict(outputs)
            level_results = await asyncio.gather(
                *(self._run_node(node, snapshot) for node in ready)
            )
            for result in level_results:
                results[result.node_id] = result
                pending.pop(result.node_id, None)
                if result.status == "success":
                    outputs[result.node_id] = result.output

            if self.fail_fast and any(result.status == "failed" for result in level_results):
                for node in pending.values():
                    results[node.id] = NodeResult(
                        node_id=node.id,
                        tool=node.tool,
                        status="skipped",
                        arguments={},
                        error="fail_fast 已触发",
                    )
                pending.clear()

        ordered_results = [results[node.id] for node in plan.nodes]
        return {
            "goal": plan.goal,
            "status": "success" if all(r.status == "success" for r in ordered_results) else "partial_failure",
            "levels": levels,
            "latency_ms": (time.perf_counter() - started) * 1000,
            "success_count": sum(r.status == "success" for r in ordered_results),
            "failed_count": sum(r.status == "failed" for r in ordered_results),
            "skipped_count": sum(r.status == "skipped" for r in ordered_results),
            "results": [result.__dict__ for result in ordered_results],
        }
