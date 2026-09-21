# -*- coding: utf-8 -*-
"""基于 Tool DAG 的旅行规划推理入口。

流程: Planner 生成结构化 DAG -> 调度器并发执行 -> Answerer 汇总结果。
Planner 或 DAG 执行异常时可回退到原有 ReAct tool loop。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date
from types import SimpleNamespace
from typing import Any, Dict, List

from prompts.prompt import COLDSTART_SYSTEM_PROMPT
from inference.run_tool_loop_infer import (
    TOOL_CLASS_MAP,
    _extract_tag_block,
    execute_tool,
    extract_answer_text,
    infer,
    init_backend,
    load_jsonl_row,
    normalize_system_prompt_inplace,
    tool_loop,
)
from inference.tool_dag import (
    ToolDAGError,
    ToolDAGScheduler,
    ToolPlan,
    _collect_reference_nodes,
    extract_json_object,
)
from inference.tool_verifier import (
    assess_answer_consistency,
    assess_evidence,
    assess_plan,
    build_replan_feedback,
    build_targeted_evidence_repair,
    extract_route_entities,
    merge_plans,
    should_attempt_evidence_repair,
)


PLANNER_SYSTEM_PROMPT = """你是旅行规划 Agent 的任务编译器。请把用户需求编译成可并发执行的 Tool DAG。

当前日期：__CURRENT_DATE__。用户提到“今天、明天、后天”等相对日期时，必须基于该日期准确换算；不得凭空使用历史日期。

规则：
1. 仅使用给定工具，最多 __MAX_NODES__ 个节点。
2. 没有数据依赖的工具不得人为串行化，应放在同一层并发执行。
3. depends_on 只填写当前节点真正依赖的上游节点 id。
4. 参数已经从用户需求中获知时直接填写，不要创建多余节点。
5. 下游确实需要完整上游结果时，可用 {{node_id}}；上游返回 JSON 时可用 {{node_id.field.path}}。
6. 不要将自然语言最终回答作为节点；DAG 中只能包含工具调用。
7. 只规划满足用户明确需求所必需的工具。用户未询问住宿、餐饮、天气或交通时，禁止擅自增加对应工具。
8. 多个工具能提供同类信息时只选一个最合适的，避免重复查询；节点数应尽可能少。
9. 必须覆盖用户明确要求的全部实体和比较维度；出发地、目的地、途经点、交通方式、票价、时长等不得遗漏。
10. 禁止猜测经纬度。around_search、route_planning 的坐标不确定时，直接填写用户给出的地点名称；路线起终点优先保留原始地名。
11. 严格区分“从 A 去 B”和“在 A 搜索带 B 关键词的地点”。例如“广州去顺德寻味”必须在顺德检索餐饮并规划广州到顺德的交通，不能只在广州搜索“顺德菜”。
12. 周边类别查询应使用具体类别词；科教/文化类至少覆盖博物馆、图书馆、科技馆、文化馆或美术馆中的相关类别，允许在节点预算内并行查询。
13. “能推荐一下”表示推荐当前问题所问对象，不表示推荐餐饮、酒店或其他无关类别；禁止擅自扩展工具类型。
14. 用户指定“大巴/公交”时只查询公交路线，不得用飞机或火车工具替代；只有用户明确要求比较多种交通方式时才并行查询对应工具。
15. region 仅填写用户明确给出的城市或可从完整地址确定的城市；无法确定时省略，禁止填写“中国”等过宽区域。
16. 只输出严格 JSON，不要 markdown、思考过程或解释。
17. 若用户消息包含 <route_intent>，它是已经规范化的有序地点链。必须为每一对相邻地点生成独立的 route_planning 节点，origin 和 destination 必须逐字使用对应地点，禁止把“从/经/到”及相邻地点合并进同一个参数。

输出格式：
{
  "goal": "一句话任务目标",
  "nodes": [
    {
      "id": "唯一英文节点名",
      "tool": "工具名称",
      "arguments": {},
      "depends_on": [],
      "timeout_seconds": 15,
      "retries": 1
    }
  ]
}

可用工具：
<tools>
__TOOLS__
</tools>
"""


ANSWER_SYSTEM_PROMPT = """你是严谨的旅行规划助手。请仅根据用户需求和工具执行证据生成最终方案。
要求：
1. 不得杜撰工具结果中没有的实时事实；失败节点涉及的信息要明确说明无法确认。
2. 优先满足日期、预算、同行人、交通和偏好等用户约束。
3. 信息不足时给出合理的待确认项。
4. 回答控制在 600 个汉字以内，优先保证行程完整，避免冗长复述工具原文。
5. 最终只输出一次完整的 <answer>...</answer>，必须包含闭合标签。
6. 逐项覆盖用户明确提出的地点、交通方式、价格、时间和偏好；证据未覆盖的项目必须明确标为待确认，不得直接省略。
7. 严格校验地理一致性：只有工具结果的城市/区域与用户目标一致，或返回了相对中心点的 distance，才可称为“附近”；异地结果不得推荐为附近地点。
8. 不得把“名称含目的地特色”的异地餐厅当作目的地内餐厅，例如广州的“顺德菜”餐厅不能回答成已经到顺德寻味。
9. 周边检索无结果时，只能表述“当前范围内未检索到，不代表一定不存在”，并建议扩大半径、确认地点或查询品牌官方渠道；禁止臆测当地偏远、消费能力、门店布局，也禁止无证据推荐其他城市。
"""


def _model_args(args, max_new_tokens: int, temperature: float) -> SimpleNamespace:
    values = vars(args).copy()
    values.update({
        "max_new_tokens": max_new_tokens,
        "temperature": temperature,
        "do_sample": temperature > 0,
    })
    return SimpleNamespace(**values)


def _extract_query_from_dataset(path: str, idx: int) -> str:
    row = load_jsonl_row(path, idx)
    conversations = row.get("conversations") or row.get("messages") or row.get("answer")
    if not conversations:
        raise ValueError("数据集中没有 conversations/messages/answer")
    for message in conversations:
        if message.get("role") == "user":
            return str(message.get("content", ""))
    raise ValueError("数据集中没有 user 消息")


def compile_plan(
    model_or_llm,
    tokenizer,
    args,
    user_query: str,
    planner_feedback: str = "",
    allow_verifier: bool = True,
) -> ToolPlan:
    tools_block = _extract_tag_block(COLDSTART_SYSTEM_PROMPT, "tools")
    prompt = (
        PLANNER_SYSTEM_PROMPT
        .replace("__MAX_NODES__", str(args.dag_max_nodes))
        .replace("__TOOLS__", tools_block)
        .replace("__CURRENT_DATE__", date.today().strftime("%Y-%m-%d"))
    )
    user_content = user_query
    route_entities = extract_route_entities(user_query)
    if len(route_entities) >= 2:
        route_intent = json.dumps({
            "type": "ordered_multi_stop_route",
            "locations": route_entities,
            "required_legs": [
                {"origin": origin, "destination": destination}
                for origin, destination in zip(route_entities, route_entities[1:])
            ],
        }, ensure_ascii=False)
        user_content += f"\n\n<route_intent>{route_intent}</route_intent>"
    if planner_feedback:
        user_content += f"\n\n<verifier_feedback>{planner_feedback}</verifier_feedback>"
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": user_content},
    ]
    raw = infer(
        model_or_llm,
        tokenizer,
        messages,
        _model_args(args, args.planner_max_new_tokens, args.planner_temperature),
    )
    plan_dict = extract_json_object(raw)
    raw_nodes = plan_dict.get("nodes", [])
    if not isinstance(raw_nodes, list) or len(raw_nodes) > max(args.dag_max_nodes * 8, 64):
        raise ToolDAGError("Planner 节点数量异常，拒绝自动修复")
    sanitized_nodes = []
    sanitize_actions = []
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict) or not str(node.get("tool", "")).strip():
            sanitize_actions.append(f"drop_invalid_node:{index}")
            continue
        normalized = dict(node)
        if not str(normalized.get("id", "")).strip():
            normalized["id"] = f"auto_node_{index + 1}"
            sanitize_actions.append(f"fill_node_id:{index}")
        if not normalized.get("timeout_seconds"):
            normalized["timeout_seconds"] = 15
        if normalized.get("retries") in (None, ""):
            normalized["retries"] = 1
        sanitized_nodes.append(normalized)
    if not sanitized_nodes:
        raise ToolDAGError("Planner 未生成可修复的工具节点")
    node_ids = {str(node["id"]) for node in sanitized_nodes}
    repaired_nodes = []
    for node in sanitized_nodes:
        node_id = str(node["id"])
        references = _collect_reference_nodes(node.get("arguments", {}))
        missing_references = references.difference(node_ids)
        if missing_references:
            sanitize_actions.append(f"drop_missing_reference:{node_id}")
            continue
        declared = [str(dep) for dep in node.get("depends_on", []) if str(dep) in node_ids]
        if len(declared) != len(node.get("depends_on", [])):
            sanitize_actions.append(f"drop_missing_dependency:{node_id}")
        undeclared = sorted(references.difference(declared))
        if undeclared:
            declared.extend(undeclared)
            sanitize_actions.append(f"add_reference_dependency:{node_id}")
        node["depends_on"] = list(dict.fromkeys(declared))
        repaired_nodes.append(node)
    sanitized_nodes = repaired_nodes
    if not sanitized_nodes:
        raise ToolDAGError("Planner 节点引用均无效，无法修复")
    sanitized_plan = dict(plan_dict)
    sanitized_plan["nodes"] = sanitized_nodes
    plan = ToolPlan.from_dict(
        sanitized_plan,
        allowed_tools=set(TOOL_CLASS_MAP),
        max_nodes=max(args.dag_max_nodes, len(sanitized_nodes)),
    )
    plan.repair_actions.extend(sanitize_actions)
    plan = plan.prune_semantic_redundancy().prune_irrelevant_to_query(user_query).fit_max_nodes(args.dag_max_nodes)
    verification = assess_plan(user_query, plan, args.dag_max_nodes)
    plan.verification = {
        "selected_candidate": 1,
        "candidate_count": 1,
        "initial": verification.to_dict(),
    }
    if (
        allow_verifier
        and getattr(args, "enable_plan_verifier", True)
        and verification.risk >= getattr(args, "plan_replan_threshold", 0.45)
    ):
        try:
            candidate = compile_plan(
                model_or_llm,
                tokenizer,
                args,
                user_query,
                planner_feedback=build_replan_feedback("规划", verification),
                allow_verifier=False,
            )
            candidate_check = assess_plan(user_query, candidate, args.dag_max_nodes)
            if candidate_check.risk < verification.risk:
                plan = candidate
                selected = 2
            else:
                selected = 1
            plan.verification = {
                "selected_candidate": selected,
                "candidate_count": 2,
                "initial": verification.to_dict(),
                "candidate": candidate_check.to_dict(),
            }
        except Exception as exc:
            plan.verification = {
                "selected_candidate": 1,
                "candidate_count": 2,
                "initial": verification.to_dict(),
                "candidate_error": f"{type(exc).__name__}: {exc}",
            }
    return plan


async def execute_verified_dag(
    model_or_llm,
    tokenizer,
    args,
    user_query: str,
    executor,
) -> tuple[ToolPlan, Dict[str, Any], Dict[str, Any]]:
    """执行 DAG；仅在证据高风险时生成一个修复候选并增量补图。"""
    planning_started = time.perf_counter()
    plan = compile_plan(model_or_llm, tokenizer, args, user_query)
    planning_latency_ms = (time.perf_counter() - planning_started) * 1000
    scheduler = ToolDAGScheduler(
        executor=executor,
        max_concurrency=args.dag_max_concurrency,
        fail_fast=args.dag_fail_fast,
        enable_cache=not args.no_dag_cache,
    )
    tool_started = time.perf_counter()
    dag_result = await scheduler.run(plan)
    tool_execution_latency_ms = (time.perf_counter() - tool_started) * 1000
    evidence_check = assess_evidence(user_query, plan, dag_result)
    metadata: Dict[str, Any] = {
        "plan": getattr(plan, "verification", {}),
        "evidence_initial": evidence_check.to_dict(),
        "evidence_repair_triggered": False,
        "added_nodes": 0,
        "actual_tool_calls": len(plan.nodes),
        "planning_latency_ms": planning_latency_ms,
        "tool_execution_latency_ms": tool_execution_latency_ms,
    }
    targeted_candidate = build_targeted_evidence_repair(plan, dag_result, args.dag_max_nodes)
    if targeted_candidate is not None:
        targeted = merge_plans(plan, targeted_candidate, args.dag_max_nodes)
        added = len(targeted.nodes) - len(plan.nodes)
        targeted_started = time.perf_counter()
        targeted_result = await scheduler.run(targeted)
        tool_execution_latency_ms += (time.perf_counter() - targeted_started) * 1000
        targeted_check = assess_evidence(user_query, targeted, targeted_result)
        metadata.update({
            "targeted_repair_triggered": True,
            "targeted_repair_added_nodes": added,
            "targeted_repair_evidence": targeted_check.to_dict(),
            "actual_tool_calls": len(targeted.nodes),
            "tool_execution_latency_ms": tool_execution_latency_ms,
        })
        if targeted_check.risk <= evidence_check.risk:
            plan, dag_result, evidence_check = targeted, targeted_result, targeted_check
    if (
        getattr(args, "enable_evidence_repair", True)
        and evidence_check.risk >= getattr(args, "evidence_repair_threshold", 0.5)
        and not should_attempt_evidence_repair(evidence_check)
    ):
        metadata["evidence_repair_skipped"] = "non_actionable_evidence_risk"
    if (
        getattr(args, "enable_evidence_repair", True)
        and evidence_check.risk >= getattr(args, "evidence_repair_threshold", 0.5)
        and should_attempt_evidence_repair(evidence_check)
        and len(plan.nodes) < args.dag_max_nodes
    ):
        repair_planning_started = time.perf_counter()
        try:
            candidate = compile_plan(
                model_or_llm,
                tokenizer,
                args,
                user_query,
                planner_feedback=build_replan_feedback("证据", evidence_check),
                allow_verifier=False,
            )
            planning_latency_ms += (time.perf_counter() - repair_planning_started) * 1000
            metadata["planning_latency_ms"] = planning_latency_ms
            merged = merge_plans(plan, candidate, args.dag_max_nodes)
        except Exception as exc:
            metadata["evidence_repair_error"] = f"{type(exc).__name__}: {exc}"
            return plan, dag_result, metadata
        added = len(merged.nodes) - len(plan.nodes)
        if added > 0:
            repair_tool_started = time.perf_counter()
            repaired_result = await scheduler.run(merged)
            tool_execution_latency_ms += (time.perf_counter() - repair_tool_started) * 1000
            repaired_check = assess_evidence(user_query, merged, repaired_result)
            metadata.update({
                "evidence_repair_triggered": True,
                "added_nodes": added,
                "evidence_repaired": repaired_check.to_dict(),
                "actual_tool_calls": len(plan.nodes) + added,
                "planning_latency_ms": planning_latency_ms,
                "tool_execution_latency_ms": tool_execution_latency_ms,
            })
            if repaired_check.risk <= evidence_check.risk:
                plan, dag_result = merged, repaired_result
    return plan, dag_result, metadata


def _evidence_payload(dag_result: Dict[str, Any], max_chars: int) -> str:
    compact_results: List[Dict[str, Any]] = []
    results = dag_result["results"]
    per_result_chars = max(600, max_chars // max(1, len(results)))
    for result in results:
        compact_results.append({
            "node_id": result["node_id"],
            "tool": result["tool"],
            "status": result["status"],
            "arguments": result["arguments"],
            "output": result["output"][:per_result_chars],
            "error": result["error"],
        })
    return json.dumps(compact_results, ensure_ascii=False, indent=2)


def generate_answer(
    model_or_llm,
    tokenizer,
    args,
    user_query: str,
    dag_result: Dict[str, Any],
    verification_alert: Dict[str, Any] | None = None,
) -> str:
    evidence = _evidence_payload(dag_result, args.answer_evidence_max_chars)
    alert = ""
    if verification_alert and verification_alert.get("reasons"):
        alert = (
            "\n\n<verification_alert>\n"
            f"风险类型：{verification_alert.get('reasons', [])}\n"
            f"不可可靠满足的项目：{verification_alert.get('missing_requirements', [])}\n"
            "若包含 route_geographic_jump 或 business_evidence_failure，必须明确说明地点/路线存在冲突或无法定位，"
            "不得把异地结果组织成可执行路线。若包含 empty_evidence，必须说明当前范围未检索到，"
            "不得把其他地区结果称为附近。\n</verification_alert>"
        )
    messages = [
        {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"用户需求：\n{user_query}\n\n工具执行证据：\n{evidence}{alert}",
        },
    ]
    raw = infer(
        model_or_llm,
        tokenizer,
        messages,
        _model_args(args, args.max_new_tokens, args.temperature),
    )
    if "<answer>" in raw and "</answer>" not in raw:
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": "上一次输出被截断。请重新给出不超过600个汉字的完整答案，必须以 </answer> 结束。",
            },
        ]
        raw = infer(
            model_or_llm,
            tokenizer,
            retry_messages,
            _model_args(args, max(args.max_new_tokens, 1024), 0.0),
        )
    answer = extract_answer_text(raw) or raw.strip()
    consistency = assess_answer_consistency(user_query, answer)
    if consistency.risk > 0:
        rewrite_messages = messages + [
            {"role": "assistant", "content": answer},
            {"role": "user", "content": (
                "请仅重写最终答案。必须修复以下逻辑矛盾："
                f"{consistency.missing_requirements}。"
                "比较结论必须与答案中明确列出的时间数值一致；不要编造新的时间或重跑工具。"
            )},
        ]
        rewritten = infer(
            model_or_llm, tokenizer, rewrite_messages,
            _model_args(args, max(args.max_new_tokens, 1024), 0.0),
        )
        answer = extract_answer_text(rewritten) or rewritten.strip()
    return answer


async def run_react_fallback(model_or_llm, tokenizer, args, user_query: str) -> Dict[str, Any]:
    system_prompt = (
        COLDSTART_SYSTEM_PROMPT
        .replace("__CURRENT_DATE__", date.today().strftime("%Y-%m-%d"))
        .replace("__MAX_TOOL_CALL__", str(args.system_max_tool_calls))
    )
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_query},
    ]
    normalize_system_prompt_inplace(messages, args.system_max_tool_calls)
    return await tool_loop(messages, args, tokenizer, model_or_llm, args.tools_dir)


async def run(args) -> Dict[str, Any]:
    total_started = time.perf_counter()
    user_query = args.query or _extract_query_from_dataset(args.dataset_path, args.sample_idx)
    backend_started = time.perf_counter()
    tokenizer, model, llm = init_backend(args)
    backend_ms = (time.perf_counter() - backend_started) * 1000
    model_or_llm = llm if args.infer_backend == "vllm" else model

    try:
        planner_started = time.perf_counter()
        async def executor(tool: str, arguments: Dict[str, Any], node_id: str) -> str:
            return await execute_tool(args.tools_dir, tool, arguments, node_id, args)
        plan, dag_result, verifier = await execute_verified_dag(
            model_or_llm, tokenizer, args, user_query, executor
        )
        planner_ms = float(verifier["planning_latency_ms"])
        if dag_result["success_count"] == 0:
            raise ToolDAGError("DAG 没有成功执行任何工具节点")
        answer_started = time.perf_counter()
        answer = generate_answer(
            model_or_llm, tokenizer, args, user_query, dag_result,
            verifier.get("evidence_initial"),
        )
        answer_ms = (time.perf_counter() - answer_started) * 1000
        return {
            "mode": "tool_dag",
            "query": user_query,
            "plan": {
                "goal": plan.goal,
                "nodes": [node.__dict__ for node in plan.nodes],
                "repair_actions": plan.repair_actions,
            },
            "dag_result": dag_result,
            "verifier": verifier,
            "prediction": answer,
            "timings_ms": {
                "backend_init": backend_ms,
                "planner": planner_ms,
                "tool_execution": verifier["tool_execution_latency_ms"],
                "answer_generation": answer_ms,
                "total": (time.perf_counter() - total_started) * 1000,
            },
        }
    except Exception as exc:
        if not args.react_fallback:
            raise
        fallback = await run_react_fallback(model_or_llm, tokenizer, args, user_query)
        return {
            "mode": "react_fallback",
            "query": user_query,
            "dag_error": f"{type(exc).__name__}: {exc}",
            "prediction": fallback["final_answer"] or fallback["final_response"],
            "react_result": fallback,
        }


def parse_args():
    parser = argparse.ArgumentParser(description="旅行规划 Agent：Tool DAG 并发推理")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--query", default="", help="直接输入用户问题")
    source.add_argument("--dataset_path", default="", help="从 JSONL 数据集读取用户问题")
    parser.add_argument("--sample_idx", type=int, default=0)
    parser.add_argument("--infer_backend", default="transformers", choices=["transformers", "vllm"])
    parser.add_argument("--model_dir", required=True)
    parser.add_argument("--tools_dir", default="tools")
    parser.add_argument("--dag_max_nodes", type=int, default=8)
    parser.add_argument("--dag_max_concurrency", type=int, default=4)
    parser.add_argument("--enable_plan_verifier", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--plan_replan_threshold", type=float, default=0.3)
    parser.add_argument("--enable_evidence_repair", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--evidence_repair_threshold", type=float, default=0.4)
    parser.add_argument("--dag_fail_fast", action="store_true")
    parser.add_argument("--no_dag_cache", action="store_true")
    parser.add_argument("--react_fallback", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--planner_max_new_tokens", type=int, default=1200)
    parser.add_argument("--planner_temperature", type=float, default=0.1)
    parser.add_argument("--answer_evidence_max_chars", type=int, default=12000,
                        help="传给 Answerer 的全部工具证据总字符预算")
    parser.add_argument("--max_new_tokens", type=int, default=5000)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--do_sample", action="store_true", default=True)
    parser.add_argument("--top_p", type=float, default=0.95)
    parser.add_argument("--top_k", type=int, default=50)
    parser.add_argument("--tool_response_max_chars", type=int, default=5000)
    parser.add_argument("--max_turns", type=int, default=13)
    parser.add_argument("--system_max_tool_calls", type=int, default=13)
    parser.add_argument("--force_answer_after_turns", type=int, default=12)
    parser.add_argument("--max_no_tool_no_answer_retries", type=int, default=2)
    parser.add_argument("--max_invalid_tool_rounds", type=int, default=3)
    parser.add_argument("--max_same_tool_call_rounds", type=int, default=3)
    parser.add_argument("--tool_first_enforce", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--print_chars", type=int, default=1800)
    parser.add_argument("--quiet", action="store_true", default=True)
    parser.add_argument("--vllm_tensor_parallel_size", type=int, default=1)
    parser.add_argument("--vllm_gpu_memory_utilization", type=float, default=0.7)
    parser.add_argument("--vllm_max_model_len", type=int, default=32000)
    parser.add_argument("--output_path", default="")
    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    result = asyncio.run(run(cli_args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if cli_args.output_path:
        with open(cli_args.output_path, "w", encoding="utf-8") as output_file:
            output_file.write(rendered + "\n")
