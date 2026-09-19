#!/usr/bin/env python3
"""构造分层、来源隐藏的 AgentPRM 教师蒸馏任务批次。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path


def ordered(rows):
    return sorted(rows, key=lambda x: hashlib.sha256((x.get("example_id") or x["pair_id"]).encode()).hexdigest())


def compact_state(state):
    history = state["history"][-4:]
    return {
        "query": state["query"][:900],
        "history": [{"role": x["role"], "content": x["content"][:700]} for x in history],
        "tool_steps_so_far": state["tool_steps_so_far"],
    }


def opaque_id(prefix, source_id):
    return f"{prefix}_{hashlib.sha256(source_id.encode()).hexdigest()[:16]}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rl", type=Path, default=Path("data/final/agent_prm_candidates_rl.jsonl"))
    parser.add_argument("--sft", type=Path, default=Path("data/final/agent_prm_candidates_sft.jsonl"))
    parser.add_argument("--pairs", type=Path, default=Path("data/final/agent_prm_preference_pairs_v1.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/final/agent_prm_distillation_v1_tasks.jsonl"))
    parser.add_argument("--mapping-output", type=Path, default=Path("data/final/agent_prm_distillation_v1_mapping.jsonl"))
    args = parser.parse_args()

    real_rows = []
    for path in (args.rl, args.sft):
        real_rows.extend(json.loads(x) for x in path.open(encoding="utf-8") if x.strip())
    by_action = defaultdict(list)
    for row in real_rows:
        by_action[row["observed_action"]].append(row)
    # 720 real examples：工具过程与最终作答各 360，避免“回答必然是坏动作”的偏置。
    selected_real = ordered(by_action["continue_tool"])[:360] + ordered(by_action["answer"])[:360]

    pairs = [json.loads(x) for x in args.pairs.open(encoding="utf-8") if x.strip()]
    by_type = defaultdict(list)
    for pair in pairs:
        by_type[pair["counterfactual_type"]].append(pair)
    quotas = {"premature_answer": 120, "redundant_tool": 120, "tool_failure": 120, "unsupported_terminal": 120}
    selected_pairs = [pair for kind, quota in quotas.items() for pair in ordered(by_type[kind])[:quota]]

    tasks = []
    mappings = []
    for row in selected_real:
        task_id = opaque_id("aq", row["example_id"])
        tasks.append({
            "task_id": task_id,
            "task_type": "action_quality",
            "state": compact_state(row["state"]),
            "proposed_action": row["observed_action"],
            "proposed_action_content": row["observed_action_content"][:900],
            "allowed_actions": row["candidate_actions"],
            "teacher_label": None,
        })
        mappings.append({"task_id": task_id, "source_id": row["example_id"], "task_type": "action_quality"})
    for pair in selected_pairs:
        task_id = opaque_id("ap", pair["pair_id"])
        preferred_is_a = int(hashlib.sha256(pair["pair_id"].encode()).hexdigest(), 16) % 2 == 0
        option_a, option_b = (pair["chosen"], pair["rejected"]) if preferred_is_a else (pair["rejected"], pair["chosen"])
        tasks.append({
            "task_id": task_id,
            "task_type": "action_preference",
            "state": compact_state(pair["state"]),
            "option_a": option_a,
            "option_b": option_b,
            "allowed_actions": ["continue_tool", "answer", "replan", "rewrite_answer", "ask_clarification"],
            "teacher_label": None,
        })
        mappings.append({
            "task_id": task_id,
            "source_id": pair["pair_id"],
            "task_type": "action_preference",
            "counterfactual_type": pair["counterfactual_type"],
            "synthetic_preferred_option": "A" if preferred_is_a else "B",
        })
    tasks = sorted(tasks, key=lambda x: hashlib.sha256(x["task_id"].encode()).hexdigest())
    with args.output.open("w", encoding="utf-8") as target:
        for task in tasks:
            target.write(json.dumps(task, ensure_ascii=False) + "\n")
    with args.mapping_output.open("w", encoding="utf-8") as target:
        for mapping in mappings:
            target.write(json.dumps(mapping, ensure_ascii=False) + "\n")
    print(json.dumps({"total": len(tasks), "real": len(selected_real), "preference": len(selected_pairs), "quotas": quotas}, ensure_ascii=False))


if __name__ == "__main__":
    main()
