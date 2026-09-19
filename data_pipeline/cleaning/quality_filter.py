# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 质量打分过滤 
# --------------------------------------------

import argparse
import json_repair
import re
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib import error, request
from dotenv import load_dotenv

from prompts.prompt import DATA_JUDGE_PROMPT 

# 加载环境变量
load_dotenv()


DIMENSION_KEYS = [
    "task_relevance",
    "completeness",
    "factual_safety",
    "tool_use_reasonableness",
    "format_quality",
]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def get_sample_id(row: dict[str, Any]) -> str:
    value = row.get("id")
    if value is None:
        value = row.get("qid")
    return str(value) if value is not None else ""


def extract_question(row: dict[str, Any]) -> str:
    if isinstance(row.get("question"), str):
        return row["question"].strip()
    for msg in row.get("conversations", []):
        if msg.get("role") == "user":
            content = str(msg.get("content", "")).strip()
            if content:
                return content
    return ""


def extract_answer(conversations: list[dict[str, Any]]) -> str:
    for msg in reversed(conversations):
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content", ""))
        if "<answer>" in content and "</answer>" in content:
            return content.split("<answer>", 1)[1].split("</answer>", 1)[0].strip()
    return ""


def summarize_tool_trajectory(conversations: list[dict[str, Any]]) -> dict[str, Any]:
    tool_names  = []
    tools = []
    failed_tool_messages = 0
    num_tool_response = 0

    for msg in conversations:
        if msg.get("role") == "assistant":
            raw_tools = re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", msg["content"])
            if raw_tools:
                try:
                    tool_calls = [json.loads(k) for k in raw_tools]
                except:
                    tool_calls = [json_repair.loads(k) for k in raw_tools]

                tool_names.extend([info["name"] for info in tool_calls])
                tools.append(msg["content"])
        elif msg.get("role") == "user":
            num_tool_response += 1
            content = str(msg.get("content", ""))
            lowered = content.lower()
            if any(
                marker in lowered
                for marker in [
                    "error fetching",
                    "failed",
                    "timeout",
                    "no results found",
                    "invalid request",
                    "could not be accessed",
                ]
            ):
                failed_tool_messages += 1

    return {
        "tool_call_path": tools,
        "num_tool_call": len(tools),
        "num_tool_response": num_tool_response,
        "unique_tool_count": len(set(tool_names)),
        "first_tools": tool_names[0],
        "failed_tool_messages": failed_tool_messages,
    }


def build_prompt(
    question: str,
    answer: str,
    trajectory: dict[str, Any],
) -> str:
    trajectory_str = json.dumps(trajectory, ensure_ascii=False)
    return DATA_JUDGE_PROMPT.replace("__QUESTION__", question).replace("__ANSWER__",
         answer).replace("__TRAJECTORY__", trajectory_str) 


def extract_json_object(text: str) -> str:
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    left = text.find("{")
    right = text.rfind("}")
    if left >= 0 and right >= left:
        return text[left : right + 1]
    return text


def call_chat_completion(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/v1/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a strict data quality judge."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_completion_tokens": 2048,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = request.Request(url=url, data=data, headers=headers, method="POST")

    last_error = ""
    for attempt in range(retries):
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw_body = resp.read().decode("utf-8", errors="replace")
            raw_json = json.loads(raw_body)
            content = (
                raw_json.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                or ""
            )
            judge = json.loads(extract_json_object(content))
            return {"ok": True, "judge": judge, "raw_content": content}
        except error.HTTPError as exc:
            last_error = exc.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"

        if attempt < retries - 1:
            time.sleep(1 + attempt)

    return {"ok": False, "error": last_error}


def judge_one(
    row: dict[str, Any],
    base_url: str,
    api_key: str,
    model: str,
    timeout: int,
    retries: int,
) -> dict[str, Any]:
    conversations = row.get("conversations", [])
    question = extract_question(row)
    answer = extract_answer(conversations)
    trajectory = summarize_tool_trajectory(conversations)
    prompt = build_prompt(question=question, answer=answer, trajectory=trajectory)
    result = call_chat_completion(
        base_url=base_url,
        api_key=api_key,
        model=model,
        prompt=prompt,
        timeout=timeout,
        retries=retries,
    )

    output = {
        "id": get_sample_id(row),
        "question": question,
        "tool_trajectory_summary": trajectory,
    }
    output.update(result)
    return output


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    ok_results = [item for item in results if item.get("ok")]
    failed_results = [item for item in results if not item.get("ok")]
    summary: dict[str, Any] = {
        "total": len(results),
        "success": len(ok_results),
        "failed": len(failed_results),
    }
    if not ok_results:
        return summary

    verdict_counts: dict[str, int] = {}
    overall_scores: list[float] = []
    dimension_sums = {key: 0.0 for key in DIMENSION_KEYS}

    for item in ok_results:
        judge = item.get("judge", {})
        verdict = str(judge.get("verdict", "unknown"))
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        overall_scores.append(float(judge.get("overall_score", 0)))
        dims = judge.get("dimension_scores", {})
        for key in DIMENSION_KEYS:
            dimension_sums[key] += float(dims.get(key, 0))

    summary["overall_avg"] = round(sum(overall_scores) / len(overall_scores), 4)
    summary["verdict_distribution"] = verdict_counts
    summary["dimension_avg"] = {
        key: round(value / len(ok_results), 4) for key, value in dimension_sums.items()
    }
    return summary


def export_filtered(
    input_rows: list[dict[str, Any]],
    judged_results: list[dict[str, Any]],
    output_path: Path,
    keep_verdicts: set[str],
) -> int:
    id_to_row = {get_sample_id(row): row for row in input_rows}
    kept_rows: list[dict[str, Any]] = []
    for item in judged_results:
        if not item.get("ok"):
            continue
        verdict = str(item.get("judge", {}).get("verdict", ""))
        if verdict not in keep_verdicts:
            continue
        row = id_to_row.get(str(item.get("id")))
        if row:
            kept_rows.append(row)

    write_jsonl(output_path, kept_rows)
    return len(kept_rows)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run LLM-as-Judge for cleaned travel Agent training data."
    )
    parser.add_argument(
        "--input",
        default="data/clean/test_clean_openai_toolcalls.jsonl",
        help="Input cleaned jsonl path.",
    )
    parser.add_argument(
        "--output",
        default="data/clean/test_clean_judged_full.jsonl",
        help="Per-sample judged jsonl output.",
    )
    parser.add_argument(
        "--summary-output",
        default="data/clean/test_clean_judged_summary.json",
        help="Summary json output.",
    )
    parser.add_argument(
        "--filtered-output",
        default="data/clean/test_clean_judged_filtered.jsonl",
        help="Filtered data output path.",
    )
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.getenv("OPENAI_API_KEY", ""))
    parser.add_argument("--model", default=os.getenv("JUDGE_MODEL_ID", ""))
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument(
        "--max-samples",
        type=int,
        default=0,
        help="Judge first N samples only. 0 means all.",
    )
    parser.add_argument(
        "--keep-verdicts",
        default="pass,borderline",
        help="Comma-separated verdicts to keep in filtered output.",
    )
    args = parser.parse_args()

    if not args.base_url:
        raise ValueError("Missing --base-url or JUDGE_BASE_URL.")
    if not args.api_key:
        raise ValueError("Missing --api-key or JUDGE_API_KEY.")

    input_path = Path(args.input)
    rows = load_jsonl(input_path)
    if args.max_samples > 0:
        rows = rows[: args.max_samples]

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as executor:
        futures = [
            executor.submit(
                judge_one,
                row,
                args.base_url,
                args.api_key,
                args.model,
                args.timeout,
                args.retries,
            )
            for row in rows
        ]
        for idx, future in enumerate(as_completed(futures), start=1):
            results.append(future.result())
            if idx % 20 == 0 or idx == len(futures):
                print(f"Progress: {idx}/{len(futures)}")

    output_path = Path(args.output)
    summary_path = Path(args.summary_output)
    filtered_path = Path(args.filtered_output)

    write_jsonl(output_path, results)
    summary = summarize_results(results)
    keep_verdicts = {item.strip() for item in args.keep_verdicts.split(",") if item.strip()}
    kept_count = export_filtered(rows, results, filtered_path, keep_verdicts)
    summary["filtered_kept"] = kept_count
    summary["filtered_output"] = str(filtered_path)

    with summary_path.open("w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

