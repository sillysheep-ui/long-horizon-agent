"""核心工作流的离线集成验证；不需要模型 API 或地图 Key。"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from product.orchestrator import AgentWorkflow, _rank_search_items
from product.store import Store


class FakeModel:
    async def complete(self, role, system, user, max_tokens=1200):
        contents = {
            "需求 Agent": '{"departure_city":"上海","destinations":["杭州"],"days":1,"budget":"","preferences":[],"missing_required":[]}',
            "规划 Agent": '{"days":[{"day":1,"city":"杭州","poi_queries":["西湖"]}]}',
            "回答 Agent": '{"summary":"杭州一日游","days":[{"day":1,"city":"杭州","activities":[{"title":"西湖","description":"湖边散步","evidence_id":"d1-p1"},{"title":"不存在的地点","description":"应被过滤","evidence_id":"d1-p1"}]}],"notes":[]}',
        }
        return {"role": role, "provider": "fake", "model": "fake", "content": contents[role]}


class FakeTools:
    async def poi(self, keyword, city, evidence_id):
        return {"id": evidence_id, "kind": "poi", "query": keyword, "city": city,
                "status": "ok", "items": [{"name": "西湖", "address": "杭州"}],
                "source": "测试数据", "simulated": True}


class BrokenAnswerModel(FakeModel):
    async def complete(self, role, system, user, max_tokens=1200):
        response = await super().complete(role, system, user, max_tokens)
        if role == "回答 Agent":
            response["content"] = '{"days": [invalid}'
        return response


class TwoDayModel(FakeModel):
    async def complete(self, role, system, user, max_tokens=1200):
        response = await super().complete(role, system, user, max_tokens)
        if role == "需求 Agent":
            response["content"] = '{"destinations":["杭州"],"days":2}'
        if role == "规划 Agent":
            response["content"] = '{"days":[{"day":1,"city":"杭州","poi_queries":["西湖"]},{"day":2,"city":"杭州","poi_queries":["西湖"]}]}'
        return response


class CountingTools(FakeTools):
    def __init__(self):
        self.calls = 0

    async def poi(self, keyword, city, evidence_id):
        self.calls += 1
        return await super().poi(keyword, city, evidence_id)


class ProductTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_version_and_evidence_guard(self):
        first = self.store.create_trip("从上海去杭州玩一天")
        revised = self.store.revise(first["trip_id"], "希望看西湖")
        self.assertEqual(self.store.job_status(first["job_id"]), "superseded")
        job = self.store.claim()
        self.assertEqual(job["id"], revised["job_id"])
        self.assertIn("希望看西湖", job["query"])
        result = asyncio.run(AgentWorkflow(self.store, FakeModel(), FakeTools()).run(job))
        self.assertEqual(result["status"], "complete")
        self.assertEqual([activity["title"] for activity in result["days"][0]["activities"]], ["西湖"])
        self.store.finish(job, result)
        self.assertEqual(self.store.trip(first["trip_id"])["result"]["summary"], "杭州一日游")

    def test_broken_answer_uses_evidence_fallback(self):
        self.store.create_trip("从上海去杭州玩一天")
        job = self.store.claim()
        result = asyncio.run(AgentWorkflow(self.store, BrokenAnswerModel(), FakeTools()).run(job))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["days"][0]["activities"][0]["title"], "西湖")
        self.assertIn("保守方案", result["notes"][0])

    def test_duplicate_queries_share_one_tool_call(self):
        self.store.create_trip("从上海去杭州玩两天")
        tools = CountingTools()
        result = asyncio.run(AgentWorkflow(self.store, TwoDayModel(), tools).run(self.store.claim()))
        self.assertEqual(tools.calls, 1)
        self.assertEqual([e["id"] for e in result["evidence"]], ["d1-p1", "d2-p1"])

    def test_web_result_ranking(self):
        hits = [
            {"title": "CCTV.com-文化频道", "url": "https://cctv.com/a", "snippet": "其他内容"},
            {"title": "西湖风景区", "url": "https://wgly.hangzhou.gov.cn/a"},
            {"title": "杭州西湖风景名胜区_百度百科", "url": "https://baidu.com/a"},
        ]
        items = _rank_search_items(hits, "西湖", "杭州")
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["name"], "西湖风景区")
        self.assertEqual(items[1]["name"], "杭州西湖风景名胜区")


if __name__ == "__main__":
    unittest.main()
