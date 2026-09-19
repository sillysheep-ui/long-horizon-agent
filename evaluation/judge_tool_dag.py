# -*- coding: utf-8 -*-
"""对 ReAct 与 Tool DAG 结果做交换位置的双向 LLM Judge。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from statistics import mean
from typing import Any, Dict, List

from inference.tool_dag import extract_json_object


JUDGE_PROMPT = """你是严格、公平的旅行 Agent 评审员。比较同一用户问题下的两个候选结果。
只根据提供的工具轨迹与答案评分，不使用外部知识；不得因为答案更长而给高分。
若答案把模拟票务数据说成实时真实数据，应降低 factual_safety。

逐项给 0-10 分：relevance、completeness、factual_safety、tool_reasonableness、clarity。
overall = relevance*0.30 + completeness*0.25 + factual_safety*0.20 + tool_reasonableness*0.15 + clarity*0.10。
只输出 JSON：
{"A":{"relevance":0,"completeness":0,"factual_safety":0,"tool_reasonableness":0,"clarity":0,"overall":0},
 "B":{"relevance":0,"completeness":0,"factual_safety":0,"tool_reasonableness":0,"clarity":0,"overall":0},
 "winner":"A|B|Tie"}
"""


def load_records(path: str) -> Dict[int, Dict[str, Any]]:
    records: Dict[int, Dict[str, Any]] = {}
    with open(path, encoding="utf-8") as file:
        for line in file:
            if line.strip():
                record = json.loads(line)
                records[int(record["sample_idx"])] = record
    return records


def compact_candidate(record: Dict[str, Any]) -> Dict[str, Any]:
    trajectory = []
    for message in record.get("messages", []):
        if message.get("role") == "tool":
            trajectory.append({
                "tool": message.get("name", ""),
                "result": str(message.get("content", ""))[:1200],
            })
        elif message.get("role") == "assistant" and message.get("tool_calls"):
            trajectory.append({"tool_calls": message["tool_calls"]})
    return {
        "status": record.get("status"),
        "tool_calls": record.get("tool_calls", 0),
        "trajectory": trajectory[:12],
        "answer": str(record.get("prediction", ""))[:6000],
    }


def aggregate_bidirectional(first: Dict[str, Any], second: Dict[str, Any]) -> Dict[str, Any]:
    dimensions = ["relevance", "completeness", "factual_safety", "tool_reasonableness", "clarity", "overall"]
    react = {key: round((float(first["A"][key]) + float(second["B"][key])) / 2, 3) for key in dimensions}
    dag = {key: round((float(first["B"][key]) + float(second["A"][key])) / 2, 3) for key in dimensions}
    winner = "tool_dag" if dag["overall"] > react["overall"] else "react" if react["overall"] > dag["overall"] else "tie"
    return {"react": react, "tool_dag": dag, "winner": winner}


async def judge_direction(client, model: str, query: str, a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    user = json.dumps({"query": query, "candidate_A": a, "candidate_B": b}, ensure_ascii=False)
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": user}],
        "temperature": 0,
        "max_tokens": 1200,
        "response_format": {"type": "json_object"},
    }
    if "deepseek.com" in str(client.base_url):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    response = await client.chat.completions.create(**kwargs)
    return extract_json_object(response.choices[0].message.content)


async def main(args) -> None:
    from dotenv import load_dotenv
    from openai import AsyncOpenAI

    load_dotenv()
    react_records = load_records(args.react_records)
    dag_records = load_records(args.dag_records)
    indices = sorted(set(react_records) & set(dag_records))
    if args.limit:
        indices = indices[:args.limit]
    client = AsyncOpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_BASE_URL"],
        timeout=60,
        max_retries=1,
    )
    semaphore = asyncio.Semaphore(args.concurrency)

    async def judge_index(index: int) -> Dict[str, Any]:
        react = compact_candidate(react_records[index])
        dag = compact_candidate(dag_records[index])
        query = str(react_records[index].get("query", ""))
        async with semaphore:
            first, second = await asyncio.gather(
                judge_direction(client, args.model, query, react, dag),
                judge_direction(client, args.model, query, dag, react),
            )
        return {"sample_idx": index, "query": query, **aggregate_bidirectional(first, second)}

    results = await asyncio.gather(*(judge_index(index) for index in indices), return_exceptions=True)
    valid: List[Dict[str, Any]] = []
    errors = []
    for index, result in zip(indices, results):
        if isinstance(result, Exception):
            errors.append({"sample_idx": index, "error": f"{type(result).__name__}: {result}"})
        else:
            valid.append(result)
    dimensions = ["relevance", "completeness", "factual_safety", "tool_reasonableness", "clarity", "overall"]
    summary = {
        "samples": len(valid),
        "errors": errors,
        "react": {key: round(mean(item["react"][key] for item in valid), 3) for key in dimensions} if valid else {},
        "tool_dag": {key: round(mean(item["tool_dag"][key] for item in valid), 3) for key in dimensions} if valid else {},
        "wins": {
            "react": sum(item["winner"] == "react" for item in valid),
            "tool_dag": sum(item["winner"] == "tool_dag" for item in valid),
            "tie": sum(item["winner"] == "tie" for item in valid),
        },
    }
    target = Path(args.output_dir)
    target.mkdir(parents=True, exist_ok=True)
    (target / "judge_records.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in valid), encoding="utf-8"
    )
    (target / "judge_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    await client.close()


def parse_args():
    parser = argparse.ArgumentParser(description="ReAct vs Tool DAG 双向质量评测")
    parser.add_argument("--react_records", required=True)
    parser.add_argument("--dag_records", required=True)
    parser.add_argument("--output_dir", default="test_output/tool_dag_judge")
    parser.add_argument("--model", default=os.environ.get("JUDGE_MODEL_ID") or os.environ.get("LLM_MODEL_ID", ""))
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))
