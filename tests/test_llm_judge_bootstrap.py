"""evaluation/llm_judge.py 的纯逻辑测试：配对构造与配对 Bootstrap 区间。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.llm_judge import build_pairs, load_records, paired_bootstrap_ci


class PairedBootstrapTest(unittest.TestCase):
    def test_empty_input_returns_zeros(self):
        self.assertEqual(paired_bootstrap_ci([], n_boot=100), (0.0, 0.0, 0.0))

    def test_constant_differences_collapse_to_a_point(self):
        mean, low, high = paired_bootstrap_ci([0.5] * 40, n_boot=500)
        self.assertAlmostEqual(mean, 0.5, places=6)
        self.assertAlmostEqual(low, 0.5, places=6)
        self.assertAlmostEqual(high, 0.5, places=6)

    def test_all_zero_differences_stay_at_zero(self):
        self.assertEqual(paired_bootstrap_ci([0.0] * 30, n_boot=500), (0.0, 0.0, 0.0))

    def test_interval_brackets_the_mean(self):
        diffs = [0.8, -0.2, 0.5, 1.1, -0.4, 0.3, 0.9, -0.1]
        mean, low, high = paired_bootstrap_ci(diffs, n_boot=2000)
        self.assertLessEqual(low, mean)
        self.assertLessEqual(mean, high)

    def test_same_seed_is_deterministic(self):
        diffs = [0.4, -0.3, 0.9, 0.1, -0.2]
        self.assertEqual(
            paired_bootstrap_ci(diffs, n_boot=500, seed=7),
            paired_bootstrap_ci(diffs, n_boot=500, seed=7),
        )

    def test_wider_alpha_gives_narrower_interval(self):
        diffs = [1.0 if index % 2 else -0.2 for index in range(60)]
        _, low_narrow, high_narrow = paired_bootstrap_ci(diffs, n_boot=3000, alpha=0.5)
        _, low_wide, high_wide = paired_bootstrap_ci(diffs, n_boot=3000, alpha=0.05)
        self.assertLess(high_narrow - low_narrow, high_wide - low_wide)

    def test_clear_signal_excludes_zero(self):
        diffs = [1.0] * 20 + [0.9] * 20
        _, low, _ = paired_bootstrap_ci(diffs, n_boot=2000)
        self.assertGreater(low, 0.0)

    def test_noisy_signal_crosses_zero(self):
        diffs = [1.0 if index % 2 else -0.9 for index in range(40)]
        _, low, high = paired_bootstrap_ci(diffs, n_boot=2000)
        self.assertLess(low, 0.0)
        self.assertGreater(high, 0.0)


class BuildPairsTest(unittest.TestCase):
    @staticmethod
    def _write(path: Path, rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            handle.write("\n")

    def test_pairs_records_by_query(self):
        with TemporaryDirectory() as tmp:
            reference = Path(tmp) / "reference.jsonl"
            prediction = Path(tmp) / "prediction.jsonl"
            self._write(reference, [{"query": "q1", "messages": [{"role": "user"}]},
                                    {"query": "q2", "messages": [{"role": "user"}]}])
            self._write(prediction, [{"query": "q2", "messages": [{"role": "assistant"}]},
                                     {"query": "q1", "messages": [{"role": "assistant"}]}])

            predictions, references, queries = build_pairs(str(reference), str(prediction))
            self.assertEqual(sorted(queries), ["q1", "q2"])
            self.assertEqual(len(predictions), 2)
            self.assertEqual(len(references), 2)

    def test_unmatched_queries_are_dropped(self):
        with TemporaryDirectory() as tmp:
            reference = Path(tmp) / "reference.jsonl"
            prediction = Path(tmp) / "prediction.jsonl"
            self._write(reference, [{"query": "q1", "messages": []}])
            self._write(prediction, [{"query": "others", "messages": []}])
            self.assertEqual(build_pairs(str(reference), str(prediction)), ([], [], []))

    def test_limit_truncates_pairs(self):
        with TemporaryDirectory() as tmp:
            reference = Path(tmp) / "reference.jsonl"
            prediction = Path(tmp) / "prediction.jsonl"
            rows = [{"query": f"q{index}", "messages": []} for index in range(5)]
            self._write(reference, rows)
            self._write(prediction, rows)

            predictions, references, queries = build_pairs(str(reference), str(prediction), limit=2)
            self.assertEqual(len(predictions), 2)
            self.assertEqual(len(references), 2)
            self.assertEqual(len(queries), 2)

    def test_load_records_skips_blank_lines(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            self._write(path, [{"query": "q1", "messages": []}])
            self.assertEqual(list(load_records(str(path))), ["q1"])


if __name__ == "__main__":
    unittest.main()
