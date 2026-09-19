#!/usr/bin/env python3
"""从既有真实问题中挖掘多约束 AgentPRM 训练种子，不生成伪问题。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PATTERNS = {
    "time": r"今天|明天|周末|[0-9]+天|[0-9]+月|早|晚|小时|日期|假期",
    "budget": r"预算|[0-9]+元|花费|价格|便宜|省钱|经济",
    "preference": r"亲子|老人|带娃|情侣|独自|美食|摄影|徒步|自驾|高铁|飞机",
    "contingency": r"如果|若|下雨|雨天|失败|没有|不可|无法|超出|冲突",
    "comparison": r"比较|对比|还是|哪个|优先|更适合",
    "multi_hop": r"途经|中转|先.*再|出发.*到",
}


def query_of(row: dict) -> str:
    for message in row.get("conversations", []):
        content = message.get("content", "")
        if message.get("role") == "user" and "<tool_response>" not in content:
            return content.strip()
    return ""


def features(query: str) -> list[str]:
    return [name for name, pattern in PATTERNS.items() if re.search(pattern, query)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", type=Path, default=[Path("data/final/sft_train.jsonl"), Path("data/final/rl.jsonl")])
    parser.add_argument("--output", type=Path, default=Path("data/final/agent_prm_train_seeds_v2.jsonl"))
    parser.add_argument("--min-features", type=int, default=2)
    args = parser.parse_args()
    selected = []
    for source in args.inputs:
        for line in source.open(encoding="utf-8"):
            if not line.strip():
                continue
            row = json.loads(line)
            query = query_of(row)
            tags = features(query)
            # 至少两类约束，且包含真正会改变决策路径的触发因素。
            dynamic = {"budget", "contingency", "comparison", "multi_hop"}
            if len(tags) >= args.min_features and dynamic.intersection(tags):
                selected.append({"id": row["id"], "query": query, "constraint_tags": tags, "source": source.name,
                                 "selection_note": "真实问题；标签仅用于分层采样，不是过程质量标签"})
    with args.output.open("w", encoding="utf-8") as target:
        for row in selected:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"selected": len(selected), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
