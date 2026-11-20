"""模型排期有硬伤时，让它自己重排一次。"""

import asyncio
import json
import unittest
from datetime import UTC, datetime

from product.orchestrator import AgentWorkflow
from product.store import Store


def _evidence(evidence_id, keyword, city):
    return {"id": evidence_id, "kind": "poi", "query": keyword, "city": city, "status": "ok",
            "source": "高德", "source_kind": "map_poi", "verification_level": "address_verified",
            "simulated": False, "fetched_at": datetime.now(UTC).isoformat(),
            "items": [{"name": city + keyword, "address": "某路", "city": city}], "reused": False}


class FakeTools:
    async def poi(self, keyword, city, evidence_id):
        return _evidence(evidence_id, keyword, city)


class RepairModel:
    """第一次故意输出重叠日程，收到修正提示后给出正常日程。"""

    def __init__(self):
        self.answer_calls = 0
        self.repair_calls = 0

    async def complete(self, role, system, user, max_tokens=1200):
        if role == "需求 Agent":
            content = {"departure_city": "上海", "destinations": ["杭州"], "days": 1,
                       "budget": "800", "preferences": [], "missing_required": [], "conflicts": []}
        elif role == "规划 Agent":
            content = {"days": [{"day": 1, "city": "杭州", "poi_queries": ["西湖", "灵隐寺"]}]}
        elif role == "行程评审":
            content = {"score": 8, "verdict": "accept", "issues": []}
        else:
            self.answer_calls += 1
            payload = json.loads(user)
            names = [item["items"][0]["name"] for item in payload["evidence"]]
            if "硬伤" in system:
                self.repair_calls += 1
                starts = ["09:30", "13:30"]
            else:
                starts = ["09:30", "10:00"]  # 故意重叠
            content = {"summary": "按来源整理", "notes": [], "days": [{
                "day": 1, "city": "杭州",
                "activities": [{"title": name, "display_title": name, "start": start,
                                "duration": "2 小时", "description": "走一段", "transport": "地铁",
                                "cost": "未核实", "evidence_id": item["id"]}
                               for name, start, item in zip(names, starts, payload["evidence"], strict=False)],
            }]}
        return {"role": role, "provider": "fake", "model": "fake", "content": json.dumps(content, ensure_ascii=False)}


class PlanRepairTests(unittest.TestCase):
    def test_repair_runs_once_and_keeps_valid_plan(self):
        import tempfile

        model = RepairModel()
        with tempfile.TemporaryDirectory() as folder:
            store = Store(folder + "/repair.db")
            workflow = AgentWorkflow(store, model, FakeTools())
            store.create_trip("去杭州玩一天")
            job = store.claim()
            result = asyncio.run(workflow.run(job))
        self.assertEqual(model.answer_calls, 2)
        self.assertEqual(model.repair_calls, 1)
        answer_span = [span for span in result["trace"]["children"] if span["name"] == "answer"][0]
        self.assertTrue(answer_span["attributes"]["plan_repair"]["attempted"])
        starts = [item["start"] for item in result["days"][0]["activities"]]
        self.assertEqual(starts, sorted(starts))
