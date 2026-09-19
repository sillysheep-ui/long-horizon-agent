import asyncio
import json
import time
import unittest

from inference.tool_dag import (
    ToolDAGError,
    ToolDAGScheduler,
    ToolNode,
    ToolPlan,
    detect_tool_business_error,
    resolve_references,
    extract_json_object,
)


class ToolPlanTest(unittest.TestCase):
    def test_repairs_python_style_json(self):
        repaired = extract_json_object(
            "{'goal': 'x', 'nodes': [{'id': 'a', 'tool': 'search', 'arguments': {},}],}"
        )
        self.assertEqual(repaired["nodes"][0]["id"], "a")

    def test_rejects_cycle(self):
        with self.assertRaises(ToolDAGError):
            ToolPlan.from_dict({
                "goal": "cycle",
                "nodes": [
                    {"id": "a", "tool": "search", "arguments": {}, "depends_on": ["b"]},
                    {"id": "b", "tool": "visit", "arguments": {}, "depends_on": ["a"]},
                ],
            })

    def test_rejects_unknown_tool(self):
        with self.assertRaises(ToolDAGError):
            ToolPlan.from_dict(
                {"nodes": [{"id": "a", "tool": "unknown", "arguments": {}}]},
                allowed_tools={"search"},
            )

    def test_ticket_tools_use_slow_timeout_without_retry(self):
        plan = ToolPlan.from_dict({
            "nodes": [{
                "id": "train", "tool": "train_tickets_search", "arguments": {},
                "timeout_seconds": 15, "retries": 1,
            }]
        })
        self.assertEqual(plan.nodes[0].timeout_seconds, 45.0)
        self.assertEqual(plan.nodes[0].retries, 0)

    def test_empty_numeric_fields_use_safe_defaults(self):
        node = ToolNode.from_dict({
            "id": "poi", "tool": "poi_search", "arguments": {},
            "timeout_seconds": "", "retries": "",
        })
        self.assertEqual(node.timeout_seconds, 15.0)
        self.assertEqual(node.retries, 0)

    def test_fits_node_budget_without_breaking_dependencies(self):
        plan = ToolPlan.from_dict({
            "nodes": [
                {"id": "root", "tool": "search", "arguments": {}},
                {"id": "a", "tool": "visit", "arguments": {}, "depends_on": ["root"]},
                {"id": "b", "tool": "visit", "arguments": {}, "depends_on": ["root"]},
                {"id": "c", "tool": "visit", "arguments": {}, "depends_on": ["root"]},
            ]
        }).fit_max_nodes(3)
        self.assertEqual([node.id for node in plan.nodes], ["root", "a", "b"])
        self.assertEqual(plan.repair_actions, ["budget_prune:4->3"])

    def test_resolves_reference_and_json_path(self):
        outputs = {"hotel": json.dumps({"location": {"lnglat": "116.1,39.9"}})}
        arguments = {
            "origin": "{{hotel.location.lnglat}}",
            "label": "酒店坐标={{hotel.location.lnglat}}",
        }
        self.assertEqual(
            resolve_references(arguments, outputs),
            {"origin": "116.1,39.9", "label": "酒店坐标=116.1,39.9"},
        )

    def test_resolves_location_from_markdown_tool_output(self):
        outputs = {"poi": "name: 故宫博物院\nlocation: 116.397, 39.918"}
        self.assertEqual(
            resolve_references({"origin": "{{poi.lng,lat}}"}, outputs),
            {"origin": "116.397,39.918"},
        )

    def test_reference_must_be_declared_as_dependency(self):
        with self.assertRaises(ToolDAGError):
            ToolPlan.from_dict({
                "nodes": [
                    {"id": "hotel", "tool": "hotel", "arguments": {}},
                    {
                        "id": "route",
                        "tool": "route",
                        "arguments": {"origin": "{{hotel.location}}"},
                        "depends_on": [],
                    },
                ]
            })

    def test_prunes_synthesis_visit_and_redundant_around_search(self):
        plan = ToolPlan.from_dict({
            "nodes": [
                {"id": "poi", "tool": "poi_search", "arguments": {"address": "故宫"}},
                {"id": "near", "tool": "around_search", "arguments": {
                    "location": "{{poi.location}}", "radius": 2000, "keyword": "景点"
                }, "depends_on": ["poi"]},
                {"id": "parks", "tool": "around_search", "arguments": {
                    "location": "{{poi.location}}", "radius": 2000, "keyword": "公园"
                }, "depends_on": ["poi"]},
                {"id": "answer", "tool": "visit", "arguments": {
                    "url": ["https://example.com"], "goal": "生成行程建议"
                }, "depends_on": ["near", "parks"]},
            ]
        }).prune_semantic_redundancy()
        self.assertEqual([node.id for node in plan.nodes], ["poi", "near"])

    def test_prunes_unrequested_food_and_ticket_tools(self):
        plan = ToolPlan.from_dict({
            "nodes": [
                {"id": "car", "tool": "around_search", "arguments": {"location": "关佛寺", "keyword": "法拉利4S店"}},
                {"id": "food", "tool": "around_search", "arguments": {"location": "关佛寺", "keyword": "美食"}},
                {"id": "flight", "tool": "flights_search", "arguments": {"from_city": "广州", "to_city": "顺德"}},
                {"id": "train", "tool": "train_tickets_search", "arguments": {"from_city": "广州", "to_city": "顺德"}},
                {"id": "bus", "tool": "route_planning", "arguments": {"origin": "广州", "destination": "顺德", "mode": "transit"}},
            ]
        }).prune_irrelevant_to_query("广州坐大巴去顺德，关佛寺附近有法拉利4S店吗")
        self.assertEqual([node.id for node in plan.nodes], ["car", "bus"])
        self.assertIn("intent_prune:5->2", plan.repair_actions)


class ToolDAGSchedulerTest(unittest.IsolatedAsyncioTestCase):
    def test_detects_business_error_text(self):
        self.assertTrue(detect_tool_business_error(
            "API response error: CUQPS_HAS_EXCEEDED_THE_LIMIT"
        ))
        self.assertEqual(detect_tool_business_error("正常地点查询结果"), "")

    async def test_parallel_level_and_dependency(self):
        calls = []

        async def executor(tool, arguments, node_id):
            calls.append(("start", node_id, time.perf_counter()))
            await asyncio.sleep(0.08)
            calls.append(("end", node_id, time.perf_counter()))
            if node_id == "hotel":
                return json.dumps({"location": "116.1,39.9"})
            return json.dumps({"ok": True, "args": arguments})

        plan = ToolPlan.from_dict({
            "goal": "并发测试",
            "nodes": [
                {"id": "weather", "tool": "weather", "arguments": {"city": "北京"}},
                {"id": "hotel", "tool": "hotel", "arguments": {"city": "北京"}},
                {
                    "id": "route",
                    "tool": "route",
                    "arguments": {"origin": "{{hotel.location}}"},
                    "depends_on": ["hotel"],
                },
            ],
        })
        started = time.perf_counter()
        result = await ToolDAGScheduler(executor, max_concurrency=4).run(plan)
        elapsed = time.perf_counter() - started

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["levels"], [["weather", "hotel"], ["route"]])
        self.assertLess(elapsed, 0.22)  # 三次串行约 0.24s；首层应并发
        route = next(item for item in result["results"] if item["node_id"] == "route")
        self.assertEqual(route["arguments"]["origin"], "116.1,39.9")

    async def test_retry_and_cache(self):
        attempts = 0

        async def executor(tool, arguments, node_id):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary")
            return "ok"

        scheduler = ToolDAGScheduler(executor, enable_cache=True)
        plan = ToolPlan.from_dict({
            "nodes": [
                {"id": "a", "tool": "search", "arguments": {"q": "x"}, "retries": 1},
                {"id": "b", "tool": "search", "arguments": {"q": "x"}},
            ]
        })
        result = await scheduler.run(plan)
        by_id = {item["node_id"]: item for item in result["results"]}
        self.assertEqual(by_id["a"]["attempts"], 2)
        # 同层任务并发启动，但事件循环可能让 b 在 a 重试成功前或后完成
        # 首次缓存检查；两种调度顺序都合法，测试不应依赖协程时序。
        self.assertIn(attempts, (2, 3))

        attempts_before_cache_read = attempts
        second = await scheduler.run(ToolPlan.from_dict({
            "nodes": [{"id": "c", "tool": "search", "arguments": {"q": "x"}}]
        }))
        self.assertTrue(second["results"][0]["cache_hit"])
        self.assertEqual(attempts, attempts_before_cache_read)

    async def test_skips_downstream_after_failure(self):
        async def executor(tool, arguments, node_id):
            if node_id == "a":
                raise RuntimeError("boom")
            return "must not run"

        plan = ToolPlan.from_dict({
            "nodes": [
                {"id": "a", "tool": "search", "arguments": {}},
                {"id": "b", "tool": "visit", "arguments": {}, "depends_on": ["a"]},
            ]
        })
        result = await ToolDAGScheduler(executor).run(plan)
        self.assertEqual([item["status"] for item in result["results"]], ["failed", "skipped"])

    async def test_business_error_output_is_failure(self):
        async def executor(tool, arguments, node_id):
            return "API response error: CUQPS_HAS_EXCEEDED_THE_LIMIT"

        plan = ToolPlan.from_dict({
            "nodes": [{"id": "a", "tool": "search", "arguments": {}}]
        })
        result = await ToolDAGScheduler(executor).run(plan)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()
