import unittest

from evaluation.judge_tool_dag import aggregate_bidirectional


class JudgeAggregationTest(unittest.TestCase):
    def test_swaps_positions_before_averaging(self):
        first = {"A": {"relevance": 8, "completeness": 8, "factual_safety": 8, "tool_reasonableness": 8, "clarity": 8, "overall": 8},
                 "B": {"relevance": 6, "completeness": 6, "factual_safety": 6, "tool_reasonableness": 6, "clarity": 6, "overall": 6}}
        second = {"A": {"relevance": 7, "completeness": 7, "factual_safety": 7, "tool_reasonableness": 7, "clarity": 7, "overall": 7},
                  "B": {"relevance": 9, "completeness": 9, "factual_safety": 9, "tool_reasonableness": 9, "clarity": 9, "overall": 9}}
        result = aggregate_bidirectional(first, second)
        self.assertEqual(result["react"]["overall"], 8.5)
        self.assertEqual(result["tool_dag"]["overall"], 6.5)
        self.assertEqual(result["winner"], "react")


if __name__ == "__main__":
    unittest.main()
