"""独立评审：只挑语义问题，不参与生成。"""

import asyncio
import json
import tempfile
import unittest

import product.critic as critic_module
from product.critic import _normalize_issue, review_async
from product.orchestrator import AgentWorkflow
from product.store import Store


class FakeResponse:
    def __init__(self, content):
        self.content = content


class StubModel:
    def __init__(self, payload):
        self.payload = payload
        self.roles = []

    async def complete(self, role, system, user, max_tokens=1200):
        self.roles.append(role)
        return {"role": role, "provider": "fake", "model": "fake",
                "content": json.dumps(self.payload, ensure_ascii=False)}


class CriticUnitTests(unittest.TestCase):
    def setUp(self):
        self._flag = critic_module.CRITIC_ENABLED
        critic_module.CRITIC_ENABLED = True

    def tearDown(self):
        critic_module.CRITIC_ENABLED = self._flag

    def test_normalize_issue_aliases_and_limits(self):
        issue = _normalize_issue({"day": "2", "kind": "repetitive", "detail": "两天都排了西湖"})
        self.assertEqual(issue, {"day": 2, "kind": "repetitive", "detail": "两天都排了西湖"})
        self.assertIsNone(_normalize_issue({"kind": "too_tight"}))
        self.assertIsNone(_normalize_issue("not a dict"))

    def test_review_accepts_when_no_issues(self):
        model = StubModel({"score": 8.5, "verdict": "accept", "issues": []})
        result = asyncio.run(review_async(model, [{"day": 1, "activities": []}], {"days": 1}, []))
        self.assertEqual(result["verdict"], "accept")
        self.assertEqual(result["issues"], [])

    def test_review_flags_and_truncates(self):
        issues = [{"day": 1, "kind": "too_tight", "detail": "第%d个活动太赶" % index}
                  for index in range(8)]
        model = StubModel({"score": 4, "verdict": "accept", "issues": issues})
        result = asyncio.run(review_async(model, [{"day": 1}], {}, []))
        self.assertEqual(result["verdict"], "revise")
        self.assertEqual(len(result["issues"]), 5)

    def test_review_fails_open(self):
        model = StubModel({"unexpected": True})
        result = asyncio.run(review_async(model, [{"day": 1}], {}, []))
        self.assertEqual(result["verdict"], "accept")


def _evidence(evidence_id, keyword, city):
    from datetime import UTC, datetime

    return {"id": evidence_id, "kind": "poi", "query": keyword, "city": city, "status": "ok",
            "source": "高德", "source_kind": "map_poi", "verification_level": "address_verified",
            "simulated": False, "fetched_at": datetime.now(UTC).isoformat(),
            "items": [{"name": city + keyword, "address": "某路", "city": city}], "reused": False}


class FakeTools:
    async def poi(self, keyword, city, evidence_id):
        return _evidence(evidence_id, keyword, city)


class CritiqueModel:
    """第一次评审报问题，触发一次重排；回答被调用两次。"""

    def __init__(self):
        self.answer_calls = 0
        self.critic_calls = 0

    async def complete(self, role, system, user, max_tokens=1200):
        if role == "需求 Agent":
            content = {"departure_city": "上海", "destinations": ["杭州"], "days": 1,
                       "budget": "800", "preferences": [], "missing_required": [], "conflicts": []}
        elif role == "规划 Agent":
            content = {"days": [{"day": 1, "city": "杭州", "poi_queries": ["西湖"]}]}
        elif role == "行程评审":
            self.critic_calls += 1
            if self.critic_calls == 1:
                content = {"score": 4, "verdict": "accept",
                           "issues": [{"day": 1, "kind": "too_tight", "detail": "两个活动之间没留休息"}]}
            else:
                content = {"score": 9, "verdict": "accept", "issues": []}
        else:
            self.answer_calls += 1
            payload = json.loads(user)
            name = payload["evidence"][0]["items"][0]["name"]
            content = {"summary": "按来源整理", "notes": [], "days": [{
                "day": 1, "city": "杭州",
                "activities": [{"title": name, "display_title": name, "start": "09:30",
                                "duration": "2 小时", "description": "走一段", "transport": "地铁",
                                "cost": "未核实", "evidence_id": payload["evidence"][0]["id"]}]}]}
        return {"role": role, "provider": "fake", "model": "fake",
                "content": json.dumps(content, ensure_ascii=False)}


class CriticWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._flag = critic_module.CRITIC_ENABLED
        critic_module.CRITIC_ENABLED = True
        self.model = CritiqueModel()

    def tearDown(self):
        critic_module.CRITIC_ENABLED = self._flag

    def test_critic_issue_triggers_one_repair(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder + "/critic.db")
            workflow = AgentWorkflow(store, self.model, FakeTools())
            store.create_trip("去杭州玩一天")
            job = store.claim()
            result = asyncio.run(workflow.run(job))
        self.assertEqual(self.model.answer_calls, 2)
        span = [item for item in result["trace"]["children"] if item["name"] == "answer"][0]
        self.assertTrue(span["attributes"]["plan_repair"]["attempted"])
        self.assertEqual(span["attributes"]["critique"]["verdict"], "revise")
