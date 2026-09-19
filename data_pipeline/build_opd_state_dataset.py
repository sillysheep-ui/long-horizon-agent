#!/usr/bin/env python3
"""把多轮旅行工具轨迹拆成适用于 GKD/OPD 的逐决策状态样本。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def normalize_system_prompt(text: str) -> str:
    text = text.replace("<answer>...</answer>", "<answer>实际完整答复</answer>")
    if "最终答案必须同时包含 <answer> 与 </answer>" not in text:
        text += (
            "\n7. 最终答案必须同时包含 <answer> 与 </answer>；正文完整时也不得遗漏闭合标签，"
            "不得把省略号或‘实际完整答复’原样作为答案。"
        )
    return text


def trajectory_id(row: dict, row_index: int) -> str:
    value = row.get("id") or row.get("task_id")
    if value:
        return str(value)
    payload = json.dumps(row.get("conversations", []), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(f"{row_index}:{payload}".encode()).hexdigest()[:16]


def turn_kind(content: str) -> str:
    has_tool = "<tool_call>" in content
    has_answer = "<answer>" in content
    if has_tool and not has_answer:
        return "tool_call"
    if has_answer and not has_tool:
        return "final_answer"
    return "mixed_or_other"


def split_ids(ids: list[str], dev_trajectories: int) -> set[str]:
    ranked = sorted(set(ids), key=lambda value: hashlib.sha256(value.encode()).hexdigest())
    count = min(max(dev_trajectories, 0), max(0, len(ranked) - 1))
    return set(ranked[:count])


def dump_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="data/final/opd_stage0/teacher_sft_train_keep.jsonl",
    )
    parser.add_argument("--output-dir", default="data/final/opd_stage1")
    parser.add_argument("--dev-trajectories", type=int, default=10)
    parser.add_argument("--include-non-keep", action="store_true")
    args = parser.parse_args()

    source = Path(args.input)
    output_dir = Path(args.output_dir)
    trajectories = load_jsonl(source)
    trajectory_ids = [trajectory_id(row, index) for index, row in enumerate(trajectories)]
    dev_ids = split_ids(trajectory_ids, args.dev_trajectories)

    splits: dict[str, list[dict]] = {"train": [], "dev": []}
    skipped = Counter()
    kinds = Counter()

    for row_index, row in enumerate(trajectories):
        if not args.include_non_keep and row.get("clean_flag", "keep") != "keep":
            skipped["non_keep_trajectory"] += 1
            continue
        trace_id = trajectory_id(row, row_index)
        conversations = row.get("conversations") or row.get("messages") or []
        if not conversations or conversations[0].get("role") != "system":
            skipped["invalid_conversation"] += 1
            continue

        normalized = []
        for message in conversations:
            item = {"role": message.get("role"), "content": message.get("content", "")}
            if item["role"] == "system":
                item["content"] = normalize_system_prompt(str(item["content"]))
            normalized.append(item)

        assistant_ordinal = 0
        for message_index, message in enumerate(normalized):
            if message["role"] != "assistant":
                continue
            content = str(message.get("content", "")).strip()
            if not content:
                skipped["empty_assistant_turn"] += 1
                continue
            kind = turn_kind(content)
            if kind == "mixed_or_other":
                skipped["mixed_or_other_turn"] += 1
                continue
            sample_id = f"{trace_id}:a{assistant_ordinal}"
            sample = {
                "id": sample_id,
                "messages": normalized[: message_index + 1],
                "trajectory_id": trace_id,
                "assistant_turn_index": assistant_ordinal,
                "decision_type": kind,
            }
            split = "dev" if trace_id in dev_ids else "train"
            splits[split].append(sample)
            kinds[f"{split}:{kind}"] += 1
            assistant_ordinal += 1

    dump_jsonl(output_dir / "train.jsonl", splits["train"])
    dump_jsonl(output_dir / "dev.jsonl", splits["dev"])
    manifest = {
        "format": "travel-agent-opd-state-v1",
        "source": str(source),
        "method": (
            "每个样本是原轨迹中某个 assistant 决策之前的完整状态，并保留原 assistant 响应作为占位；"
            "GKD 在 lmbda=1 时会移除该响应并由学生 on-policy 重新生成。"
        ),
        "scope": "离线状态分布上的逐状态 on-policy 蒸馏，不等同于端到端在线工具环境 rollout。",
        "source_trajectories": len(trajectories),
        "train_trajectories": len({row["trajectory_id"] for row in splits["train"]}),
        "dev_trajectories": len({row["trajectory_id"] for row in splits["dev"]}),
        "train_samples": len(splits["train"]),
        "dev_samples": len(splits["dev"]),
        "decision_counts": dict(kinds),
        "skipped": dict(skipped),
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if splits["train"] and splits["dev"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
