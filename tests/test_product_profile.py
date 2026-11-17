"""跨会话用户偏好：抽取校验、规则兜底、合并与注入。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from product.profile import (
    classify_scope,
    extract_profile,
    merge_fact,
    normalize_fact,
    profile_block,
    scan_rules,
    should_extract,
)
from product.store import Store


class QuietModel:
    async def complete(self, role, system, user, max_tokens=1200):
        return {"content": json.dumps({"facts": []}), "role": role, "provider": "fake", "model": "fake"}


def _messages(*texts, start=1):
    return [{"id": start + index, "role": "user", "content": text} for index, text in enumerate(texts)]


def test_normalize_rejects_unknown_key():
    assert normalize_fact({"key": "favorite.color", "value": "蓝", "scope": "durable",
                           "confidence": 0.9, "evidence": "我喜欢蓝色"}, _messages("我喜欢蓝色")) is None


def test_normalize_rejects_fabricated_evidence():
    fact = {"key": "food.avoid", "value": "辣", "scope": "durable",
            "confidence": 0.9, "evidence": "我吃不了辣"}
    assert normalize_fact(fact, _messages("我随便吃点就行")) is None


def test_normalize_drops_trip_scope_but_keeps_durable():
    messages = _messages("我平时不吃辣", "这次预算 1500")
    durable = normalize_fact({"key": "food.avoid", "value": "辣", "scope": "durable",
                              "confidence": 0.9, "evidence": "我平时不吃辣"}, messages)
    trip = normalize_fact({"key": "budget.band", "value": "舒适", "scope": "durable",
                           "confidence": 0.9, "evidence": "这次预算 1500"}, messages)
    assert durable is not None and durable["key"] == "food.avoid"
    assert trip is None


def test_scan_rules_finds_critical_facts():
    facts = scan_rules(_messages("我吃不了辣，而且带老人出行"))
    keys = {fact["key"] for fact in facts}
    assert {"food.avoid", "companion"} <= keys
    assert all(fact["confidence"] == 0.95 for fact in facts)


def test_classify_scope_lowers_temporary_words():
    assert classify_scope("这次预算 1500", "durable") == "trip"
    assert classify_scope("我平时不吃辣", "trip") == "durable"
    assert classify_scope("明天去灵隐寺", "ephemeral") == "trip"


def test_merge_fact_add_noop_update(tmp_path):
    store = Store(str(tmp_path / "facts.db"))
    fact = {"key": "food.avoid", "value": "辣", "confidence": 0.9, "importance": 8,
            "source_message_id": 1}
    assert merge_fact(store, fact) == "added"
    assert merge_fact(store, fact) == "noop"
    changed = {**fact, "value": "辣和香菜", "source_message_id": 3}
    assert merge_fact(store, changed) == "updated"
    saved = store.get_fact("food.avoid")
    assert saved["value"] == "辣和香菜"
    assert saved["prev_value"] == "辣"


def test_extract_profile_batches_and_advances_watermark(tmp_path):
    store = Store(str(tmp_path / "extract.db"))
    conversation_id = store.create_conversation("杭州")["conversation_id"]
    for text in ("我吃不了辣", "从上海去杭州玩两天", "预算 1500"):
        store.append_message(conversation_id, "user", text)

    stats = asyncio.run(extract_profile(QuietModel(), store, conversation_id, batch_size=2))

    assert stats["batches"] == 2
    assert stats["facts"] == 1
    assert store.get_fact("food.avoid")["value"] == "辣"
    assert store.profile_watermark(conversation_id) == store.messages_after(conversation_id)[-1]["id"]
    assert asyncio.run(extract_profile(QuietModel(), store, conversation_id)) == {"facts": 0, "batches": 0}


def test_list_facts_skips_expired(tmp_path):
    store = Store(str(tmp_path / "expired.db"))
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    store.add_fact({"key": "food.avoid", "value": "辣", "confidence": 0.9, "importance": 8,
                    "source_message_id": None, "expires_at": past})
    store.add_fact({"key": "pace", "value": "relaxed", "confidence": 0.9, "importance": 5,
                    "source_message_id": None, "expires_at": future})

    assert [fact["key"] for fact in store.list_facts()] == ["pace"]
    assert len(store.list_facts(include_expired=True)) == 2


def test_profile_block_is_empty_without_facts(tmp_path):
    store = Store(str(tmp_path / "block.db"))
    assert profile_block(store) == ""
    assert profile_block(store, budget_chars=0) == ""


def test_should_extract_after_idle_window():
    now = datetime.now(UTC)
    assert should_extract((now - timedelta(minutes=31)).isoformat(), now) is True
    assert should_extract((now - timedelta(minutes=5)).isoformat(), now) is False
    assert should_extract(None, now) is False
