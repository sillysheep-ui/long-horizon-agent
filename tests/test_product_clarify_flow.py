"""澄清流程：信息不全先追问，用户补充后继续，且不丢原需求。"""

import asyncio
import json
from datetime import UTC, datetime

from product.orchestrator import AgentWorkflow
from product.store import Store


def _evidence(evidence_id, keyword, city):
    return {
        "id": evidence_id,
        "kind": "poi",
        "query": keyword,
        "city": city,
        "status": "ok",
        "source": "高德",
        "source_kind": "map_poi",
        "verification_level": "address_verified",
        "simulated": False,
        "fetched_at": datetime.now(UTC).isoformat(),
        "items": [{"name": city + keyword, "address": city + "某路", "city": city}],
        "reused": False,
    }


class FakeTools:
    def __init__(self):
        self.calls = []

    async def poi(self, keyword, city, evidence_id):
        self.calls.append(evidence_id)
        return _evidence(evidence_id, keyword, city)


class ClarifyModel:
    """第一次缺目的地 → 追问；补充后给出完整需求。"""

    def __init__(self, always_clarify=False):
        self.intake_prompts = []
        self.intake_calls = 0
        self.always_clarify = always_clarify

    async def complete(self, role, system, user, max_tokens=1200):
        if role == "需求 Agent":
            self.intake_prompts.append(user)
            self.intake_calls += 1
            if self.always_clarify or self.intake_calls == 1:
                payload = {"departure_city": "上海", "destinations": [], "days": None,
                           "budget": "1500", "preferences": [], "conflicts": []}
            else:
                payload = {"departure_city": "上海", "destinations": ["杭州"], "days": 2,
                           "budget": "1500", "preferences": ["自然"], "conflicts": []}
            content = json.dumps(payload, ensure_ascii=False)
        elif role == "规划 Agent":
            content = json.dumps({"days": [
                {"day": 1, "city": "杭州", "poi_queries": ["西湖"]},
                {"day": 2, "city": "杭州", "poi_queries": ["西溪湿地"]},
            ]}, ensure_ascii=False)
        else:
            payload = json.loads(user)
            days = []
            for item in payload["evidence"]:
                number = int(item["id"].split("-")[0][1:])
                name = item["items"][0]["name"]
                days.append({"day": number, "city": item["city"], "activities": [{
                    "title": name, "display_title": name, "start": "09:30", "duration": "2 小时",
                    "description": "沿湖走一段", "transport": "地铁", "cost": "未核实",
                    "evidence_id": item["id"]}]})
            content = json.dumps({"summary": "按来源整理", "days": days, "notes": []}, ensure_ascii=False)
        return {"role": role, "provider": "fake", "model": "fake", "content": content}


def _run_first(store, workflow, conversation_id, text):
    created = store.continue_conversation(conversation_id, text)
    job = store.claim()
    result = asyncio.run(workflow.run(job))
    store.finish(job, result)
    store.record_reply(job["id"], result)
    return created, job, result


def test_clarify_then_supplement_completes(tmp_path):
    store = Store(str(tmp_path / "clarify.db"))
    model = ClarifyModel()
    workflow = AgentWorkflow(store, model, FakeTools())
    conversation = store.create_conversation("新对话")

    _, first_job, first = _run_first(
        store, workflow, conversation["conversation_id"], "周末想出去玩两天，预算1500")
    assert first["status"] == "needs_clarification"
    assert "城市" in first["question"]

    messages = store.conversation(conversation["conversation_id"])["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == first["question"]

    created = store.continue_conversation(conversation["conversation_id"], "去杭州，就两天")
    assert created["kind"] == "revision"
    job = store.claim()
    assert job["version"] == 2
    assert job["query"].startswith("周末想出去玩两天，预算1500")
    assert "用户最新修改：去杭州，就两天" in job["query"]
    assert job["previous"]["status"] == "needs_clarification"

    result = asyncio.run(workflow.run(job))
    store.finish(job, result)
    assert result["status"] == "complete"
    assert result["constraints"]["destinations"] == ["杭州"]

    second_prompt = model.intake_prompts[1]
    assert "上一轮追问" in second_prompt
    assert "你想去哪个城市或地区" in second_prompt
    assert "去杭州，就两天" in second_prompt


def test_repeated_clarify_keeps_original_request(tmp_path):
    store = Store(str(tmp_path / "clarify2.db"))
    model = ClarifyModel(always_clarify=True)
    workflow = AgentWorkflow(store, model, FakeTools())
    conversation = store.create_conversation("新对话")

    _, _, first = _run_first(store, workflow, conversation["conversation_id"], "周末想出去玩两天")
    assert first["status"] == "needs_clarification"

    store.continue_conversation(conversation["conversation_id"], "我也不知道去哪")
    job = store.claim()
    result = asyncio.run(workflow.run(job))
    assert result["status"] == "needs_clarification"
    assert "周末想出去玩两天" in model.intake_prompts[1]
    assert "我也不知道去哪" in model.intake_prompts[1]


class MixedDestinationModel(ClarifyModel):
    """把景点混进 destinations，偏好里也有。"""

    async def complete(self, role, system, user, max_tokens=1200):
        if role == "需求 Agent":
            self.intake_prompts.append(user)
            self.intake_calls += 1
            payload = {"departure_city": "", "destinations": ["无锡", "太湖", "灵山"],
                       "days": 2, "budget": "1500", "preferences": ["太湖", "灵山"],
                       "missing_required": [], "conflicts": []}
            return {"role": role, "provider": "fake", "model": "fake",
                    "content": json.dumps(payload, ensure_ascii=False)}
        return await super().complete(role, system, user, max_tokens)


def test_attractions_in_destinations_are_trimmed(tmp_path):
    store = Store(str(tmp_path / "mixed.db"))
    workflow = AgentWorkflow(store, MixedDestinationModel(), FakeTools())
    store.create_trip("无锡两天，想去太湖和灵山，预算 1500 元")
    result = asyncio.run(workflow.run(store.claim()))
    assert result["status"] == "complete"
    assert result["constraints"]["destinations"] == ["无锡"]
