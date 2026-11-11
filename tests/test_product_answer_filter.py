import unittest

from product.orchestrator import match_activity

VALID = {
    "d1-p1": {"杭州西湖风景名胜区", "西溪国家湿地公园"},
}


class MatchActivityTest(unittest.TestCase):
    def test_exact_name_matches(self):
        self.assertEqual(("d1-p1", "杭州西湖风景名胜区"),
                         match_activity({"evidence_id": "d1-p1", "title": "杭州西湖风景名胜区"}, VALID))

    def test_whitespace_and_punctuation_ignored(self):
        self.assertEqual(("d1-p1", "杭州西湖风景名胜区"),
                         match_activity({"evidence_id": "d1-p1", "title": " 杭州西湖风景名胜区 "}, VALID))
        self.assertEqual(("d1-p1", "西溪国家湿地公园"),
                         match_activity({"evidence_id": "d1-p1", "title": "西溪国家湿地公园（东区）"}, VALID))

    def test_title_with_extra_suffix_matches_canonical_name(self):
        matched = match_activity({"evidence_id": "d1-p1", "title": "杭州西湖风景名胜区-断桥残雪"}, VALID)
        self.assertEqual(("d1-p1", "杭州西湖风景名胜区"), matched)

    def test_unknown_place_rejected(self):
        self.assertIsNone(match_activity({"evidence_id": "d1-p1", "title": "杭州灵隐寺"}, VALID))

    def test_wrong_evidence_id_rejected(self):
        self.assertIsNone(match_activity({"evidence_id": "d9-p9", "title": "杭州西湖风景名胜区"}, VALID))

    def test_empty_title_rejected(self):
        self.assertIsNone(match_activity({"evidence_id": "d1-p1", "title": " "}, VALID))


if __name__ == "__main__":
    unittest.main()
