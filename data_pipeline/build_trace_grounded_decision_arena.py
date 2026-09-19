#!/usr/bin/env python3
"""从真实 DAG 轨迹构造来源隐藏、无规则标签的 AgentPRM 决策候选集。"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path


def compact_result(item: dict) -> dict:
    return {
        "node_id": item.get("node_id"), "tool": item.get("tool"), "status": item.get("status"),
        "arguments": item.get("arguments", {}), "output": item.get("output", "")[:900],
        "error": item.get("error", ""),
    }


def shuffled(arena_id: str, candidates: list[dict]) -> list[dict]:
    copied = list(candidates)
    random.Random(int(hashlib.sha256(arena_id.encode()).hexdigest(), 16)).shuffle(copied)
    return copied


def base_state(task: dict, result: dict) -> dict:
    dag = result.get("dag_result", {})
    verifier = result.get("verifier", {})
    return {
        "query": result.get("query", task["query"]), "constraints": task["required_constraints"],
        "evidence": [compact_result(item) for item in dag.get("results", [])],
        "observations": {
            "success_count": dag.get("success_count", 0), "failed_count": dag.get("failed_count", 0),
            "initial_evidence_risk": verifier.get("evidence_initial", {}).get("risk", 0),
            "risk_reasons": verifier.get("evidence_initial", {}).get("reasons", []),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tasks", type=Path, default=Path("data/final/agent_prm_hard_tasks_v1.jsonl"))
    parser.add_argument("--results-dir", type=Path, default=Path("data/final/agent_prm_hard_v1_raw"))
    parser.add_argument("--output", type=Path, default=Path("data/final/agent_prm_trace_grounded_arena_v1.jsonl"))
    args = parser.parse_args()
    tasks = [json.loads(x) for x in args.tasks.open(encoding="utf-8") if x.strip()]
    paths = [args.results_dir / "l3_01.json"] + [args.results_dir / f"sample_{i}.json" for i in range(1, 16)]
    if len(tasks) != len(paths) or not all(path.exists() for path in paths):
        raise ValueError("任务数与结果文件不匹配")

    arena = []
    for task, path in zip(tasks, paths):
        result = json.loads(path.read_text(encoding="utf-8"))
        state = base_state(task, result)
        terminal_id = f"{task['id']}::terminal"
        terminal_candidates = [
            {"action": "answer_with_evidence", "description": "仅依据当前可用证据作答，并标注未覆盖约束。"},
            {"action": "targeted_repair", "description": "针对失败或缺失的约束补充一次最小必要查询后再决定。"},
            {"action": "clarify_or_defer", "description": "若关键约束无法从当前证据确认，向用户澄清或保守延期结论。"},
        ]
        arena.append({"arena_id": terminal_id, "tier": task["tier"], "state_type": "terminal_decision", "state": state,
                      "candidate_actions": shuffled(terminal_id, terminal_candidates), "label_source": "teacher_pending"})

        for item in result.get("dag_result", {}).get("results", []):
            if item.get("status") != "failed":
                continue
            arena_id = f"{task['id']}::failure::{item['node_id']}"
            failure_state = dict(state)
            failure_state["failed_node"] = compact_result(item)
            candidates = [
                {"action": "retry_same", "description": "保持相同参数重试该工具。"},
                {"action": "replan_targeted_query", "description": "根据失败节点的工具与参数，改写查询范围、实体或工具路径。"},
                {"action": "answer_with_uncertainty", "description": "不将失败节点视为事实，只回答已被其他证据支持的内容。"},
            ]
            arena.append({"arena_id": arena_id, "tier": task["tier"], "state_type": "tool_failure_recovery", "state": failure_state,
                          "candidate_actions": shuffled(arena_id, candidates), "label_source": "teacher_pending"})

        if state["observations"]["initial_evidence_risk"] >= 0.30:
            arena_id = f"{task['id']}::risk_review"
            candidates = [
                {"action": "targeted_repair", "description": "只补齐风险报告指出的缺失证据，避免盲目扩展工具调用。"},
                {"action": "answer_now", "description": "直接基于当前证据输出完整结论。"},
                {"action": "conservative_answer", "description": "输出已确认部分，并明确高风险约束尚无法确认。"},
            ]
            arena.append({"arena_id": arena_id, "tier": task["tier"], "state_type": "evidence_risk_review", "state": state,
                          "candidate_actions": shuffled(arena_id, candidates), "label_source": "teacher_pending"})
    with args.output.open("w", encoding="utf-8") as target:
        for row in arena:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"arena_states": len(arena), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
