import unittest

from inference.tool_dag import ToolPlan
from inference.tool_verifier import VerificationResult, assess_answer_consistency, assess_evidence, assess_plan, build_targeted_evidence_repair, extract_route_entities, merge_plans, should_attempt_evidence_repair


class ToolVerifierTest(unittest.TestCase):
    def test_detects_missing_comparison_tool(self):
        plan = ToolPlan.from_dict({"nodes": [
            {"id": "train", "tool": "train_tickets_search", "arguments": {}},
        ]})
        result = assess_plan("北京到上海高铁和飞机哪个快", plan)
        self.assertGreaterEqual(result.risk, 0.3)
        self.assertIn("flights_search", result.missing_requirements)

    def test_detects_failed_and_empty_evidence(self):
        plan = ToolPlan.from_dict({"nodes": [
            {"id": "poi", "tool": "around_search", "arguments": {"location": "某地"}},
        ]})
        result = assess_evidence("某地附近有什么", plan, {"results": [{
            "node_id": "poi", "status": "success", "output": "No POI data available",
        }]})
        self.assertIn("empty_evidence", result.reasons)

    def test_partial_empty_parallel_search_is_soft_risk(self):
        plan = ToolPlan.from_dict({"nodes": [
            {"id": "fabric", "tool": "around_search", "arguments": {"location": "纪念馆", "keyword": "布艺市场"}},
            {"id": "cloth", "tool": "around_search", "arguments": {"location": "纪念馆", "keyword": "布料市场"}},
        ]})
        result = assess_evidence("纪念馆周边有布艺或布料市场吗", plan, {"results": [
            {"node_id": "fabric", "tool": "around_search", "status": "success", "arguments": {"location": "纪念馆"}, "output": "distance: 100\nname: 窗帘店"},
            {"node_id": "cloth", "tool": "around_search", "status": "success", "arguments": {"location": "纪念馆"}, "output": "No POI data available"},
        ]})
        self.assertIn("partial_empty_evidence", result.reasons)
        self.assertNotIn("empty_evidence", result.reasons)
        self.assertLess(result.risk, 0.3)

    def test_merge_only_adds_novel_calls(self):
        base = ToolPlan.from_dict({"nodes": [
            {"id": "a", "tool": "poi_search", "arguments": {"address": "A"}},
        ]})
        candidate = ToolPlan.from_dict({"nodes": [
            {"id": "a2", "tool": "poi_search", "arguments": {"address": "A"}},
            {"id": "b", "tool": "poi_search", "arguments": {"address": "B"}},
        ]})
        merged = merge_plans(base, candidate, 4)
        self.assertEqual(len(merged.nodes), 2)
        self.assertIn("verifier_merge:+1", merged.repair_actions)

    def test_open_ended_route_does_not_create_fake_entities(self):
        plan = ToolPlan.from_dict({"nodes": [
            {"id": "search", "tool": "search", "arguments": {"query": ["自驾路线"]}},
        ]})
        result = assess_plan("给我推荐一条自驾游路线，旅途中有城市、湖泊和山川", plan)
        self.assertNotIn("route_entity_coverage", result.reasons)

    def test_penalizes_guessed_route_coordinates(self):
        plan = ToolPlan.from_dict({"nodes": [
            {"id": "route", "tool": "route_planning", "arguments": {
                "origin": "114.1,40.1", "destination": "114.2,40.2"
            }},
        ]})
        result = assess_plan("从汇鑫水族经拱极楼到世纪广场怎么走", plan)
        self.assertIn("unresolved_named_locations", result.reasons)

    def test_detects_cross_city_food_destination_mismatch(self):
        plan = ToolPlan.from_dict({"nodes": [
            {"id": "food", "tool": "catering_search", "arguments": {
                "location": "113.2644,23.1291", "keyword": "顺德菜"
            }},
        ]})
        result = assess_plan("广州坐大巴去顺德寻味，求美食路线图", plan)
        self.assertIn("destination_mismatch", result.reasons)

    def test_extracts_ordered_route_intent_without_merging_locations(self):
        query = "请帮我规划一下怎么从汇鑫水族经拱极楼、宣化区世纪广场、若瑟修女院到察哈尔省民主政府旧址，这几地的顺路路线怎么走？"
        self.assertEqual(extract_route_entities(query), [
            "汇鑫水族", "拱极楼", "宣化区世纪广场", "若瑟修女院", "察哈尔省民主政府旧址",
        ])

    def test_detects_merged_origin_even_when_all_entity_names_appear(self):
        plan = ToolPlan.from_dict({"nodes": [
            {"id": "bad", "tool": "route_planning", "arguments": {
                "origin": "汇鑫水族经拱极楼", "destination": "宣化区世纪广场"
            }},
            {"id": "leg2", "tool": "route_planning", "arguments": {
                "origin": "宣化区世纪广场", "destination": "若瑟修女院"
            }},
            {"id": "leg3", "tool": "route_planning", "arguments": {
                "origin": "若瑟修女院", "destination": "察哈尔省民主政府旧址"
            }},
        ]})
        result = assess_plan(
            "从汇鑫水族经拱极楼、宣化区世纪广场、若瑟修女院到察哈尔省民主政府旧址，这几地怎么走？",
            plan,
        )
        self.assertIn("route_edge_mismatch", result.reasons)
        self.assertIn("路线段:汇鑫水族->拱极楼", result.missing_requirements)

    def test_accepts_complete_ordered_route_legs(self):
        plan = ToolPlan.from_dict({"nodes": [
            {"id": "leg1", "tool": "route_planning", "arguments": {"origin": "A地", "destination": "B地"}},
            {"id": "leg2", "tool": "route_planning", "arguments": {"origin": "B地", "destination": "C地"}},
        ]})
        result = assess_plan("从A地经B地到C地怎么走？", plan)
        self.assertNotIn("route_edge_mismatch", result.reasons)

    def test_splits_final_location_joined_by_he(self):
        query = "从大湖口铁道遗址出发，经过Huaxing Park、清泉堂和Gukenghebaoshan Park，最后到秀才步道，有没有方便的路线推荐？"
        self.assertEqual(extract_route_entities(query), [
            "大湖口铁道遗址", "Huaxing Park", "清泉堂", "Gukenghebaoshan Park", "秀才步道",
        ])

    def test_removes_route_grammar_suffix_from_destination(self):
        query = "从慧通寺出发，途径首善公园、站东清真寺，最后到大同市平城区明明水族馆的路线，怎么走最方便啊？"
        self.assertEqual(extract_route_entities(query)[-1], "大同市平城区明明水族馆")

    def test_detects_route_business_failure_in_success_output(self):
        plan = ToolPlan.from_dict({"nodes": [{"id": "route", "tool": "route_planning", "arguments": {}}]})
        result = assess_evidence("从甲地经乙地到丙地怎么走", plan, {"results": [{
            "tool": "route_planning", "status": "success", "output": "[RoutePlanning] 路线地点无法定位: C",
            "arguments": {},
        }]})
        self.assertIn("business_evidence_failure", result.reasons)

    def test_detects_cross_province_route_jump(self):
        plan = ToolPlan.from_dict({"nodes": [{"id": "route", "tool": "route_planning", "arguments": {}}]})
        result = assess_evidence("从甲地经乙地到丙地怎么走", plan, {"results": [{
            "tool": "route_planning", "status": "success", "arguments": {},
            "output": "起点解析为：湖南省长沙市甲地（1,1）\n终点解析为：河北省石家庄市乙地（2,2）\ndistance: 1000000",
        }]})
        self.assertIn("route_geographic_jump", result.reasons)

    def test_detects_time_recommendation_conflict(self):
        result = assess_answer_consistency(
            "明天郑州到哈尔滨高铁和飞机哪个更快",
            "高铁全程约7小时27分。飞机总耗时约5.5小时。结论：推荐高铁，更快。",
        )
        self.assertIn("answer_time_recommendation_conflict", result.reasons)

    def test_only_actionable_evidence_triggers_repair(self):
        self.assertFalse(should_attempt_evidence_repair(
            VerificationResult(0.8, ["business_evidence_failure", "route_geographic_jump"])
        ))
        self.assertTrue(should_attempt_evidence_repair(
            VerificationResult(0.8, ["tool_failure"])
        ))

    def test_empty_nearby_search_expands_radius_once(self):
        plan = ToolPlan.from_dict({"nodes": [{
            "id": "nearby", "tool": "around_search",
            "arguments": {"location": "关佛寺", "radius": 5000, "keyword": "法拉利4S店"},
        }]})
        repair = build_targeted_evidence_repair(plan, {"results": [{
            "node_id": "nearby", "tool": "around_search", "arguments": plan.nodes[0].arguments,
            "output": "No POI data available", "status": "success",
        }]}, 4)
        self.assertIsNotNone(repair)
        self.assertEqual(repair.nodes[0].arguments["radius"], 10000)


if __name__ == "__main__":
    unittest.main()
