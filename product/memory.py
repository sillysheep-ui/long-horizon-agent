"""会话记忆：预算内保留原文，超出预算后把早期消息压缩成摘要。"""

import json
from typing import Any

from product.model_gateway import parse_json

CONTEXT_CHAR_BUDGET = 4000
KEEP_RECENT_MESSAGES = 6
SUMMARY_MAX_CHARS = 400
MESSAGE_CLIP = 200

SUMMARY_SYSTEM_PROMPT = (
    "你在压缩一段出行规划对话，供后续轮次继续使用。只保留会影响后续决策的信息："
    "出发地、目的地、天数、预算、同行人、偏好与禁忌、已经确定的安排、尚未解决的问题。"
    "不要复述寒暄，不要编造对话里没有的内容。"
    '输出 JSON：{"summary": "不超过 300 字的要点，分号分隔"}'
)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text[:limit]


def compact_message(message: dict[str, Any], clip: int = MESSAGE_CLIP) -> dict[str, str]:
    return {"role": str(message.get("role") or ""), "content": _clip(message.get("content"), clip)}


def fallback_summary(messages: list[dict[str, Any]]) -> str:
    """模型不可用时用用户原话兜底，保证早期约束还能带进后续轮次。"""
    parts = [_clip(message.get("content"), 80) for message in messages if message.get("role") == "user"]
    return "；".join(part for part in parts if part)[:SUMMARY_MAX_CHARS]


async def summarize_history(model: Any, previous: str | None, messages: list[dict[str, Any]],
                            calls: list[dict[str, Any]] | None = None) -> str:
    payload = {
        "previous": previous or "",
        "messages": [compact_message(message, clip=400) for message in messages],
    }
    try:
        response = await model.complete("摘要 Agent", SUMMARY_SYSTEM_PROMPT,
                                        json.dumps(payload, ensure_ascii=False), max_tokens=600)
        if calls is not None:
            calls.append({key: response[key] for key in ("role", "provider", "model") if key in response})
        summary = str(parse_json(response["content"]).get("summary") or "").strip()
    except (ValueError, RuntimeError, KeyError):
        summary = ""
    if not summary:
        summary = fallback_summary(messages)
    return summary[:SUMMARY_MAX_CHARS]


async def build_history(model: Any, store: Any, conversation_id: str,
                        exclude_job: str | None = None,
                        calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """预算内返回全部消息；超出后只返回最近几条 + 早期消息的滚动摘要。"""
    messages = [message for message in store.conversation_messages(conversation_id)
                if not (exclude_job and message.get("job_id") == exclude_job)]
    total = sum(len(str(message.get("content") or "")) for message in messages)
    if total <= CONTEXT_CHAR_BUDGET:
        return {"summary": None, "messages": [compact_message(message) for message in messages]}

    cached = store.conversation_summary(conversation_id)
    older = messages[:-KEEP_RECENT_MESSAGES]
    recent = messages[-KEEP_RECENT_MESSAGES:]
    pending = [message for message in older if int(message.get("id") or 0) > int(cached["watermark"])]
    summary = cached["summary"]
    if pending:
        summary = await summarize_history(model, summary, pending, calls)
        store.set_conversation_summary(conversation_id, summary, int(pending[-1]["id"]))
    return {"summary": summary, "messages": [compact_message(message) for message in recent]}
