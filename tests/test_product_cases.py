import json
import unittest
from collections import Counter
from pathlib import Path

CASES = json.loads((Path(__file__).resolve().parents[1] / "evaluation" / "product_cases.json").read_text())
SPLITS = Counter(case.get("split") for case in CASES)


class CaseSetTests(unittest.TestCase):
    def test_expected_size_and_splits(self):
        self.assertEqual(len(CASES), 172)
        self.assertEqual(SPLITS, {"dev": 12, "test": 120, "holdout": 40})

    def test_ids_are_unique(self):
        ids = [case["id"] for case in CASES]
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_case_has_required_fields(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                self.assertIsInstance(case["query"], str)
                self.assertGreaterEqual(len(case["query"]), 8)
                self.assertIn(case["split"], {"dev", "test", "holdout"})
                self.assertIn(case["difficulty"], {"easy", "medium", "hard"})
                self.assertTrue(case.get("tags"))
                self.assertIsInstance(case["tags"], list)

    def test_trip_cases_have_days_and_cities(self):
        for case in CASES:
            if case.get("expect") == "needs_clarification":
                continue
            with self.subTest(case=case["id"]):
                self.assertIsInstance(case["days"], int)
                self.assertTrue(1 <= case["days"] <= 7)
                self.assertTrue(case.get("cities"))
                self.assertTrue(all(isinstance(city, str) and city.strip() for city in case["cities"]))

    def test_clarification_cases_are_marked(self):
        clarify = {case["id"] for case in CASES if case.get("expect") == "needs_clarification"}
        self.assertGreaterEqual(len(clarify), 3)
        self.assertTrue({"ambiguous", "no-destination", "contradictory"} <= clarify)
        for case in CASES:
            if case["id"] in clarify:
                self.assertIn(case.get("days"), (None,))


if __name__ == "__main__":
    unittest.main()
