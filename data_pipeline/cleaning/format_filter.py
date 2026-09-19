# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 规则过滤 
# --------------------------------------------

import argparse
import json
import re
from pathlib import Path
from typing import Any, Optional

from prompts.prompt import COLDSTART_SYSTEM_PROMPT 


TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(\[.*?\])\s*</tool_call>", re.S)
TOOL_RESPONSE_RE = re.compile(r"<tool_response>\s*(.*?)\s*</tool_response>", re.S)
THINK_RE = re.compile(r"<think>(.*?)</think>", re.S)
ANSWER_RE = re.compile(r"<answer>(.*?)</answer>", re.S)

DEFAULT_FAIL_PATTERNS = [
    "Error fetching",
    "Failed to read page",
    "could not be accessed",
    "no information is available",
    "No results found for query",
    "Timeout or error",
    "Invalid request format",
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


def extract_date_and_max_calls(system_content: str) -> tuple[str, str]:
    date_match = re.search(r"当前日期：([^\n]+)", system_content)
    max_match = re.search(r"最大可调用\s*(\d+)\s*轮工具", system_content)
    cur_date = date_match.group(1).strip() if date_match else "__CURRENT_DATE__"
    max_calls = max_match.group(1).strip() if max_match else "13"
    return cur_date, max_calls


def normalize_system_prompt(system_content: str) -> str:
    cur_date, max_calls = extract_date_and_max_calls(system_content)
    return (
        COLDSTART_SYSTEM_PROMPT.replace("__CURRENT_DATE__", cur_date)
        .replace("__MAX_TOOL_CALL__", max_calls)
        .strip()
    )


def normalize_tool_call(call: dict[str, Any]) -> Optional[dict[str, Any]]:
    fn = call.get("function") or {}
    name = fn.get("name")
    if not isinstance(name, str) or not name:
        return None

    args = fn.get("arguments", {})

    return {
        "name": name,
        "arguments": args,
    }


def parse_tag_tool_calls(content: str) -> tuple[list[dict[str, Any]], int, int]:
    tool_calls: list[dict[str, Any]] = []
    parse_fail_blocks = 0

    for block in TOOL_CALL_BLOCK_RE.findall(content):
        try:
            parsed = json.loads(block)
        except Exception:
            parse_fail_blocks += 1
            continue

        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            continue

        for raw_call in parsed:
            if not isinstance(raw_call, dict):
                continue
            tool_calls.append(raw_call)

    return tool_calls, parse_fail_blocks


def clean_answer_content(content: str) -> str:
    match = ANSWER_RE.search(content)
    if not match:
        return ""
    answer = match.group(1).strip()
    if not answer:
        return ""
    return f"<answer>\n{answer}\n</answer>"


def convert_and_clean_conversations(
    conversations: list[dict[str, Any]], normalize_system: bool
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    out: list[dict[str, Any]] = []
    pending_tool_calls: list[str] = []
    stats = {
        "assistant_tool_call_messages": 0,
        "tool_calls_total": 0,
        "tool_messages_total": 0,
        "deleted_assistant_chatter": 0,
        "tool_call_parse_fail_blocks": 0,
        "orphan_tool_responses": 0,
        "missing_tool_responses": 0,
        "system_normalized": 0,
    }

    for turn in conversations:
        role = turn.get("role")
        content = str(turn.get("content", ""))

        match_think = THINK_RE.search(content)
        think_content = match_think.group(1).strip() if match_think else "" 

        if think_content:
            think_content = f"<think>{think_content}</think>\n\n"
        else:
            think_content = ""

        if role == "system":
            new_content = normalize_system_prompt(content) if normalize_system else content
            if new_content != content:
                stats["system_normalized"] += 1
            out.append({"role": "system", "content": new_content})
            continue

        if role == "assistant":
            existing_tool_calls = turn.get("tool_calls")
            if isinstance(existing_tool_calls, list) and existing_tool_calls:
                tool_calls = existing_tool_calls
            else:
                tool_calls, parse_fail = parse_tag_tool_calls(content)
                stats["tool_call_parse_fail_blocks"] += parse_fail

            if tool_calls:
                normalized_calls: list[dict[str, Any]] = []
                for raw_call in tool_calls:
                    normalized = normalize_tool_call(raw_call)
                    if normalized:
                        normalized_calls.append(normalized)
                if not normalized_calls:
                    stats["deleted_assistant_chatter"] += 1
                    continue

                tool_call_content = "\n".join([f"<tool_call>\n{tool_call}\n</tool_call>" for tool_call in normalized_calls])
                out.append(
                    {
                        "role": "assistant",
                        "content": f"{think_content}{tool_call_content}" 
                    }
                )

                stats["assistant_tool_call_messages"] += 1
                stats["tool_calls_total"] += len(normalized_calls)
                pending_tool_calls.extend([tc["name"] for tc in normalized_calls])
                continue

            else:
                answer_content = clean_answer_content(content)
                if answer_content:
                    out.append(
                        {
                            "role": "assistant",
                            "content": f"{think_content}{answer_content}"
                        }
                    )
                else:
                    # 如果assistant 轮即没有tool call也没有answer，那就删掉
                    stats["deleted_assistant_chatter"] += 1
                continue

        if role == "user" and "<tool_response>" in content:
            match_resp = TOOL_RESPONSE_RE.search(content)
            tool_content = match_resp.group(1).strip() if match_resp else content.strip()
            if pending_tool_calls:
                pending_tool_calls.pop(0)
            else:
                stats["orphan_tool_responses"] += 1

            tool_response_content = f"<tool_response>\n{tool_content}\n</tool_response>"
            out.append(
                {
                    "role": "user",
                    "content": tool_response_content
                }
            )
            stats["tool_messages_total"] += 1
            continue

        if role == "user":
            out.append({"role": "user", "content": content})
            continue


    stats["missing_tool_responses"] += len(pending_tool_calls)
    return out, stats


def has_invalid_final_answer(conversations: list[dict[str, Any]]) -> bool:
    for msg in reversed(conversations):
        if msg.get("role") != "assistant":
            continue
        content = str(msg.get("content", ""))
        match = ANSWER_RE.search(content)
        if match:
            return match.group(1).strip() == ""
    return True


def has_empty_tool_message(conversations: list[dict[str, Any]]) -> bool:
    return any(
        msg.get("role") == "tool" and str(msg.get("content", "")).strip() == ""
        for msg in conversations
    )


def has_failed_tool_message(
    conversations: list[dict[str, Any]], patterns: list[str]
) -> bool:
    lower_patterns = [pattern.lower() for pattern in patterns]
    for msg in conversations:
        if msg.get("role") != "user":
            continue
        content = str(msg.get("content", "")).lower()
        if any(pattern in content for pattern in lower_patterns):
            return True
    return False


def should_filter_row(
    conversations: list[dict[str, Any]], mode: str, fail_patterns: list[str]
) -> tuple[bool, dict[str, int]]:
    empty_tool = has_empty_tool_message(conversations)
    failed_tool = has_failed_tool_message(conversations, fail_patterns)
    invalid_answer = has_invalid_final_answer(conversations)

    if mode == "empty_only":
        should_filter = empty_tool
    elif mode == "empty_and_invalid":
        should_filter = empty_tool or invalid_answer
    else:
        should_filter = empty_tool or failed_tool or invalid_answer

    reason = {
        "empty_tool_response": int(empty_tool),
        "failed_tool_response": int(failed_tool),
        "invalid_final_answer": int(invalid_answer),
    }
    return should_filter, reason


def validate_rows(rows: list[dict[str, Any]]) -> dict[str, int]:
    result = {
        "rows": len(rows),
        "system_missing": 0,
        "assistant_residual_tool_call_tags": 0,
        "rows_call_tool_mismatch": 0,
        "rows_no_answer": 0,
        "tool_messages_missing_tool_call_id": 0,
        "tool_messages_without_prior_call": 0,
    }

    for row in rows:
        conversations = row.get("conversations", [])
        if not any(msg.get("role") == "system" for msg in conversations):
            result["system_missing"] += 1

        calls = 0
        tools = 0
        seen_call_ids: set[str] = set()

        for msg in conversations:
            role = msg.get("role")
            content = str(msg.get("content", ""))
            if role == "assistant":
                if "<tool_call>" in content or "</tool_call>" in content:
                    result["assistant_residual_tool_call_tags"] += 1
                tool_calls = msg.get("tool_calls") or []
                if isinstance(tool_calls, list):
                    calls += len(tool_calls)
                    for tool_call in tool_calls:
                        call_id = tool_call.get("id")
                        if isinstance(call_id, str) and call_id:
                            seen_call_ids.add(call_id)
            elif role == "tool":
                tools += 1
                tool_call_id = msg.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    result["tool_messages_missing_tool_call_id"] += 1
                elif tool_call_id not in seen_call_ids:
                    result["tool_messages_without_prior_call"] += 1

        if calls != tools:
            result["rows_call_tool_mismatch"] += 1
        if has_invalid_final_answer(conversations):
            result["rows_no_answer"] += 1

    return result


def clean_dataset(
    rows: list[dict[str, Any]], normalize_system: bool, filter_mode: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cleaned_rows: list[dict[str, Any]] = []
    kept_rows: list[dict[str, Any]] = []

    aggregate = {
        "input_rows": len(rows),
        "converted_rows": 0,
        "filtered_rows": 0,
        "kept_rows": 0,
        "assistant_tool_call_messages": 0,
        "tool_calls_total": 0,
        "tool_messages_total": 0,
        "deleted_assistant_chatter": 0,
        "tool_call_parse_fail_blocks": 0,
        "orphan_tool_responses": 0,
        "missing_tool_responses": 0,
        "system_normalized": 0,
        "rows_with_empty_tool_response": 0,
        "rows_with_failed_tool_response": 0,
        "rows_with_invalid_final_answer": 0,
    }

    for row in rows:
        conversations = row.get("conversations", [])
        converted_conversations, stats = convert_and_clean_conversations(
            conversations, normalize_system=normalize_system
        )
        converted_row = {
            "id": row.get("id"),
            "conversations": converted_conversations,
        }
        cleaned_rows.append(converted_row)
        aggregate["converted_rows"] += 1
        for key in (
            "assistant_tool_call_messages",
            "tool_calls_total",
            "tool_messages_total",
            "deleted_assistant_chatter",
            "tool_call_parse_fail_blocks",
            "orphan_tool_responses",
            "missing_tool_responses",
            "system_normalized",
        ):
            aggregate[key] += stats.get(key, 0)

        should_filter, reason = should_filter_row(
            converted_conversations, mode=filter_mode, fail_patterns=DEFAULT_FAIL_PATTERNS
        )
        aggregate["rows_with_empty_tool_response"] += reason["empty_tool_response"]
        aggregate["rows_with_failed_tool_response"] += reason["failed_tool_response"]
        aggregate["rows_with_invalid_final_answer"] += reason["invalid_final_answer"]

        if should_filter:
            aggregate["filtered_rows"] += 1
        else:
            kept_rows.append(converted_row)

    aggregate["kept_rows"] = len(kept_rows)
    validation = validate_rows(kept_rows)
    return kept_rows, {"stats": aggregate, "validation": validation}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Clean travel Agent data: convert tag tool calls to OpenAI tool_calls, "
            "remove noisy assistant turns, normalize final answers, and filter bad tool outputs."
        )
    )
    parser.add_argument(
        "--input",
        default="data/clean/test.jsonl",
        help="Input jsonl path.",
    )
    parser.add_argument(
        "--output",
        default="data/clean/test_clean_openai_toolcalls.jsonl",
        help="Cleaned output jsonl path.",
    )
    parser.add_argument(
        "--report",
        default="data/clean/test_clean_openai_toolcalls_report.json",
        help="Cleaning report json path.",
    )
    parser.add_argument(
        "--filter-mode",
        #default="strict_practical",
        default="empty_and_invalid",
        choices=["empty_only", "empty_and_invalid", "strict_practical"],
        help=(
            "Filtering mode: empty_only, empty_and_invalid, or strict_practical "
            "(empty/failed tool response or invalid final answer)."
        ),
    )
    parser.add_argument(
        "--keep-original-system",
        action="store_true",
        help="Do not normalize system prompts.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    report_path = Path(args.report)

    rows = load_jsonl(input_path)
    kept_rows, report = clean_dataset(
        rows=rows,
        normalize_system=not args.keep_original_system,
        filter_mode=args.filter_mode,
    )

    write_jsonl(output_path, kept_rows)
    full_report = {
        "input": str(input_path),
        "output": str(output_path),
        "filter_mode": args.filter_mode,
        "fail_patterns": DEFAULT_FAIL_PATTERNS,
        **report,
    }
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(full_report, f, ensure_ascii=False, indent=2)

    print(json.dumps(full_report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

