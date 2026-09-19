#!/usr/bin/env python3
"""把 L3/L4 任务规范适配为现有推理器所需的 conversations JSONL。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/final/agent_prm_hard_tasks_v1.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/final/agent_prm_hard_tasks_v1_runner.jsonl"))
    args = parser.parse_args()
    count = 0
    with args.input.open(encoding="utf-8") as source, args.output.open("w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue
            task = json.loads(line)
            row = {
                "id": task["id"],
                "conversations": [{"role": "user", "content": task["query"]}],
                "evaluation_spec": {key: task[key] for key in (
                    "tier", "required_constraints", "runtime_branch_signals",
                    "min_distinct_tool_types", "min_conditional_decisions",
                )},
            }
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    print(json.dumps({"output": str(args.output), "tasks": count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
