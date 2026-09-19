#!/usr/bin/env python3
"""使用配置的教师模型为 AgentPRM 候选状态生成结构化过程监督标签。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI


SYSTEM = """你是工具型 Agent 的过程监督教师。只能依据给出的状态、工具证据和候选动作判断，不能补充外部事实。
评估目标是下一步行动的可靠性、约束覆盖、事实安全与工具成本。不要因为行动更多就给高分，也不要因为回答/终止就天然扣分。
所有输出必须是一个合法 JSON 对象，不要输出 Markdown。"""


def prompt(task: dict[str, Any]) -> str:
    if task["task_type"] == "action_quality":
        payload = {
            "state": task["state"],
            "proposed_action": task["proposed_action"],
            "proposed_action_content": task["proposed_action_content"],
            "allowed_actions": task["allowed_actions"],
        }
        schema = {
            "process_quality": "1-5 integer",
            "risk_types": ["unsupported_claim|missing_constraint|tool_failure|redundant_action|premature_stop|none"],
            "recommended_action": "one allowed action",
            "confidence": "0-1 number",
            "reason": "<=80 Chinese characters",
        }
        return "评估这个已提出的下一步行动。\n输入：\n" + json.dumps(payload, ensure_ascii=False) + "\n输出格式：\n" + json.dumps(schema, ensure_ascii=False)
    payload = {"state": task["state"], "candidate_actions": task["candidate_actions"]}
    schema = {
        "ranking": ["candidate action indices from best to worst, 0-based"],
        "best_action_index": "0-based integer",
        "risk_types": ["unsupported_claim|missing_constraint|tool_failure|redundant_action|premature_stop|none"],
        "confidence": "0-1 number",
        "reason": "<=80 Chinese characters",
    }
    return "对候选下一步行动排序，必须从最佳到最差列出全部索引。\n输入：\n" + json.dumps(payload, ensure_ascii=False) + "\n输出格式：\n" + json.dumps(schema, ensure_ascii=False)


def parse_json(content: str) -> dict[str, Any]:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("teacher response has no JSON object")
    return json.loads(content[start : end + 1])


async def label_one(client: AsyncOpenAI, model: str, task: dict[str, Any], semaphore: asyncio.Semaphore) -> dict[str, Any]:
    async with semaphore:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt(task)}],
            "temperature": 0,
            "max_tokens": 700,
            "response_format": {"type": "json_object"},
        }
        if "deepseek.com" in os.environ.get("OPENAI_BASE_URL", ""):
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        response = await client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content or ""
        return {"task_id": task["task_id"], "teacher_label": parse_json(content), "teacher_raw": content}


async def main(args: argparse.Namespace) -> None:
    load_dotenv()
    model = args.model or os.environ.get("JUDGE_MODEL_ID") or os.environ.get("LLM_MODEL_ID")
    if not model:
        raise RuntimeError("JUDGE_MODEL_ID or LLM_MODEL_ID is required")
    tasks = [json.loads(line) for line in Path(args.input).open(encoding="utf-8") if line.strip()]
    output = Path(args.output)
    completed = set()
    if output.exists():
        completed = {json.loads(line)["task_id"] for line in output.open(encoding="utf-8") if line.strip()}
    pending = [task for task in tasks if task["task_id"] not in completed]
    client = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"], base_url=os.environ["OPENAI_BASE_URL"], timeout=90, max_retries=2)
    semaphore = asyncio.Semaphore(args.concurrency)
    with output.open("a", encoding="utf-8") as handle:
        for begin in range(0, len(pending), args.batch_size):
            batch = pending[begin : begin + args.batch_size]
            results = await asyncio.gather(*(label_one(client, model, task, semaphore) for task in batch), return_exceptions=True)
            for task, result in zip(batch, results):
                row = result if not isinstance(result, Exception) else {"task_id": task["task_id"], "error": f"{type(result).__name__}: {result}"}
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
            print(json.dumps({"completed": min(begin + len(batch), len(pending)), "pending_total": len(pending)}, ensure_ascii=False), flush=True)
    await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="data/final/agent_prm_project_distillation_v2_tasks.jsonl")
    parser.add_argument("--output", default="data/final/agent_prm_project_distillation_v2_labels.jsonl")
    parser.add_argument("--model", default="")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=20)
    asyncio.run(main(parser.parse_args()))
