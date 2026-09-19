#!/usr/bin/env python3
"""按 OPD 教师上下文预算过滤逐状态数据。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

from transformers import AutoTokenizer


def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tokenizer", required=True)
    parser.add_argument("--max-model-len", type=int, default=16384)
    parser.add_argument("--max-completion-len", type=int, default=2048)
    args = parser.parse_args()

    # vLLM 还要生成 1 token，因此输入上限比 max_model_len 少1。
    max_input_len = args.max_model_len - 1
    max_prefix_len = max_input_len - args.max_completion_len
    if max_prefix_len <= 0:
        raise ValueError("max_completion_len 必须小于 max_model_len - 1")

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, trust_remote_code=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {
        "tokenizer": args.tokenizer,
        "max_model_len": args.max_model_len,
        "max_input_len": max_input_len,
        "max_completion_len": args.max_completion_len,
        "max_prefix_len": max_prefix_len,
        "splits": {},
    }

    for split in ("train", "dev"):
        source = args.input_dir / f"{split}.jsonl"
        target = args.output_dir / f"{split}.jsonl"
        rows = load_jsonl(source)
        kept: List[Dict[str, Any]] = []
        dropped: List[Dict[str, Any]] = []

        for row_index, row in enumerate(rows):
            messages = row["messages"]
            if not messages or messages[-1].get("role") != "assistant":
                raise ValueError(f"{source}:{row_index + 1} 末条消息不是 assistant")
            full_len = len(
                tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=False)
            )
            prefix_len = len(
                tokenizer.apply_chat_template(
                    messages[:-1], tokenize=True, add_generation_prompt=True
                )
            )
            if full_len <= max_input_len and prefix_len <= max_prefix_len:
                kept.append(row)
            else:
                dropped.append(
                    {
                        "row_index": row_index,
                        "trajectory_id": row.get("trajectory_id"),
                        "decision_index": row.get("decision_index"),
                        "full_len": full_len,
                        "prefix_len": prefix_len,
                        "reasons": [
                            reason
                            for condition, reason in (
                                (full_len > max_input_len, "full_sequence_too_long"),
                                (prefix_len > max_prefix_len, "prefix_plus_completion_too_long"),
                            )
                            if condition
                        ],
                    }
                )

        with target.open("w", encoding="utf-8") as handle:
            for row in kept:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        summary["splits"][split] = {
            "source": len(rows),
            "kept": len(kept),
            "dropped": len(dropped),
            "dropped_rows": dropped,
        }

    with (args.output_dir / "filter_summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
