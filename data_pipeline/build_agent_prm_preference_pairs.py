#!/usr/bin/env python3
"""将反事实困难状态转为 AgentPRM 的成对过程偏好样本。

每个样本共享同一状态，chosen 是可解释的修复动作，rejected 是已构造的
风险动作。它适用于动作价值排序、DPO 或作为 PRM 的偏好监督；不伪造旅行事实。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ACTION_RATIONALES = {
    "continue_tool": "已有证据尚不足以覆盖用户约束，继续获取缺失事实。",
    "replan": "当前证据链失效或动作无新增信息，应调整查询目标或工具路径。",
}


def to_pair(row: dict) -> dict:
    supervision = row["synthetic_supervision"]
    chosen = supervision["preferred_action"]
    return {
        "pair_id": row["example_id"],
        "trajectory_id": row["trajectory_id"],
        "state": row["state"],
        "chosen": {
            "action": chosen,
            "rationale": ACTION_RATIONALES[chosen],
        },
        "rejected": {
            "action": row["counterfactual_action"],
            "rationale": supervision["reason"],
        },
        "counterfactual_type": row["counterfactual_type"],
        "label_source": "synthetic_pairwise_preference",
        "requires_teacher_review": True,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/final/agent_prm_counterfactuals_rl.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/final/agent_prm_preference_pairs_v1.jsonl"))
    args = parser.parse_args()
    count = 0
    with args.input.open(encoding="utf-8") as source, args.output.open("w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue
            target.write(json.dumps(to_pair(json.loads(line)), ensure_ascii=False) + "\n")
            count += 1
    print(json.dumps({"pairs": count, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
