"""规划草案编译与 DAG 执行的最小单元测试。"""

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone

from inference.tool_dag import ToolDAGScheduler
from product.planner import days_to_tool_plan
from product.tool_runtime import ResearchExecutor


def _evidence(evidence_id, keyword, city, fetched_at=None, status="ok"):
    return {
        "id": evidence_id,
        "kind": "poi",
        "query": keyword,
        "city": city,
        "status": status,
        "source": "高德",
        "source_kind": "map_poi" if status == "ok" else "none",
        "verification_level": "address_verified" if status == "ok" else "none",
        "simulated": False,
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "reason": "",
        "items": [{"name": "西湖", "address": "杭州市西湖区"}],
        "reused": False,
    }


class FakeTools:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    async def poi(self, keyword, city, evidence_id):
        self.calls.append((keyword, city, evidence_id))
        if keyword in self.fail:
            raise RuntimeError("查询超时")
        return _evidence(evidence_id, keyword, city)


class PlannerTests(unittest.TestCase):
    def test_builds_nodes_in_day_order(self):
        days = [
            {"day": 1, "city": "杭州", "poi_queries": ["西湖", "灵隐寺"]},
            {"day": 2, "city": "杭州", "poi_queries": ["西溪湿地"]},
        ]
        plan = days_to_tool_plan(days, 2, ["杭州"])
        self.assertEqual([node.id for node in plan.nodes], ["d1-p1", "d1-p2", "d2-p1"])
        self.assertEqual(plan.nodes[0].arguments, {"city": "杭州", "keyword": "西湖"})
        self.assertEqual(plan.nodes[0].tool, "poi")
        self.assertEqual(plan.nodes[0].timeout_seconds, 45.0)

    def test_rejects_wrong_day_count(self):
        with self.assertRaises(ValueError) as ctx:
            days_to_tool_plan([{"day": 1, "city": "杭州"}], 2, ["杭州"])
        self.assertIn("天数", str(ctx.exception))

    def test_rejects_non_dict_day(self):
        with self.assertRaises(ValueError):
            days_to_tool_plan(["杭州"], 1, ["杭州"])

    def test_fills_city_and_default_query(self):
        days = [{"day": 1, "poi_queries": []}]
        plan = days_to_tool_plan(days, 1, ["杭州"])
        self.assertEqual(days[0]["city"], "杭州")
        self.assertEqual(plan.nodes[0].arguments["keyword"], "杭州 景点")

    def test_drops_blank_queries(self):
        plan = days_to_tool_plan([{"day": 1, "city": "杭州", "poi_queries": ["", "  "]}], 1, ["杭州"])
        self.assertEqual(len(plan.nodes), 1)
        self.assertEqual(plan.nodes[0].arguments["keyword"], "杭州 景点")

    def test_rejects_plan_over_budget(self):
        days = [{"day": i, "city": "杭州", "poi_queries": ["a", "b", "c"]} for i in range(1, 8)]
        with self.assertRaises(ValueError) as ctx:
            days_to_tool_plan(days, 7, ["杭州"])
        self.assertIn("预算", str(ctx.exception))


class ExecutorTests(unittest.TestCase):
    def test_reuses_fresh_evidence_without_calling_tool(self):
        tools = FakeTools()
        old = _evidence("d9-p9", "西湖", "杭州")
        executor = ResearchExecutor(tools, [old])
        output = asyncio.run(executor("poi", {"city": "杭州", "keyword": "西湖"}, "d1-p1"))
        evidence = json.loads(output)
        self.assertEqual(evidence["id"], "d1-p1")
        self.assertTrue(evidence["reused"])
        self.assertEqual(tools.calls, [])

    def test_refetches_stale_evidence(self):
        tools = FakeTools()
        stale = datetime.now(timezone.utc) - timedelta(days=2)
        executor = ResearchExecutor(tools, [_evidence("d9-p9", "西湖", "杭州", stale.isoformat())])
        output = asyncio.run(executor("poi", {"city": "杭州", "keyword": "西湖"}, "d1-p1"))
        self.assertEqual(json.loads(output)["id"], "d1-p1")
        self.assertEqual(tools.calls, [("西湖", "杭州", "d1-p1")])

    def test_rejects_unknown_tool(self):
        executor = ResearchExecutor(FakeTools())
        with self.assertRaises(ValueError):
            asyncio.run(executor("weather", {"city": "杭州"}, "d1-p1"))

    def test_collect_marks_failed_node(self):
        plan = days_to_tool_plan([{"day": 1, "city": "杭州", "poi_queries": ["西湖"]}], 1, ["杭州"])
        executor = ResearchExecutor(FakeTools())
        dag_result = {"results": [{"node_id": "d1-p1", "status": "failed", "error": "超时"}]}
        evidence = executor.collect(dag_result, plan)
        self.assertEqual(evidence[0]["status"], "failed")
        self.assertEqual(evidence[0]["reason"], "超时")

    def test_collect_rewrites_cached_id(self):
        plan = days_to_tool_plan(
            [{"day": 1, "city": "杭州", "poi_queries": ["西湖", "西湖"]}], 1, ["杭州"]
        )
        executor = ResearchExecutor(FakeTools())
        first = json.dumps(_evidence("d1-p1", "西湖", "杭州"), ensure_ascii=False)
        dag_result = {"results": [{"node_id": "d1-p2", "status": "success", "output": first}]}
        evidence = executor.collect(dag_result, plan)
        self.assertEqual(evidence[0]["id"], "d1-p2")
        self.assertTrue(evidence[0]["reused"])


class SchedulerIntegrationTests(unittest.TestCase):
    def test_runs_nodes_and_keeps_failures(self):
        tools = FakeTools(fail={"灵隐寺"})
        plan = days_to_tool_plan(
            [{"day": 1, "city": "杭州", "poi_queries": ["西湖", "灵隐寺"]}], 1, ["杭州"]
        )
        executor = ResearchExecutor(tools)
        dag_result = asyncio.run(ToolDAGScheduler(executor, max_concurrency=2).run(plan))
        self.assertEqual(dag_result["success_count"], 1)
        self.assertEqual(dag_result["failed_count"], 1)
        evidence = executor.collect(dag_result, plan)
        self.assertEqual([item["status"] for item in evidence], ["ok", "failed"])
        self.assertEqual(evidence[1]["id"], "d1-p2")


if __name__ == "__main__":
    unittest.main()
