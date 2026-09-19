#!/usr/bin/env python3
"""建立分层均衡的 AgentPRM 教师标注集，不修改完整候选样本池。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


# 总计 893：与 893 条真实状态 1:1 配平，同时保证每一种失败模式都出现。
QUOTAS = {"premature_answer": 300, "redundant_tool": 220, "tool_failure": 260, "unsupported_terminal": 113}


def stable_order(rows):
    return sorted(rows, key=lambda row: hashlib.sha256(row["example_id"].encode()).hexdigest())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", type=Path, default=Path("data/final/agent_prm_candidates_rl.jsonl"))
    parser.add_argument("--counterfactual", type=Path, default=Path("data/final/agent_prm_counterfactuals_rl.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/final/agent_prm_teacher_labeling_v1.jsonl"))
    args = parser.parse_args()

    real = [json.loads(line) | {"data_source": "real"} for line in args.real.open(encoding="utf-8") if line.strip()]
    grouped = {kind: [] for kind in QUOTAS}
    for line in args.counterfactual.open(encoding="utf-8"):
        if line.strip():
            row = json.loads(line)
            grouped[row["counterfactual_type"]].append(row)
    selected = []
    for kind, quota in QUOTAS.items():
        rows = stable_order(grouped[kind])
        if len(rows) < quota:
            raise ValueError(f"{kind}: only {len(rows)}, need {quota}")
        selected.extend(rows[:quota])
    rows = stable_order(real) + stable_order(selected)
    with args.output.open("w", encoding="utf-8") as target:
        for row in rows:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"real": len(real), "counterfactual": len(selected), "total": len(rows), "quotas": QUOTAS}, ensure_ascii=False))


if __name__ == "__main__":
    main()
