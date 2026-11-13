import unittest

from evaluation.product_judge import CRITERIA, JUDGE_SYSTEM_PROMPT, fabrication_of, score_of


class ScoreOfTests(unittest.TestCase):
    def test_full_pass(self):
        parsed = {"criteria": {item["id"]: {"pass": True, "reason": "…"} for item in CRITERIA}}
        self.assertEqual(score_of(parsed), 1.0)
        self.assertFalse(fabrication_of(parsed))

    def test_partial(self):
        parsed = {"criteria": {"c1": {"pass": True}, "b2": {"pass": False}}}
        self.assertEqual(score_of(parsed), 0.1)
        self.assertTrue(fabrication_of(parsed))

    def test_missing_criteria(self):
        self.assertIsNone(score_of({}))
        self.assertIsNone(score_of({"criteria": {}}))

    def test_prompt_lists_all_criteria(self):
        for item in CRITERIA:
            self.assertIn(item["id"], JUDGE_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
