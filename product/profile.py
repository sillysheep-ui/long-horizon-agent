"""跨会话用户偏好：抽取稳定事实、去重合并、按预算注入。"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from product.model_gateway import call_info, parse_json

# 白名单：模型只能从这些 key 里选，避免抽出没用的特征。
FACT_KEYS: dict[str, str] = {
    "food.avoid": "不吃的食物或口味",
    "food.prefer": "偏好的食物",
    "mobility": "行动限制：轮椅、少走路、恐高、晕车",
    "companion": "同行人：老人、孩子、婴儿",
    "pace": "节奏：relaxed / normal / packed",
    "interest": "兴趣：博物馆、自然、摄影、美食",
    "avoid.place": "明确不想去的地方",
    "budget.band": "常驻预算档位：经济 / 舒适 / 高端",
}

LABELS = {
    "food.avoid": "饮食禁忌",
    "food.prefer": "饮食偏好",
    "mobility": "行动限制",
    "companion": "同行人",
    "pace": "节奏",
    "interest": "兴趣",
    "avoid.place": "不想去",
    "budget.band": "预算习惯",
}

IMPORTANCE = {
    "mobility": 9,
    "food.avoid": 8,
    "companion": 7,
    "budget.band": 6,
    "pace": 5,
    "interest": 4,
    "avoid.place": 6,
    "food.prefer": 4,
}

PROFILE_SYSTEM_PROMPT = (
    "你负责从一段出行对话里抽取值得长期记住的用户偏好。只输出 JSON 对象："
    '{"facts":[{"key":"food.avoid","value":"辣","scope":"durable","confidence":0.9,"evidence":"原话"}]}。'
    "key 只能取：" + "、".join(FACT_KEYS) + "。"
    "scope 判断：换一个城市、换一个时间仍然成立填 durable（不吃辣、恐高、带老人出行习惯）；"
    "只和本次行程有关填 trip（这次预算 1500、这次住西湖边）；具体某天某时段填 ephemeral（明天去灵隐寺）。"
    "拿不准时填 trip，不要填 durable。"
    "evidence 必须逐字摘自对话原话，抽不到原话就不要输出这条。"
    "寒暄、一次性安排、模型推测一律不要。没有可记的就输出 {\"facts\":[]}。"
)

_IDLE_SECONDS = 30 * 60
PENDING_TTL_HOURS = 24
_BATCH_SIZE = 20
_PROFILE_BUDGET_CHARS = 800

_TEMP_MARKERS = ("这次", "本次", "这趟", "今晚", "明天", "后天", "这个周末", "下周", "下个月")
_DURABLE_MARKERS = ("平时", "一直", "从来", "每次", "总是", "我一般", "通常")

_RULES: tuple[tuple[str, re.Pattern[str], str | None], ...] = (
    ("food.avoid", re.compile(r"不吃辣|吃不了辣|不能吃辣|忌辣"), "辣"),
    ("food.avoid", re.compile(r"(?:不吃|吃不了|不能吃|忌口)(海鲜|香菜|牛羊肉|猪肉|牛肉|羊肉)"), None),
    ("food.avoid", re.compile(r"过敏"), "过敏原未说明"),
    ("mobility", re.compile(r"轮椅"), "轮椅"),
    ("mobility", re.compile(r"走不动|腿脚不便|少走路"), "少走路"),
    ("mobility", re.compile(r"恐高"), "恐高"),
    ("mobility", re.compile(r"晕车|晕船"), "晕车"),
    ("companion", re.compile(r"带(?:老人|爸妈|父母)"), "老人"),
    ("companion", re.compile(r"带(?:孩子|娃|小孩|婴儿)"), "孩子"),
    ("pace", re.compile(r"不想太累|别太赶|轻松|慢慢逛|节奏慢"), "relaxed"),
    ("pace", re.compile(r"紧凑|多逛几个|赶一点"), "packed"),
    ("budget.band", re.compile(r"预算(?:有限|不多)|穷游|省着花"), "经济"),
    ("budget.band", re.compile(r"预算充足|不差钱|豪华"), "高端"),
)


def classify_scope(evidence: str, scope: str) -> str:
    """模型给的 scope 再用规则纠偏：临时词降级，长期词升级。"""
    text = str(evidence or "")
    if any(marker in text for marker in _TEMP_MARKERS):
        return "trip"
    if scope not in ("durable", "trip", "ephemeral"):
        return "trip"
    if scope == "trip" and any(marker in text for marker in _DURABLE_MARKERS):
        return "durable"
    return scope


def scan_rules(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """关键类事实用规则兜底，避免模型漏抽。"""
    facts: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        for key, pattern, fixed in _RULES:
            match = pattern.search(content)
            if not match:
                continue
            value = fixed or (match.group(1) if match.groups() else match.group(0))
            facts.append({"key": key, "value": value, "scope": "durable",
                          "confidence": 0.95, "evidence": match.group(0),
                          "source_message_id": message.get("id")})
    return facts


def normalize_fact(raw: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """校验白名单、原话、scope、置信度；不合格的丢弃。"""
    key = str(raw.get("key") or "").strip()
    value = str(raw.get("value") or "").strip()[:40]
    evidence = str(raw.get("evidence") or "").strip()
    if key not in FACT_KEYS or not value or not evidence:
        return None
    texts = [str(message.get("content") or "") for message in messages]
    if not any(evidence in text for text in texts):
        return None
    try:
        confidence = float(raw.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = min(1.0, max(0.0, confidence))
    scope = classify_scope(evidence, str(raw.get("scope") or "trip"))
    if scope != "durable" or confidence < 0.6:
        return None
    source_id = raw.get("source_message_id")
    if source_id is None:
        source_id = next((message.get("id") for message in messages
                          if evidence in str(message.get("content") or "")), None)
    return {"key": key, "value": value, "confidence": round(confidence, 2),
            "importance": IMPORTANCE.get(key, 5), "source_message_id": source_id,
            "scope": scope, "evidence": evidence}


def merge_fact(store: Any, fact: dict[str, Any]) -> str:
    """ADD / UPDATE / NOOP：同 key 新值覆盖，旧值留底；低置信度先挂起等用户确认。"""
    fact = dict(fact)
    fact["status"] = "confirmed" if float(fact["confidence"]) >= 0.85 else "pending"
    existing = store.get_fact(fact["key"])
    if existing is None:
        store.add_fact(fact)
        return "added"
    if str(existing["value"]) == fact["value"]:
        store.touch_fact(fact["key"], fact["confidence"])
        return "noop"
    if existing.get("status") == "confirmed" and fact["status"] == "pending":
        return "noop"
    store.update_fact(fact, prev_value=str(existing["value"]))
    return "updated"


def select_facts(store: Any, budget_chars: int = _PROFILE_BUDGET_CHARS) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    used = 0
    for fact in store.list_facts(status="confirmed"):
        line = profile_line(fact)
        if used + len(line) > budget_chars:
            break
        selected.append(fact)
        used += len(line)
    return selected


def profile_line(fact: dict[str, Any]) -> str:
    return "- %s：%s" % (LABELS.get(fact["key"], fact["key"]), fact["value"])


def profile_block(store: Any, budget_chars: int = _PROFILE_BUDGET_CHARS) -> str:
    facts = select_facts(store, budget_chars)
    if not facts:
        return ""
    return "已知用户长期偏好（仅供参考，与本次明确要求冲突时以本次为准）：\n" + "\n".join(
        profile_line(fact) for fact in facts)


async def _model_facts(model: Any, messages: list[dict[str, Any]], summary: str | None,
                       calls: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    payload = {"summary": summary or "",
               "messages": [{"role": message.get("role"), "content": str(message.get("content") or "")[:600]}
                            for message in messages]}
    try:
        response = await model.complete("画像 Agent", PROFILE_SYSTEM_PROMPT,
                                        json.dumps(payload, ensure_ascii=False), max_tokens=700)
        if calls is not None:
            calls.append(call_info(response))
        data = parse_json(response["content"])
        facts = data.get("facts") if isinstance(data, dict) else None
        return [fact for fact in facts if isinstance(fact, dict)] if isinstance(facts, list) else []
    except (ValueError, RuntimeError, KeyError, TypeError):
        return []


async def extract_profile(model: Any, store: Any, conversation_id: str,
                          batch_size: int = _BATCH_SIZE,
                          calls: list[dict[str, Any]] | None = None) -> dict[str, int]:
    """按水位分批抽取，成功一批推进一次水位；失败不推进，下次重试。"""
    watermark = store.profile_watermark(conversation_id)
    pending = store.messages_after(conversation_id, watermark)
    if not pending:
        return {"facts": 0, "batches": 0}
    summary = store.conversation_summary(conversation_id).get("summary")
    added = batches = 0
    for start in range(0, len(pending), batch_size):
        batch = pending[start:start + batch_size]
        candidates = scan_rules(batch) + await _model_facts(model, batch, summary, calls)
        for raw in candidates:
            fact = normalize_fact(raw, batch)
            if fact is not None and merge_fact(store, fact) != "noop":
                added += 1
        store.set_profile_watermark(conversation_id, int(batch[-1]["id"]))
        batches += 1
        # 摘要随水位推进，让后续批次知道前文。
        if summary is None:
            summary = store.conversation_summary(conversation_id).get("summary")
    return {"facts": added, "batches": batches}


def should_extract(last_message_at: str | None, now: datetime | None = None) -> bool:
    if not last_message_at:
        return False
    try:
        last = datetime.fromisoformat(last_message_at)
    except ValueError:
        return False
    now = now or datetime.now(UTC)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last) >= timedelta(seconds=_IDLE_SECONDS)


_YES = re.compile(r"^(是|对|对的|是的|嗯|嗯嗯|没错|确认|可以|好|好的|记下|记住|没问题)")
_NO = re.compile(r"^(不是|不对|没有|不|错|别记|不用|算了)")


def pending_question(store: Any, asked: bool = False) -> dict[str, Any] | None:
    store.expire_pending_facts(PENDING_TTL_HOURS)
    return store.pending_fact(asked=asked)


def attach_pending(store: Any, message_id: int) -> None:
    """在回复末尾补一句确认，只问一次。"""
    fact = pending_question(store)
    if fact is None:
        return
    label = LABELS.get(fact["key"], fact["key"])
    store.append_to_message(message_id, "\n\n顺便确认一下：您%s是「%s」，对吗？回复“是”或“不是”就行。"
                            % (label, fact["value"]))
    store.mark_fact_asked(fact["key"])


def handle_confirmation(store: Any, conversation_id: str, content: str) -> dict[str, Any] | None:
    """识别对上一条确认的“是/不是”。"""
    fact = store.pending_fact(asked=True)
    if fact is None:
        return None
    text = content.strip()
    if _YES.match(text):
        store.confirm_fact(fact["key"])
        answer = "好的，记下了：%s是「%s」。" % (LABELS.get(fact["key"], fact["key"]), fact["value"])
    elif _NO.match(text):
        store.delete_fact(fact["key"])
        answer = "好的，这条不记了。"
    else:
        return None
    message = store.append_message(conversation_id, "user", content)
    reply = store.append_message(conversation_id, "assistant", answer)
    return {"conversation_id": conversation_id, "kind": "profile_ack",
            "message": message, "reply": reply}
