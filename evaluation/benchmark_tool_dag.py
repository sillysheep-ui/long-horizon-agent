# -*- coding: utf-8 -*-
"""在同一批测试样本上运行 ReAct 与零样本 Tool DAG 基线。"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Tuple

from evaluation.baseline_metrics import write_summary_files
from prompts.prompt import COLDSTART_SYSTEM_PROMPT
from inference.run_tool_dag_infer import execute_verified_dag, generate_answer
from inference.run_tool_loop_infer import (
    execute_tool,
    init_backend,
    load_jsonl_row,
    normalize_system_prompt_inplace,
    tool_loop,
)
from inference.tool_dag import ToolDAGScheduler


def extract_sample(path: str, idx: int) -> Tuple[str, str]:
    row = load_jsonl_row(path, idx)
    conversations = row.get("conversations") or row.get("messages") or row.get("answer")
    if not conversations:
        raise ValueError("样本缺少 conversations/messages/answer")
    query = next(
        (str(message.get("content", "")) for message in conversations if message.get("role") == "user"),
        "",
    )
    if not query:
        raise ValueError("样本缺少 user query")
    return str(row.get("id", idx)), query


def react_messages(query: str, max_tool_calls: int) -> List[Dict[str, Any]]:
    system_prompt = (
        COLDSTART_SYSTEM_PROMPT
        .replace("__CURRENT_DATE__", date.today().strftime("%Y-%m-%d"))
        .replace("__MAX_TOOL_CALL__", str(max_tool_calls))
    )
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": query},
    ]
    normalize_system_prompt_inplace(messages, max_tool_calls)
    return messages


async def run_react_sample(args, tokenizer, model_or_llm, sample_idx: int) -> Dict[str, Any]:
    sample_id, query = extract_sample(args.dataset_path, sample_idx)
    messages = react_messages(query, args.system_max_tool_calls)
    started = time.perf_counter()
    try:
        result = await tool_loop(messages, args, tokenizer, model_or_llm, args.tools_dir)
        elapsed = (time.perf_counter() - started) * 1000
        return {
            "mode": "react",
            "sample_idx": sample_idx,
            "id": sample_id,
            "query": query,
            "success": result["status"] == "answer" and bool(result["final_answer"]),
            "status": result["status"],
            "prediction": result["final_answer"] or result["final_response"],
            "tool_calls": result["total_tool_calls"],
            "turns": result["turns"],
            "total_latency_ms": round(elapsed, 2),
            "tool_latency_ms": None,
            "messages": messages,
        }
    except Exception as exc:
        return error_record("react", sample_idx, sample_id, query, started, exc)


async def run_dag_sample(args, tokenizer, model_or_llm, sample_idx: int) -> Dict[str, Any]:
    sample_id, query = extract_sample(args.dataset_path, sample_idx)
    started = time.perf_counter()
    planner_started = time.perf_counter()
    try:
        async def executor(tool: str, arguments: Dict[str, Any], node_id: str) -> str:
            return await execute_tool(args.tools_dir, tool, arguments, node_id, args)
        plan, dag_result, verifier = await execute_verified_dag(
            model_or_llm, tokenizer, args, query, executor
        )
        planner_latency = float(verifier["planning_latency_ms"])
        answer_started = time.perf_counter()
        prediction = generate_answer(
            model_or_llm, tokenizer, args, query, dag_result,
            verifier.get("evidence_initial"),
        )
        answer_latency = (time.perf_counter() - answer_started) * 1000
        total_latency = (time.perf_counter() - started) * 1000
        node_latencies = [float(item["latency_ms"]) for item in dag_result["results"]]
        level_count = len(dag_result["levels"])
        average_width = len(plan.nodes) / level_count if level_count else 0.0
        tool_latency = float(verifier["tool_execution_latency_ms"])
        speedup_proxy = sum(node_latencies) / tool_latency if tool_latency > 0 else 1.0
        synthetic_messages = [
            {"role": "user", "content": query},
            {"role": "assistant", "content": json.dumps({
                "goal": plan.goal,
                "nodes": [node.__dict__ for node in plan.nodes],
            }, ensure_ascii=False)},
            *[
                {
                    "role": "tool",
                    "name": item["tool"],
                    "content": item["output"] or item["error"],
                }
                for item in dag_result["results"]
            ],
            {"role": "assistant", "content": prediction},
        ]
        return {
            "mode": "tool_dag",
            "sample_idx": sample_idx,
            "id": sample_id,
            "query": query,
            "success": dag_result["success_count"] > 0 and bool(prediction),
            "status": dag_result["status"],
            "prediction": prediction,
            "plan_valid": True,
            "plan_repaired": bool(plan.repair_actions),
            "repair_actions": plan.repair_actions,
            "plan": {"goal": plan.goal, "nodes": [node.__dict__ for node in plan.nodes]},
            "tool_calls": verifier["actual_tool_calls"],
            "total_latency_ms": round(total_latency, 2),
            "planner_latency_ms": round(planner_latency, 2),
            "tool_latency_ms": round(tool_latency, 2),
            "answer_latency_ms": round(answer_latency, 2),
            "average_parallel_width": round(average_width, 4),
            "parallel_speedup_proxy": round(speedup_proxy, 4),
            "dag_result": dag_result,
            "verifier": verifier,
            "messages": synthetic_messages,
        }
    except Exception as exc:
        record = error_record("tool_dag", sample_idx, sample_id, query, started, exc)
        record["plan_valid"] = False
        return record


def error_record(mode, sample_idx, sample_id, query, started, exc) -> Dict[str, Any]:
    return {
        "mode": mode,
        "sample_idx": sample_idx,
        "id": sample_id,
        "query": query,
        "success": False,
        "status": "error",
        "prediction": "",
        "tool_calls": 0,
        "total_latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "tool_latency_ms": None,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


async def main(args) -> None:
    # Transformers 要求 temperature=0 时关闭采样；命令行的 store_true 默认值
    # 可能与用户显式传入的确定性解码参数冲突。
    args.do_sample = args.temperature > 0
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    react_path = output_dir / "react_records.jsonl"
    dag_path = output_dir / "dag_records.jsonl"
    if "react" in args.modes:
        react_path.write_text("", encoding="utf-8")
    if "dag" in args.modes:
        dag_path.write_text("", encoding="utf-8")

    tokenizer, model, llm = init_backend(args)
    model_or_llm = llm if args.infer_backend == "vllm" else model
    react_records: List[Dict[str, Any]] = []
    dag_records: List[Dict[str, Any]] = []

    sample_indices = (
        args.sample_indices
        if args.sample_indices
        else list(range(args.start_idx, args.start_idx + args.num_samples))
    )
    for sample_idx in sample_indices:
        if "react" in args.modes:
            record = await run_react_sample(args, tokenizer, model_or_llm, sample_idx)
            react_records.append(record)
            append_jsonl(react_path, record)
            print(f"[{sample_idx}] ReAct: {record['status']} {record['total_latency_ms']}ms")
        if "dag" in args.modes:
            record = await run_dag_sample(args, tokenizer, model_or_llm, sample_idx)
            dag_records.append(record)
            append_jsonl(dag_path, record)
            print(f"[{sample_idx}] DAG: {record['status']} {record['total_latency_ms']}ms")

    write_summary_files(
        str(output_dir),
        react_records if "react" in args.modes else None,
        dag_records if "dag" in args.modes else None,
    )
    print(f"测评完成：{output_dir / 'report.md'}")


def parse_args():
    parser = argparse.ArgumentParser(description="ReAct vs Tool DAG 零样本基线测评")
    parser.add_argument("--dataset_path", required=True)
    parser.add_argument("--model_dir", required=True)
    parser.add_argument("--tools_dir", default="tools")
    parser.add_argument("--output_dir", default="test_output/tool_dag_baseline")
    parser.add_argument("--modes", nargs="+", choices=["react", "dag"], default=["react", "dag"])
    parser.add_argument("--start_idx", type=int, default=0)
    parser.add_argument("--num_samples", type=int, default=20)
    parser.add_argument("--sample_indices", type=int, nargs="+", default=None,
                        help="指定非连续样本编号；设置后覆盖 start_idx/num_samples")
    parser.add_argument("--infer_backend", default="transformers", choices=["transformers", "vllm"])
    parser.add_argument("--dag_max_nodes", type=int, default=8)
    parser.add_argument("--dag_max_concurrency", type=int, default=4)
    parser.add_argument("--enable_plan_verifier", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--plan_replan_threshold", type=float, default=0.3)
    parser.add_argument("--enable_evidence_repair", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--evidence_repair_threshold", type=float, default=0.4)
    parser.add_argument("--dag_fail_fast", action="store_true")
    parser.add_argument("--no_dag_cache", action="store_true")
    parser.add_argument("--planner_max_new_tokens", type=int, default=1200)
    parser.add_argument("--planner_temperature", type=float, default=0.1)
    parser.add_argument("--answer_evidence_max_chars", type=int, default=12000,
                        help="传给 Answerer 的全部工具证据总字符预算")
    parser.add_argument("--max_turns", type=int, default=13)
    parser.add_argument("--system_max_tool_calls", type=int, default=13)
    parser.add_argument("--max_new_tokens", type=int, default=5000)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--do_sample", action="store_true", default=True)
    parser.add_argument("--top_p", type=float, default=0.95)
    parser.add_argument("--top_k", type=int, default=50)
    parser.add_argument("--tool_response_max_chars", type=int, default=5000)
    parser.add_argument("--force_answer_after_turns", type=int, default=12)
    parser.add_argument("--max_no_tool_no_answer_retries", type=int, default=2)
    parser.add_argument("--max_invalid_tool_rounds", type=int, default=3)
    parser.add_argument("--max_same_tool_call_rounds", type=int, default=3)
    parser.add_argument("--tool_first_enforce", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--print_chars", type=int, default=1000)
    parser.add_argument("--quiet", action="store_true", default=True)
    parser.add_argument("--vllm_tensor_parallel_size", type=int, default=1)
    parser.add_argument("--vllm_gpu_memory_utilization", type=float, default=0.7)
    parser.add_argument("--vllm_max_model_len", type=int, default=32000)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
