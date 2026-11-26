import csv
import json
import tempfile
import unittest
from pathlib import Path

from evaluation.calibration import compare, export


def _record(case_id, rubric, failed=None):
    return {
        "id": case_id, "query": "北京两天", "checks": {"complete": True, "day_count": False},
        "judge": {"rubric": rubric, "failed": failed or [], "fabrication": False},
        "result": {"days": [{"day": 1, "city": "北京", "title": "中轴线",
                             "activities": [{"start": "09:00", "title": "故宫博物院", "duration": "3 小时"}],
                             "meals": [{"start": "12:00", "title": "四季民福", "duration": "1 小时"}]}]},
    }


class CalibrationTest(unittest.TestCase):
    def test_export_writes_csv_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "records.json").write_text(json.dumps([_record("c1", 0.8)]))
            (root / "cases.json").write_text(json.dumps(
                [{"id": "c1", "cities": ["北京"], "days": 2, "difficulty": "medium"}]))
            result = export(root / "records.json", root / "cases.json", root / "review")
            self.assertEqual(result["cases"], 1)
            rows = list(csv.DictReader((root / "review" / "review.csv").open()))
            self.assertEqual(rows[0]["id"], "c1")
            self.assertEqual(rows[0]["human_score"], "")
            self.assertIn("故宫博物院", (root / "review" / "review.md").read_text())

    def test_compare_agreement_and_kappa(self):
        records = [_record("a", 0.9), _record("b", 0.5), _record("c", 0.8), _record("d", 0.4)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "records.json").write_text(json.dumps(records))
            with (root / "labels.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["id", "human_score", "human_note"])
                writer.writeheader()
                for cid, score in [("a", 4), ("b", 2), ("c", 4), ("d", 4)]:
                    writer.writerow({"id": cid, "human_score": str(score), "human_note": ""})
            stats = compare(root / "records.json", root / "labels.csv")
        self.assertEqual(stats["n"], 4)
        self.assertEqual(stats["agreement"], 0.75)
        self.assertIsNotNone(stats["kappa"])
        self.assertEqual(stats["human_ok_rate"], 0.75)

    def test_compare_skips_blank_labels(self):
        records = [_record("a", 0.9)]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "records.json").write_text(json.dumps(records))
            with (root / "labels.csv").open("w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["id", "human_score", "human_note"])
                writer.writeheader()
                writer.writerow({"id": "a", "human_score": "", "human_note": ""})
            stats = compare(root / "records.json", root / "labels.csv")
        self.assertEqual(stats["n"], 0)


if __name__ == "__main__":
    unittest.main()
