#!/usr/bin/env python3
"""从已有 Agent 轨迹构建 AgentPRM 的过程状态数据集。

不调用任何外部 API。输出的每一条样本代表 Agent 在某一决策点看到的
状态、实际采取的动作以及该轨迹的最终结果，供后续教师标注或训练使用。
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ACTION_SPACE = [
    "continue_tool",
    "answer",
    "replan",
    "rewrite_answer",
    "ask_clarification",
]


def clean_text(value: str, limit: int) -> str:
    value = re.sub(r"<think>.*?</think>", "", value, flags=re.S).strip()
    return value if len(value) <= limit else value[:limit] + "…"


def get_query(conversations: list[dict[str, Any]]) -> str:
    for message in conversations:
        if message.get("role") == "user" and "<tool_response>" not in message.get("content", ""):
            return clean_text(message.get("content", ""), 1200)
    return ""


def classify_action(content: str) -> str:
    return "continue_tool" if "<tool_call>" in content else "answer"


def serialise_context(messages: list[dict[str, Any]], per_message_limit: int) -> list[dict[str, str]]:
    return [
        {"role": item.get("role", "unknown"), "content": clean_text(item.get("content", ""), per_message_limit)}
        for item in messages
        if item.get("role") != "system"
    ]


def build_examples(record: dict[str, Any], context_turns: int, text_limit: int) -> list[dict[str, Any]]:
    conversations = record.get("conversations", [])
    query = get_query(conversations)
    assistant_positions = [i for i, message in enumerate(conversations) if message.get("role") == "assistant"]
    if not assistant_positions:
        return []

    final_answer = clean_text(conversations[assistant_positions[-1]].get("content", ""), 2400)
    examples: list[dict[str, Any]] = []
    for step_index, position in enumerate(assistant_positions):
        observed = conversations[position].get("content", "")
        action = classify_action(observed)
        preceding = conversations[max(0, position - context_turns) : position]
        examples.append(
            {
                "example_id": f"{record.get('id', 'unknown')}::step_{step_index}",
                "trajectory_id": record.get("id", "unknown"),
                "step_index": step_index,
                "state": {
                    "query": query,
                    "history": serialise_context(preceding, text_limit),
                    "tool_steps_so_far": sum(
                        classify_action(conversations[p].get("content", "")) == "continue_tool"
                        for p in assistant_positions[:step_index]
                    ),
                },
                "observed_action": action,
                "candidate_actions": ACTION_SPACE,
                "observed_action_content": clean_text(observed, text_limit),
                "outcome": {
                    "is_terminal": step_index == len(assistant_positions) - 1,
                    "total_tool_steps": sum(
                        classify_action(conversations[p].get("content", "")) == "continue_tool"
                        for p in assistant_positions
                    ),
                    "final_answer": final_answer,
                },
                "teacher_label": None,
            }
        )
    return examples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("data/final/rl.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("data/final/agent_prm_candidates_rl.jsonl"))
    parser.add_argument("--context-turns", type=int, default=6)
    parser.add_argument("--text-limit", type=int, default=1800)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    trajectory_count = example_count = 0
    with args.input.open(encoding="utf-8") as source, args.output.open("w", encoding="utf-8") as target:
        for line in source:
            if not line.strip():
                continue
            trajectory_count += 1
            examples = build_examples(json.loads(line), args.context_turns, args.text_limit)
            for example in examples:
                target.write(json.dumps(example, ensure_ascii=False) + "\n")
                example_count += 1
    print(json.dumps({"trajectories": trajectory_count, "decision_states": example_count, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
