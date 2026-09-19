#!/usr/bin/env python3
"""构建项目内 AgentPRM v2 教师蒸馏批次。

训练候选来自真实多约束轨迹和真实运行中的风险决策场；来源与预设偏好不进入
教师任务文件。教师只根据状态、证据与候选动作给过程监督。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path("data/final")


def opaque(prefix: str, source: str) -> str:
    return f"{prefix}_{hashlib.sha256(source.encode()).hexdigest()[:16]}"


def ordered(rows: list[dict], key: str) -> list[dict]:
    return sorted(rows, key=lambda row: hashlib.sha256(row[key].encode()).hexdigest())


def compact_history(history: list[dict]) -> list[dict]:
    return [{"role": x["role"], "content": x["content"][:700]} for x in history[-4:]]


def real_task(row: dict) -> dict:
    state = row["state"]
    return {
        "task_type": "action_quality",
        "state": {
            "query": state["query"][:900],
            "history": compact_history(state["history"]),
            "tool_steps_so_far": state["tool_steps_so_far"],
        },
        "proposed_action": row["observed_action"],
        "proposed_action_content": row["observed_action_content"][:900],
        "allowed_actions": row["candidate_actions"],
        "teacher_label": None,
    }


def arena_task(row: dict) -> dict:
    state = row["state"]
    evidence = []
    for item in state["evidence"]:
        evidence.append({**item, "output": item.get("output", "")[:600]})
    return {
        "task_type": "action_ranking",
        "state": {
            "query": state["query"][:900],
            "constraints": state["constraints"],
            "observations": state["observations"],
            "evidence": evidence,
            "failed_node": state.get("failed_node"),
        },
        "candidate_actions": row["candidate_actions"],
        "teacher_label": None,
    }


def main() -> None:
    seed_ids = {json.loads(line)["id"] for line in (ROOT / "agent_prm_train_seeds_v2.jsonl").open(encoding="utf-8") if line.strip()}
    real_rows = []
    for name in ("agent_prm_candidates_sft.jsonl", "agent_prm_candidates_rl.jsonl"):
        real_rows.extend(json.loads(line) for line in (ROOT / name).open(encoding="utf-8") if line.strip())

    complex_rows = [row for row in real_rows if row["trajectory_id"] in seed_ids]
    complex_tools = ordered([row for row in complex_rows if row["observed_action"] == "continue_tool"], "example_id")[:300]
    complex_answers = ordered([row for row in complex_rows if row["observed_action"] == "answer"], "example_id")[:250]
    normal_answers = ordered(
        [row for row in real_rows if row["trajectory_id"] not in seed_ids and row["observed_action"] == "answer"], "example_id"
    )[:250]
    selected_real = complex_tools + complex_answers + normal_answers

    arena_rows = [json.loads(line) for line in (ROOT / "agent_prm_trace_grounded_arena_v1.jsonl").open(encoding="utf-8") if line.strip()]
    tasks, mappings = [], []
    for row in selected_real:
        task_id = opaque("aq", row["example_id"])
        tasks.append({"task_id": task_id, **real_task(row)})
        mappings.append({"task_id": task_id, "source_id": row["example_id"], "group": "real"})
    for row in arena_rows:
        task_id = opaque("ar", row["arena_id"])
        tasks.append({"task_id": task_id, **arena_task(row)})
        mappings.append({"task_id": task_id, "source_id": row["arena_id"], "group": "trace_grounded"})

    tasks = ordered(tasks, "task_id")
    out = ROOT / "agent_prm_project_distillation_v2_tasks.jsonl"
    mapping = ROOT / "agent_prm_project_distillation_v2_mapping.jsonl"
    with out.open("w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")
    with mapping.open("w", encoding="utf-8") as handle:
        for item in mappings:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(json.dumps({"total": len(tasks), "complex_tools": len(complex_tools), "complex_answers": len(complex_answers), "normal_answers": len(normal_answers), "trace_grounded": len(arena_rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
