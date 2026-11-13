import unittest

from evaluation.product_checks import check_result, summarize


def make_day(day, activities):
    return {"day": day, "city": "杭州", "activities": activities}


def make_activity(title="西湖", start="09:30", evidence_id="d1-p1"):
    return {"title": title, "start": start, "duration": "2 小时", "description": "沿湖步行",
            "transport": "地铁", "cost": "未核实", "evidence_id": evidence_id}


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
        self.assertTrue(checks["density"] is False)

    def test_flags_missing_days(self):
        checks = check_result({"days": 2}, make_result([make_day(1, [make_activity()])]))
        self.assertFalse(checks["day_count"])


class SummarizeTests(unittest.TestCase):
    def test_aggregates(self):
        records = [
            {"checks": {"complete": True, "density": True}, "judge": {"overall": 4.0}, "elapsed": 10},
            {"checks": {"complete": False, "density": True}, "judge": {"overall": 3.0}, "elapsed": 20},
        ]
        summary = summarize(records)
        self.assertEqual(summary["check_pass_rate"]["complete"], 0.5)
        self.assertEqual(summary["all_checks_passed"], 1)
        self.assertEqual(summary["judge_mean"], 3.5)
        self.assertEqual(summary["latency_mean"], 15.0)


if __name__ == "__main__":
    unittest.main()
