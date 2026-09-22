"""evaluation/benchmark_verifier_detection.py 的纯逻辑测试：错误注入。

该基准只注入 Verifier 明确定义应检出的错误，因此每个用例都可以精确断言。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.benchmark_verifier_detection import mutations, plan_dict

ROUTE_QUERY = (
    "请帮我规划一下怎么从汇鑫水族经拱极楼、宣化区世纪广场、若瑟修女院"
    "到察哈尔省民主政府旧址，这几地的顺路路线怎么走？"
)


def make_record(query: str = ROUTE_QUERY, nodes: list[dict] | None = None) -> dict:
    return {
        "query": query,
        "plan": {
            "nodes": nodes
            if nodes is not None
            else [
                {"id": "r1", "tool": "route_planning",
                 "arguments": {"origin": "汇鑫水族", "destination": "察哈尔省民主政府旧址"}},
                {"id": "p1", "tool": "poi_search", "arguments": {"address": "汇鑫水族"}},
            ]
        },
    }


def kinds_of(record: dict) -> list[str]:
    return [name for name, _ in mutations(record)]


class RouteMutationsTest(unittest.TestCase):
    def test_route_entity_merging_and_guessed_coordinates_are_injected(self):
        kinds = kinds_of(make_record())
        self.assertIn("merged_route_entities", kinds)
        self.assertIn("guessed_route_coordinates", kinds)

    def test_merged_mutation_really_merges_entities(self):
        for name, plan in mutations(make_record()):
            if name == "merged_route_entities":
                origin = plan["nodes"][0]["arguments"]["origin"]
                self.assertIn("经", origin)
                return
        self.fail("未生成 merged_route_entities")

    def test_guessed_mutation_replaces_names_with_coordinates(self):
        for name, plan in mutations(make_record()):
            if name == "guessed_route_coordinates":
                arguments = plan["nodes"][0]["arguments"]
                self.assertRegex(arguments["origin"], r"^-?\d+\.\d+,-?\d+\.\d+$")
                self.assertRegex(arguments["destination"], r"^-?\d+\.\d+,-?\d+\.\d+$")
                return
        self.fail("未生成 guessed_route_coordinates")

    def test_no_route_mutation_without_route_node(self):
        record = make_record(nodes=[{"id": "p1", "tool": "poi_search", "arguments": {}}])
        kinds = kinds_of(record)
        self.assertNotIn("merged_route_entities", kinds)
        self.assertNotIn("guessed_route_coordinates", kinds)


class MissingToolMutationTest(unittest.TestCase):
    NODES = [
        {"id": "t", "tool": "train_tickets_search", "arguments": {}},
        {"id": "f", "tool": "flights_search", "arguments": {}},
        {"id": "w", "tool": "weather_search", "arguments": {}},
    ]

    def test_requested_tools_are_removed_one_by_one(self):
        record = make_record(query="明天郑州到哈尔滨高铁和飞机哪个更快", nodes=self.NODES)
        kinds = kinds_of(record)
        self.assertIn("missing_tool:flights_search", kinds)
        self.assertIn("missing_tool:train_tickets_search", kinds)

    def test_only_the_requested_tool_is_flagged(self):
        record = make_record(query="明天郑州到哈尔滨高铁哪个更快", nodes=self.NODES)
        kinds = kinds_of(record)
        self.assertIn("missing_tool:train_tickets_search", kinds)
        self.assertNotIn("missing_tool:flights_search", kinds)

    def test_removing_the_only_node_is_skipped(self):
        record = make_record(
            query="明天郑州到哈尔滨有航班吗",
            nodes=[{"id": "f", "tool": "flights_search", "arguments": {}}],
        )
        self.assertNotIn("missing_tool:flights_search", kinds_of(record))

    def test_tool_not_flagged_when_query_does_not_mention_it(self):
        record = make_record(query="帮我看看明天的天气", nodes=self.NODES)
        kinds = kinds_of(record)
        self.assertNotIn("missing_tool:flights_search", kinds)
        self.assertNotIn("missing_tool:train_tickets_search", kinds)


class FoodMutationTest(unittest.TestCase):
    def test_food_destination_mismatch_is_injected(self):
        record = make_record(
            query="广州坐大巴去顺德寻味，求美食路线图",
            nodes=[{"id": "a", "tool": "around_search",
                    "arguments": {"location": "113.2644,23.1291"}}],
        )
        self.assertIn("food_destination_mismatch", kinds_of(record))

    def test_food_mutation_rewrites_location(self):
        record = make_record(
            query="广州坐大巴去顺德寻味，求美食路线图",
            nodes=[{"id": "a", "tool": "around_search",
                    "arguments": {"location": "113.2644,23.1291"}}],
        )
        for name, plan in mutations(record):
            if name == "food_destination_mismatch":
                self.assertEqual(plan["nodes"][0]["arguments"]["location"], "其他城市")
                return
        self.fail("未生成 food_destination_mismatch")

    def test_no_food_mutation_without_around_search(self):
        record = make_record(
            query="广州坐大巴去顺德寻味，求美食路线图",
            nodes=[{"id": "p", "tool": "poi_search", "arguments": {"address": "顺德"}}],
        )
        self.assertNotIn("food_destination_mismatch", kinds_of(record))


class RecordIsolationTest(unittest.TestCase):
    def test_source_record_is_not_modified(self):
        record = make_record()
        before = plan_dict(record)
        list(mutations(record))
        self.assertEqual(plan_dict(record), before)

    def test_plan_dict_returns_an_independent_copy(self):
        record = make_record()
        plan = plan_dict(record)
        plan["nodes"][0]["arguments"]["origin"] = "被改动"
        self.assertNotEqual(record["plan"]["nodes"][0]["arguments"]["origin"], "被改动")


if __name__ == "__main__":
    unittest.main()
