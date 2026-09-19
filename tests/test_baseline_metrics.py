import tempfile
import unittest
from pathlib import Path

from evaluation.baseline_metrics import build_markdown_report, percentile, summarize_records, write_summary_files


class BaselineMetricsTest(unittest.TestCase):
    def test_percentile_interpolation(self):
        self.assertEqual(percentile([100, 200, 300], 0.5), 200)
        self.assertEqual(percentile([100, 200], 0.5), 150)
        self.assertIsNone(percentile([], 0.95))

    def test_summarize_dag_records(self):
        records = [
            {
                "mode": "tool_dag",
                "success": True,
                "prediction": "answer",
                "plan_valid": True,
                "tool_calls": 4,
                "total_latency_ms": 1000,
                "tool_latency_ms": 400,
                "average_parallel_width": 2,
                "parallel_speedup_proxy": 1.8,
            },
            {
                "mode": "tool_dag",
                "success": False,
                "prediction": "",
                "plan_valid": False,
                "tool_calls": 0,
                "total_latency_ms": 2000,
                "tool_latency_ms": None,
                "error_type": "ToolDAGError",
            },
        ]
        summary = summarize_records(records)
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["success_rate"], 0.5)
        self.assertEqual(summary["plan_valid_rate"], 0.5)
        self.assertEqual(summary["latency_ms"]["p50"], 1500)
        self.assertEqual(summary["errors"], {"ToolDAGError": 1})

    def test_report_and_files(self):
        react = [{
            "mode": "react", "success": True, "prediction": "ok",
            "tool_calls": 3, "total_latency_ms": 1200, "tool_latency_ms": None,
        }]
        dag = [{
            "mode": "tool_dag", "success": True, "prediction": "ok", "plan_valid": True,
            "tool_calls": 2, "total_latency_ms": 800, "tool_latency_ms": 300,
            "average_parallel_width": 2, "parallel_speedup_proxy": 1.5,
        }]
        report = build_markdown_report(summarize_records(react), summarize_records(dag))
        self.assertIn("ReAct vs Tool DAG", report)
        self.assertIn("端到端延迟 P95", report)

        with tempfile.TemporaryDirectory() as tmp:
            write_summary_files(tmp, react, dag)
            self.assertTrue((Path(tmp) / "summary.json").exists())
            self.assertTrue((Path(tmp) / "report.md").exists())


if __name__ == "__main__":
    unittest.main()
