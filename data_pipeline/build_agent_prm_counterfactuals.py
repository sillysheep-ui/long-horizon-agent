#!/usr/bin/env python3
"""从真实 AgentPRM 状态构造可追溯的反事实困难样本。

这些不是人工真值：`synthetic_supervision` 只表达设计出的干预和预期行为，
后续仍由教师模型复核。不会伪造 POI、路线或天气等旅行事实。
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any


def latest_tool_response(history: list[dict[str, str]]) -> int | None:
    for index in range(len(history) - 1, -1, -1):
        item = history[index]
        if item.get("role") == "user" and "<tool_response>" in item.get("content", ""):
            return index
    return None


def make_variant(example: dict[str, Any], kind: str) -> dict[str, Any] | None:
    result = copy.deepcopy(example)
    result["example_id"] = f"{example['example_id']}::cf_{kind}"
    result["data_source"] = "counterfactual"
    result["counterfactual_type"] = kind
    result.pop("teacher_label", None)
    history = result["state"]["history"]

    if kind == "premature_answer":
        if result["observed_action"] != "continue_tool":
            return None
        result["counterfactual_action"] = "answer"
        result["synthetic_supervision"] = {
            "preferred_action": "continue_tool",
            "process_quality": "low",
            "reason": "原始轨迹尚未结束；此时直接回答会丢失后续已获取的证据。",
        }
    elif kind == "redundant_tool":
        if result["observed_action"] != "continue_tool" or not any(item["role"] == "assistant" for item in history):
            return None
        result["counterfactual_action"] = "continue_tool"
        result["synthetic_supervision"] = {
            "preferred_action": "replan",
            "process_quality": "low",
            "reason": "候选动作为重复调用最近工具，未引入新的信息需求或约束覆盖。",
        }
    elif kind == "tool_failure":
        index = latest_tool_response(history)
        if index is None:
            return None
        history[index]["content"] = "<tool_response>工具调用失败：未返回可用证据。请勿将该次调用视为事实依据。</tool_response>"
        result["counterfactual_action"] = "answer"
        result["synthetic_supervision"] = {
            "preferred_action": "replan",
            "process_quality": "low",
            "reason": "最近的关键工具证据不可用，直接作答会造成无证据断言；应调整查询或重规划。",
        }
    elif kind == "unsupported_terminal":
        if not result["outcome"]["is_terminal"]:
            return None
        index = latest_tool_response(history)
        if index is None:
            return None
        history[index]["content"] = "<tool_response>本轮只返回了部分约束的证据；其余约束尚未核实。</tool_response>"
        result["counterfactual_action"] = "answer"
        result["synthetic_supervision"] = {
            "preferred_action": "continue_tool",
            "process_quality": "medium_low",
            "reason": "最终回答前仍存在未核实约束，应补充检索或明确不确定性。",
        }
    else:
        raise ValueError(f"unknown variant: {kind}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/final/agent_prm_candidates_rl.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/final/agent_prm_counterfactuals_rl.jsonl"))
    args = parser.parse_args()

    counts: dict[str, int] = {}
    with args.input.open(encoding="utf-8") as source, args.output.open("w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue
            example = json.loads(line)
            for kind in ("premature_answer", "redundant_tool", "tool_failure", "unsupported_terminal"):
                variant = make_variant(example, kind)
                if variant is None:
                    continue
                target.write(json.dumps(variant, ensure_ascii=False) + "\n")
                counts[kind] = counts.get(kind, 0) + 1
    print(json.dumps({"output": str(args.output), "variants": counts, "total": sum(counts.values())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
