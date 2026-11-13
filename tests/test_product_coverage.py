"""按天覆盖度核验与定向补查的单元测试。"""

import asyncio
import unittest
from datetime import datetime, timezone

from inference.tool_dag import ToolDAGScheduler
from product.coverage import assess_days, build_repair_plan
from product.planner import days_to_tool_plan
from product.tool_runtime import ResearchExecutor


def _evidence(evidence_id, keyword, city, status="ok", items=None):
    return {
        "id": evidence_id,
        "kind": "poi",
        "query": keyword,
        "city": city,
        "status": status,
        "source": "高德" if status == "ok" else "地点查询",
        "source_kind": "map_poi" if status == "ok" else "none",
        "verification_level": "address_verified" if status == "ok" else "none",
        "simulated": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "reason": "" if status == "ok" else "查询失败",
        "items": (items if items is not None else [{"name": "西湖"}]) if status == "ok" else [],
        "reused": False,
    }


class FakeTools:
    def __init__(self, broken=()):
        self.calls = []
        self.broken = set(broken)

    async def poi(self, keyword, city, evidence_id):
        self.calls.append((keyword, city, evidence_id))
        if keyword in self.broken:
            raise RuntimeError("查询超时")
        return _evidence(evidence_id, keyword, city)


DAYS = [
    {"day": 1, "city": "杭州", "poi_queries": ["西湖"]},
    {"day": 2, "city": "杭州", "poi_queries": ["灵隐寺"]},
]


class AssessTests(unittest.TestCase):
    def test_covered_days_have_no_risk(self):
        assessment = assess_days(DAYS, [_evidence("d1-p1", "西湖", "杭州"), _evidence("d2-p1", "灵隐寺", "杭州")])
        self.assertEqual(assessment["risk"], 0.0)
        self.assertEqual(assessment["uncovered_days"], [])
        self.assertEqual(assessment["reasons"], [])

    def test_uncovered_day_is_flagged(self):
        assessment = assess_days(DAYS, [_evidence("d1-p1", "西湖", "杭州")])
        self.assertEqual(assessment["uncovered_days"], [2])
        self.assertIn("day_uncovered", assessment["reasons"])
        self.assertEqual(assessment["days"][1]["uncovered"], True)

    def test_failed_evidence_raises_risk(self):
        assessment = assess_days(DAYS, [_evidence("d1-p1", "西湖", "杭州", status="failed")])
        self.assertIn("tool_failure", assessment["reasons"])
        self.assertGreater(assessment["risk"], 0.35)

    def test_empty_items_do_not_count_as_coverage(self):
        assessment = assess_days(DAYS, [_evidence("d1-p1", "西湖", "杭州", items=[])])
        self.assertEqual(assessment["uncovered_days"], [1, 2])


class RepairPlanTests(unittest.TestCase):
    def test_repair_targets_only_uncovered_days(self):
        plan = build_repair_plan(DAYS, [_evidence("d1-p1", "西湖", "杭州")])
        self.assertIsNotNone(plan)
        self.assertEqual([node.id for node in plan.nodes], ["d2-r1"])
        self.assertEqual(plan.nodes[0].arguments, {"city": "杭州", "keyword": "杭州 景点"})

    def test_no_repair_when_all_covered(self):
        evidence = [_evidence("d1-p1", "西湖", "杭州"), _evidence("d2-p1", "灵隐寺", "杭州")]
        self.assertIsNone(build_repair_plan(DAYS, evidence))

    def test_skips_query_already_successful(self):
        evidence = [_evidence("d1-p1", "西湖", "杭州"), _evidence("d2-p1", "杭州 景点", "杭州")]
        self.assertIsNone(build_repair_plan(DAYS, evidence))

    def test_respects_repair_limit(self):
        days = [{"day": index, "city": "杭州", "poi_queries": ["西湖"]} for index in range(1, 6)]
        plan = build_repair_plan(days, [])
        self.assertEqual(len(plan.nodes), 4)


class RepairExecutionTests(unittest.TestCase):
    def test_uncovered_day_is_repaired_once(self):
        days = [dict(day) for day in DAYS]
        plan = days_to_tool_plan(days, 2, ["杭州"])
        executor = ResearchExecutor(FakeTools(broken={"西湖"}))
        scheduler = ToolDAGScheduler(executor=executor, max_concurrency=2, fail_fast=False)
        first = executor.collect(asyncio.run(scheduler.run(plan)), plan)
        assessment = assess_days(days, first)
        self.assertEqual(assessment["uncovered_days"], [1])

        repair = build_repair_plan(days, first)
        second = executor.collect(asyncio.run(scheduler.run(repair)), repair)
        final = assess_days(days, first + second)
        self.assertEqual(final["uncovered_days"], [])
        self.assertEqual([node.id for node in repair.nodes], ["d1-r1"])


if __name__ == "__main__":
    unittest.main()
