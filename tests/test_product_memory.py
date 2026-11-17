"""会话记忆：预算内保留原文，超出后压缩早期消息。"""

import asyncio
import json

from product.memory import CONTEXT_CHAR_BUDGET, KEEP_RECENT_MESSAGES, build_history
from product.store import Store


class SummaryModel:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def complete(self, role, system, user, max_tokens=1200):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model down")
        return {"content": json.dumps({"summary": "杭州两天，预算 1500，喜欢安静"}),
                "role": role, "provider": "fake", "model": "fake"}


def _seed(store, conversation_id, count, size=500):
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        store.append_message(conversation_id, role, ("第%d轮 " % index) + "杭州" * size)


def test_keeps_all_messages_under_budget(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    conversation_id = store.create_conversation("杭州")["conversation_id"]
    store.append_message(conversation_id, "user", "从上海去杭州玩两天")
    store.append_message(conversation_id, "assistant", "好的，两天围绕西湖")
    model = SummaryModel()

    context = asyncio.run(build_history(model, store, conversation_id))

    assert context["summary"] is None
    assert [message["role"] for message in context["messages"]] == ["user", "assistant"]
    assert model.calls == 0


def test_compresses_older_messages_over_budget(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    conversation_id = store.create_conversation("杭州")["conversation_id"]
    _seed(store, conversation_id, 12)
    messages = store.conversation_messages(conversation_id)
    assert sum(len(message["content"]) for message in messages) > CONTEXT_CHAR_BUDGET
    model = SummaryModel()

    context = asyncio.run(build_history(model, store, conversation_id))

    assert context["summary"] == "杭州两天，预算 1500，喜欢安静"
    assert len(context["messages"]) == KEEP_RECENT_MESSAGES
    assert model.calls == 1
    saved = store.conversation_summary(conversation_id)
    assert saved["watermark"] == messages[-KEEP_RECENT_MESSAGES - 1]["id"]


def test_reuses_summary_until_new_messages(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    conversation_id = store.create_conversation("杭州")["conversation_id"]
    _seed(store, conversation_id, 12)
    model = SummaryModel()

    asyncio.run(build_history(model, store, conversation_id))
    asyncio.run(build_history(model, store, conversation_id))

    assert model.calls == 1


def test_falls_back_to_user_quotes_when_model_fails(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    conversation_id = store.create_conversation("杭州")["conversation_id"]
    _seed(store, conversation_id, 12)
    model = SummaryModel(fail=True)

    context = asyncio.run(build_history(model, store, conversation_id))

    assert context["summary"]
    assert "杭州" in context["summary"]


def test_excludes_current_job_message(tmp_path):
    store = Store(str(tmp_path / "memory.db"))
    conversation_id = store.create_conversation("杭州")["conversation_id"]
    store.append_message(conversation_id, "user", "从上海去杭州玩两天", job_id="job-1")
    store.append_message(conversation_id, "assistant", "好的", job_id=None)
    store.append_message(conversation_id, "user", "第二天换成博物馆", job_id="job-2")

    context = asyncio.run(build_history(SummaryModel(), store, conversation_id, exclude_job="job-2"))

    contents = [message["content"] for message in context["messages"]]
    assert "第二天换成博物馆" not in " ".join(contents)
    assert "从上海去杭州玩两天" in " ".join(contents)
