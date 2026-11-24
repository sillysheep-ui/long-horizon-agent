import unittest

from evaluation.product_checks import (
    check_result,
    combined_score,
    hard_pass_rate,
    summarize,
)


def make_day(day, activities, city="杭州"):
    return {"day": day, "city": city, "activities": activities}


def make_activity(title="西湖", start="09:30", evidence_id="d1-p1", cost="未核实"):
    return {"title": title, "start": start, "duration": "2 小时", "description": "沿湖步行",
            "transport": "地铁", "cost": cost, "evidence_id": evidence_id}


def make_result(days, evidence=None):
    return {"status": "complete", "days": days,
            "evidence": evidence if evidence is not None else [{"id": "d1-p1", "status": "ok"}]}


class CheckResultTests(unittest.TestCase):
    def test_all_good(self):
        result = make_result([make_day(1, [make_activity(), make_activity("灵隐寺", "14:00")])])
        checks = check_result({"days": 1}, result)
        self.assertTrue(all(checks.values()), checks)

    def test_clarification_case(self):
        checks = check_result({"expect": "needs_clarification"}, {"status": "needs_clarification", "days": []})
        self.assertEqual(checks, {"clarification": True})

    def test_flags_noisy_title(self):
        result = make_result([make_day(1, [make_activity("杭州龙井村门票价格【携程攻略】")])])
        checks = check_result({"days": 1}, result)
        self.assertFalse(checks["title_clean"])

    def test_flags_duplicate_and_order_and_evidence(self):
        day = [make_activity("西湖", "15:00"), make_activity("西湖", "09:00", "d9-x")]
        result = make_result([make_day(1, day), make_day(2, [make_activity("西湖", "09:00")])])
        checks = check_result({"days": 2}, result)
        self.assertFalse(checks["unique_titles"])
        self.assertFalse(checks["time_order"])
        self.assertFalse(checks["evidence_ok"])
        self.assertFalse(checks["density"])

    def test_flags_missing_days(self):
        checks = check_result({"days": 2}, make_result([make_day(1, [make_activity()])]))
        self.assertFalse(checks["day_count"])

    def test_flags_wrong_city(self):
        result = make_result([make_day(1, [make_activity(), make_activity("灵隐寺", "14:00")], city="苏州")])
        checks = check_result({"days": 1, "cities": ["杭州"]}, result)
        self.assertFalse(checks["city_coverage"])

    def test_accepts_city_with_suffix(self):
        result = make_result([make_day(1, [make_activity(), make_activity("灵隐寺", "14:00")], city="杭州市")])
        checks = check_result({"days": 1, "cities": ["杭州"]}, result)
        self.assertTrue(checks["city_coverage"])


    def test_day_window_caps_activity_count(self):
        three = [make_activity("西湖", "09:30"), make_activity("苏博", "12:00"), make_activity("平江路", "15:00")]
        result = make_result([make_day(1, three)])
        result["constraints"] = {"day_windows": [{"day": 1, "hours": 6.0}]}
        checks = check_result({"days": 1, "query": "苏州只有半天"}, result)
        self.assertFalse(checks["density"])

    def test_day_window_allows_two_activities(self):
        two = [{**make_activity("拙政园", "09:30"), "duration": "3 小时"},
               {**make_activity("苏博", "13:00"), "duration": "2.5 小时"}]
        result = make_result([make_day(1, two)])
        result["constraints"] = {"day_windows": [{"day": 1, "hours": 6.0}]}
        checks = check_result({"days": 1, "query": "苏州只有半天"}, result)
        self.assertTrue(checks["density"], checks)
        self.assertTrue(checks["day_load"], checks)

    def test_flags_fabricated_cost(self):
        result = make_result([make_day(1, [make_activity(cost="门票 60 元"), make_activity("灵隐寺", "14:00")])],
                             evidence=[{"id": "d1-p1", "status": "ok", "items": [{"name": "西湖"}]}])
        checks = check_result({"days": 1}, result)
        self.assertFalse(checks["no_fabricated_cost"])

    def test_passes_cost_found_in_evidence(self):
        result = make_result([make_day(1, [make_activity(cost="60 元"), make_activity("灵隐寺", "14:00")])],
                             evidence=[{"id": "d1-p1", "status": "ok",
                                        "items": [{"name": "西湖", "snippet": "门票 60 元"}]}])
        checks = check_result({"days": 1}, result)
        self.assertTrue(checks["no_fabricated_cost"])

    def test_flags_title_not_in_evidence(self):
        result = make_result([make_day(1, [make_activity("某个不存在的地方"), make_activity("灵隐寺", "14:00")])],
                             evidence=[{"id": "d1-p1", "status": "ok", "items": [{"name": "西湖"}]}])
        checks = check_result({"days": 1}, result)
        self.assertFalse(checks["title_matches_evidence"])

    def test_accepts_abbreviated_title(self):
        result = make_result([make_day(1, [make_activity("熊猫基地"), make_activity("灵隐寺", "14:00")])],
                             evidence=[{"id": "d1-p1", "status": "ok",
                                        "items": [{"name": "成都大熊猫繁育研究基地"}, {"name": "灵隐寺"}]}])
        checks = check_result({"days": 1}, result)
        self.assertTrue(checks["title_matches_evidence"])


class ScoreTests(unittest.TestCase):
    def test_hard_pass_rate(self):
        self.assertEqual(hard_pass_rate({"a": True, "b": False}), 0.5)
        self.assertEqual(hard_pass_rate({}), 0.0)

    def test_combined_score(self):
        checks = {"a": True, "b": True}
        self.assertEqual(combined_score(checks, 0.5), 0.8)
        self.assertEqual(combined_score(checks, 1.0, fabrication=True), 0.8)
        self.assertIsNone(combined_score(checks, None))


class SummarizeTests(unittest.TestCase):
    def test_aggregates(self):
        records = [
            {"checks": {"complete": True, "density": True}, "judge": {"rubric": 0.8}, "score": 0.92, "elapsed": 10},
            {"checks": {"complete": False, "density": True}, "judge": {"rubric": 0.5}, "score": 0.5, "elapsed": 20},
        ]
        summary = summarize(records)
        self.assertEqual(summary["check_pass_rate"]["complete"], 0.5)
        self.assertEqual(summary["all_checks_passed"], 1)
        self.assertEqual(summary["hard_mean"], 0.75)
        self.assertEqual(summary["rubric_mean"], 0.65)
        self.assertEqual(summary["score_mean"], 0.71)
        self.assertEqual(summary["latency_mean"], 15.0)


if __name__ == "__main__":
    unittest.main()


def test_opening_hours_flags_closed_activity():
    evidence = {"items": [{"name": "灵隐寺", "opentime": "06:30", "endtime": "17:45"}]}
    result = {
        "status": "complete",
        "evidence": [{"id": "d1-p1", **evidence}],
        "constraints": {"days": 1},
        "days": [{"day": 1, "city": "杭州", "activities": [
            {"title": "灵隐寺", "start": "17:00", "duration": "2 小时", "evidence_id": "d1-p1"}]}],
        "plan": {"days": [{"day": 1, "city": "杭州"}]},
    }
    checks = check_result({"query": "杭州一天", "days": 1, "cities": ["杭州"]}, result)
    assert checks["opening_hours"] is False


def test_opening_hours_passes_inside_window():
    result = {
        "status": "complete",
        "evidence": [{"id": "d1-p1", "items": [{"name": "灵隐寺", "opentime": "06:30", "endtime": "17:45"}]}],
        "constraints": {"days": 1},
        "days": [{"day": 1, "city": "杭州", "activities": [
            {"title": "灵隐寺", "start": "09:00", "duration": "2 小时", "evidence_id": "d1-p1"}]}],
        "plan": {"days": [{"day": 1, "city": "杭州"}]},
    }
    checks = check_result({"query": "杭州一天", "days": 1, "cities": ["杭州"]}, result)
    assert checks["opening_hours"] is True


def test_opening_hours_skipped_without_times():
    result = {
        "status": "complete",
        "evidence": [{"id": "d1-p1", "items": [{"name": "灵隐寺"}]}],
        "constraints": {"days": 1},
        "days": [{"day": 1, "city": "杭州", "activities": [
            {"title": "灵隐寺", "start": "09:00", "duration": "2 小时", "evidence_id": "d1-p1"}]}],
        "plan": {"days": [{"day": 1, "city": "杭州"}]},
    }
    checks = check_result({"query": "杭州一天", "days": 1, "cities": ["杭州"]}, result)
    assert "opening_hours" not in checks


def test_opening_hours_skips_placeholder_window():
    result = {
        "status": "complete",
        "evidence": [{"id": "d1-p1", "items": [
            {"name": "玉林四巷街道", "opentime": "00:00", "endtime": "00:00"}]}],
        "constraints": {"days": 1},
        "days": [{"day": 1, "city": "成都", "activities": [
            {"title": "玉林四巷街道", "start": "14:00", "duration": "1.5 小时", "evidence_id": "d1-p1"}]}],
        "plan": {"days": [{"day": 1, "city": "成都"}]},
    }
    checks = check_result({"query": "成都一天", "days": 1, "cities": ["成都"]}, result)
    assert "opening_hours" not in checks
