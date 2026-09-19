#!/usr/bin/env python3
"""对 ms-swift rollout 服务发送一条真实多轮工具调用请求，并只输出紧凑的诊断摘要。"""

import json
import os
import uuid

import requests

import pytest

try:
    from prompts.prompt import COLDSTART_SYSTEM_PROMPT, STRICT_EVIDENCE_APPENDIX
except ImportError as exc:  # prompts.prompt 缺少该常量时跳过，避免中断整个测试套件
    pytest.skip(f"prompts.prompt 缺少所需常量: {exc}", allow_module_level=True)



def truncate(value, limit=280):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "..."


def main():
    system = COLDSTART_SYSTEM_PROMPT.replace("__CURRENT_DATE__", "2026-08-21").replace(
        "__MAX_TOOL_CALL__", "13"
    ) + STRICT_EVIDENCE_APPENDIX
    payload = {
        "infer_requests": [
            {
                "uuid": str(uuid.uuid4()),
                "messages": [
                    {"role": "system", "content": system},
                    {
                        "role": "user",
                        "content": "请查一下明天杭州西湖附近的天气，并推荐一个附近的博物馆。所有具体事实都要来自工具结果。",
                    },
                ],
            }
        ],
        "request_config": {
            "max_tokens": 1200,
            "temperature": 0,
            "top_p": 1.0,
            "top_k": -1,
            "n": 1,
            "return_details": True,
            "logprobs": True,
        },
    }
    response = requests.post(
        os.getenv("ROLLOUT_URL", "http://127.0.0.1:8001/infer/"),
        json=payload,
        timeout=600,
    )
    print("http_status:", response.status_code)
    response.raise_for_status()
    data = response.json()
    output_path = os.getenv(
        "ROLLOUT_RESULT_PATH",
        "saved/output/ms_swift_multiturn_preflight_20260821/real_rollout_response.json",
    )
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)

    items = data if isinstance(data, list) else data.get("response", data.get("responses", [data]))
    if isinstance(items, dict):
        items = [items]
    for item_index, item in enumerate(items):
        print(f"item[{item_index}] keys:", sorted(item) if isinstance(item, dict) else type(item).__name__)
        if not isinstance(item, dict):
            print("value:", truncate(item))
            continue
        messages = item.get("messages") or item.get("response", {}).get("messages") or []
        print("roles:", [message.get("role") for message in messages])
        for index, message in enumerate(messages):
            print(
                f"message[{index}] {message.get('role')}:",
                truncate(message.get("content") or message.get("tool_calls") or ""),
            )
        infos = item.get("rollout_infos") or item.get("response", {}).get("rollout_infos")
        print("rollout_infos:", truncate(infos, 1000))
        token_turns = item.get("response_token_ids") or item.get("response", {}).get("response_token_ids") or []
        mask_turns = item.get("response_loss_mask") or item.get("response", {}).get("response_loss_mask") or []
        if token_turns and isinstance(token_turns[0], int):
            token_turns = [token_turns]
        if mask_turns and isinstance(mask_turns[0], (int, float)):
            mask_turns = [mask_turns]
        print("response_token_lengths:", [len(turn) for turn in token_turns])
        print("response_mask_lengths:", [len(turn) for turn in mask_turns])
        print("response_mask_sums:", [sum(turn) for turn in mask_turns])
    print("full_result:", output_path)


if __name__ == "__main__":
    main()
