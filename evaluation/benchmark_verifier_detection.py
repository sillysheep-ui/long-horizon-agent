# -*- coding: utf-8 -*-
"""固定 Planner 输出，离线测量 Verifier 的检出率、误报率与计算开销。"""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path

from inference.tool_dag import ToolPlan
from inference.tool_verifier import assess_plan, extract_route_entities


def load_records(path: str):
    with open(path, encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def plan_dict(record):
    return copy.deepcopy(record["plan"])


def mutations(record):
    """仅注入 Verifier 明确定义应检测的错误，避免模糊人工标签。"""
    query = record["query"]
    base = plan_dict(record)
    entities = extract_route_entities(query)
    route_indices = [i for i, node in enumerate(base.get("nodes", [])) if node.get("tool") == "route_planning"]

    if len(entities) >= 3 and route_indices:
        broken = copy.deepcopy(base)
        first = broken["nodes"][route_indices[0]]
        first["arguments"]["origin"] = f"{entities[0]}经{entities[1]}"
        first["arguments"]["destination"] = entities[2]
        yield "merged_route_entities", broken

        guessed = copy.deepcopy(base)
        node = guessed["nodes"][route_indices[0]]
        node["arguments"]["origin"] = "114.1000,40.1000"
        node["arguments"]["destination"] = "114.2000,40.2000"
        yield "guessed_route_coordinates", guessed

    requested = []
    if any(term in query for term in ("飞机", "航班")):
        requested.append("flights_search")
    if any(term in query for term in ("高铁", "火车", "动车")):
        requested.append("train_tickets_search")
    for tool in requested:
        if any(node.get("tool") == tool for node in base.get("nodes", [])):
            broken = copy.deepcopy(base)
            broken["nodes"] = [node for node in broken["nodes"] if node.get("tool") != tool]
            if broken["nodes"]:
                yield f"missing_tool:{tool}", broken

    if any(term in query for term in ("美食", "餐厅", "餐馆", "寻味", "小吃")):
        food_indices = [
            i for i, node in enumerate(base.get("nodes", []))
            if node.get("tool") in {"catering_search", "around_search"}
        ]
        if food_indices:
            broken = copy.deepcopy(base)
            broken["nodes"][food_indices[0]]["arguments"]["location"] = "其他城市"
            yield "food_destination_mismatch", broken


def timed_assess(query, raw_plan, max_nodes):
    plan = ToolPlan.from_dict(raw_plan)
    started = time.perf_counter_ns()
    result = assess_plan(query, plan, max_nodes)
    elapsed_us = (time.perf_counter_ns() - started) / 1000
    return result, elapsed_us


def main(args):
    records = load_records(args.records)
    clean_rows = []
    corrupted_rows = []
    for record in records:
        clean, elapsed = timed_assess(record["query"], plan_dict(record), args.max_nodes)
        clean_rows.append({
            "sample_idx": record.get("sample_idx"), "risk": clean.risk,
            "flagged": clean.risk >= args.threshold, "reasons": clean.reasons,
            "latency_us": elapsed,
        })
        for kind, broken in mutations(record):
            checked, mutation_elapsed = timed_assess(record["query"], broken, args.max_nodes)
            corrupted_rows.append({
                "sample_idx": record.get("sample_idx"), "mutation": kind,
                "risk": checked.risk, "detected": checked.risk >= args.threshold,
                "reasons": checked.reasons, "latency_us": mutation_elapsed,
            })

    fp = sum(row["flagged"] for row in clean_rows)
    tp = sum(row["detected"] for row in corrupted_rows)
    all_latencies = [row["latency_us"] for row in clean_rows + corrupted_rows]
    by_type = {}
    for row in corrupted_rows:
        stat = by_type.setdefault(row["mutation"], {"total": 0, "detected": 0})
        stat["total"] += 1
        stat["detected"] += int(row["detected"])
    summary = {
        "threshold": args.threshold,
        "clean_plans": len(clean_rows),
        "false_positives": fp,
        "false_positive_rate": fp / max(1, len(clean_rows)),
        "corrupted_plans": len(corrupted_rows),
        "detected_corruptions": tp,
        "recall": tp / max(1, len(corrupted_rows)),
        "avg_latency_us": sum(all_latencies) / max(1, len(all_latencies)),
        "by_mutation": by_type,
    }
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "records.jsonl").open("w", encoding="utf-8") as file:
        for row in clean_rows + corrupted_rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")
    lines = [
        "# Verifier 固定计划检出能力消融", "",
        f"- 正常计划：{len(clean_rows)}，误报：{fp}，误报率：{summary['false_positive_rate']:.1%}",
        f"- 注入缺陷：{len(corrupted_rows)}，检出：{tp}，召回率：{summary['recall']:.1%}",
        f"- 单次平均检测耗时：{summary['avg_latency_us']:.1f} μs", "", "## 分类型", "",
        "| 缺陷类型 | 检出/总数 | 召回率 |", "|---|---:|---:|",
    ]
    for kind, stat in by_type.items():
        lines.append(f"| {kind} | {stat['detected']}/{stat['total']} | {stat['detected']/stat['total']:.1%} |")
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--max_nodes", type=int, default=12)
    main(parser.parse_args())
