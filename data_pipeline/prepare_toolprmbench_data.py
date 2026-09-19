#!/usr/bin/env python3
"""将 ToolPRMBench 公开过程偏好对转为平衡的 Qwen SFT 格式。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


PROMPT = """Given the interaction history, function description and two actions, which action is the correct intermediate step that could help in finishing the task:
## history: {history}

## function description: {functions}

## action_1: {action_1}

## action_2: {action_2}

Please only generate action_1 or action_2 as the final answer."""


def text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def prompt_of(row: dict, first: str, second: str) -> str:
    return PROMPT.format(history=text(row.get("history", [])), functions=text(row.get("functions", [])),
                         action_1=text(first), action_2=text(second))


def example(row: dict, source: str, swapped: bool) -> dict:
    chosen, rejected = row["action_chosen"], row["action_rejected"]
    first, second = (rejected, chosen) if swapped else (chosen, rejected)
    label = "action_2" if swapped else "action_1"
    sample_id = f"{source}::{row.get('sample_id', row.get('index', 'unknown'))}::{int(swapped)}"
    return {
        "id": sample_id,
        "conversations": [
            {"role": "user", "content": prompt_of(row, first, second)},
            {"role": "assistant", "content": label},
        ],
        "metadata": {"source": source, "swapped": swapped},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("/root/autodl-tmp/ToolPRMBench/data"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/final/toolprmbench"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in ("prmbench_bfcl_train.json", "prmbench_ToolSandbox_train.json"):
        source = name.removesuffix("_train.json")
        with (args.data_dir / name).open(encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    raw = json.loads(line)
                    rows.extend([example(raw, source, False), example(raw, source, True)])
    rows.sort(key=lambda x: hashlib.sha256(x["id"].encode()).hexdigest())
    split = int(len(rows) * 0.9)
    for name, subset in (("train.jsonl", rows[:split]), ("val.jsonl", rows[split:])):
        with (args.output_dir / name).open("w", encoding="utf-8") as target:
            for item in subset:
                target.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(json.dumps({"train": split, "val": len(rows) - split, "total": len(rows)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
