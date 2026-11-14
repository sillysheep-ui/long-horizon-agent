import unittest

from evaluation.product_checks import check_result
from product.pace import PACE_LIMITS, PACE_RULES, detect_pace


def _case(query: str) -> dict:
    return {"query": query, "days": 1, "cities": ["杭州"]}


def _result(activities: list) -> dict:
    return {
        "status": "complete",
        "days": [{"day": 1, "city": "杭州", "activities": activities}],
        "evidence": [
            {"id": a["evidence_id"], "status": "ok",
             "items": [{"name": a["title"], "snippet": "来源"}]}
            for a in activities
        ],
    }


class DetectPaceTests(unittest.TestCase):
    def test_packed_words(self):
        self.assertEqual(detect_pace("想紧凑一点，多逛几个地方"), "packed")
        self.assertEqual(detect_pace("特种兵式打卡"), "packed")

    def test_relaxed_words(self):
        self.assertEqual(detect_pace("带爸妈，轻松一点"), "relaxed")
        self.assertEqual(detect_pace("不要太赶，慢慢玩"), "relaxed")

    def test_negated_packed_is_relaxed(self):
        self.assertEqual(detect_pace("不要太紧凑，留点空"), "relaxed")

    def test_default_normal(self):
        self.assertEqual(detect_pace("从上海去杭州玩两天"), "normal")
        self.assertEqual(detect_pace(""), "normal")

    def test_rules_cover_all_levels(self):
        for level in PACE_LIMITS:
            self.assertIn(level, PACE_RULES)


class PaceAwareChecksTests(unittest.TestCase):
    def _activity(self, title: str, start: str, duration: str, index: int) -> dict:
        return {"title": title, "start": start, "duration": duration,
                "evidence_id": f"d1-p{index}", "description": "看展", "transport": "地铁"}

    def test_packed_day_with_two_activities_fails_density(self):
        activities = [self._activity("西湖", "09:00", "3 小时", 1),
                      self._activity("灵隐寺", "13:00", "3 小时", 2)]
        checks = check_result(_case("想紧凑一点，多逛几个地方"), _result(activities))
        self.assertFalse(checks["density"])

    def test_packed_day_with_three_activities_passes(self):
        activities = [self._activity("西湖", "09:00", "2 小时", 1),
                      self._activity("灵隐寺", "12:00", "2 小时", 2),
                      self._activity("河坊街", "16:00", "2 小时", 3)]
        checks = check_result(_case("想紧凑一点，多逛几个地方"), _result(activities))
        self.assertTrue(checks["density"])
        self.assertTrue(checks["day_load"])

    def test_relaxed_activity_length_requires_longer_stays(self):
        activities = [self._activity("西湖", "09:30", "0.5 小时", 1),
                      self._activity("龙井村", "13:30", "0.5 小时", 2)]
        checks = check_result(_case("轻松一点，慢慢玩"), _result(activities))
        self.assertFalse(checks["activity_length"])

    def test_relaxed_day_with_three_activities_passes_limits(self):
        activities = [self._activity("西湖", "09:30", "1.5 小时", 1),
                      self._activity("咖啡馆", "13:00", "1.5 小时", 2),
                      self._activity("书店", "16:00", "1 小时", 3)]
        checks = check_result(_case("轻松一点，慢慢玩"), _result(activities))
        self.assertTrue(checks["density"])
        self.assertTrue(checks["day_load"])


if __name__ == "__main__":
    unittest.main()
