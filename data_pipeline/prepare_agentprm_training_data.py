#!/usr/bin/env python3
"""将教师蒸馏标签转为独立 AgentPRM 的 SFT 训练/验证集。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path("data/final")
SYSTEM = """你是旅行工具 Agent 的过程奖励模型。根据当前状态、工具证据和候选动作，输出严格 JSON：
对单个行动给出 process_quality(1-5)、risk_types、recommended_action、confidence、reason；
对候选行动给出 ranking、best_action_index、risk_types、confidence、reason。
只能依据提供的证据判断，不能补充外部事实。"""


def model_input(task: dict) -> str:
    if task["task_type"] == "action_quality":
        payload = {key: task[key] for key in ("state", "proposed_action", "proposed_action_content", "allowed_actions")}
    else:
        payload = {key: task[key] for key in ("state", "candidate_actions")}
    return json.dumps({"task_type": task["task_type"], **payload}, ensure_ascii=False)


def main() -> None:
    tasks = {json.loads(line)["task_id"]: json.loads(line) for line in (ROOT / "agent_prm_project_distillation_v2_tasks.jsonl").open(encoding="utf-8") if line.strip()}
    labels = [json.loads(line) for line in (ROOT / "agent_prm_project_distillation_v2_labels.jsonl").open(encoding="utf-8") if line.strip()]
    rows = []
    for label in labels:
        task = tasks.get(label.get("task_id"))
        if not task or "teacher_label" not in label:
            continue
        rows.append({
            "id": task["task_id"],
            "conversations": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": model_input(task)},
                {"role": "assistant", "content": json.dumps(label["teacher_label"], ensure_ascii=False)},
            ],
            "metadata": {"task_type": task["task_type"]},
        })
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["metadata"]["task_type"]].append(row)
    train, val = [], []
    for _, items in grouped.items():
        items.sort(key=lambda row: hashlib.sha256(row["id"].encode()).hexdigest())
        split = int(len(items) * 0.9)
        train.extend(items[:split]); val.extend(items[split:])
    train.sort(key=lambda row: row["id"]); val.sort(key=lambda row: row["id"])
    for path, items in ((ROOT / "agent_prm_v2_train.jsonl", train), (ROOT / "agent_prm_v2_val.jsonl", val)):
        with path.open("w", encoding="utf-8") as handle:
            for row in items:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"train": len(train), "val": len(val), "total": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
