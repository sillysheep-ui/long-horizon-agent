import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluation.product_checks import CONSTRAINT_SET, check_details, summarize
from product import static_eval, tool_adapters


def make_result() -> dict:
    return {
        "status": "complete",
        "days": [{
            "day": 1,
            "city": "杭州",
            "activities": [{
                "title": "西湖", "start": "09:30", "duration": "2 小时",
                "transport": "地铁", "cost": "未核实", "evidence_id": "d1-p1",
            }],
            "meals": [],
        }],
        "evidence": [{
            "id": "d1-p1", "status": "ok", "city": "杭州",
            "items": [{"name": "西湖"}],
        }],
    }


class StaticEvalModeTests(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_disabled_by_default(self):
        os.environ.pop("STATIC_EVAL", None)
        self.assertFalse(static_eval.enabled())

    def test_configure_forces_sandbox_provider(self):
        os.environ["STATIC_EVAL"] = "1"
        os.environ["POI_PROVIDER"] = "live"
        with mock.patch("tools.sandbox.available", return_value=True):
            static_eval.configure()
        self.assertEqual(os.environ["POI_PROVIDER"], "sandbox")

    def test_configure_fails_without_snapshot(self):
        os.environ["STATIC_EVAL"] = "1"
        with mock.patch("tools.sandbox.available", return_value=False):
            with self.assertRaises(static_eval.StaticEvalError):
                static_eval.configure()

    def test_web_search_is_blocked_without_network(self):
        os.environ["STATIC_EVAL"] = "1"
        tools = tool_adapters.ResearchTools()
        with mock.patch("product.tool_adapters.search_web") as search:
            evidence = asyncio.run(tools.web_search("西湖", "杭州", "d1-p1"))
        search.assert_not_called()
        self.assertEqual(evidence["status"], "unavailable")
        self.assertIn("静态评测", evidence["reason"])

    def test_poi_always_uses_snapshot_in_static_mode(self):
        os.environ["STATIC_EVAL"] = "1"
        os.environ["POI_PROVIDER"] = "live"
        tools = tool_adapters.ResearchTools()
        tools.amap_key = "should-not-be-used"
        with mock.patch("product.tool_adapters.sandbox_available", return_value=True), \
                mock.patch("product.tool_adapters.search_sandbox", return_value=[{"name": "西湖"}]):
            evidence = asyncio.run(tools.poi("西湖", "杭州", "d1-p1"))
        self.assertEqual(evidence["status"], "ok")
        self.assertEqual(evidence["source_kind"], "sandbox")


class StaticFingerprintTests(unittest.TestCase):
    def test_fingerprint_changes_with_cases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            path.write_text('[{"id": "a"}]', encoding="utf-8")
            first = static_eval.fingerprint(path)
            path.write_text('[{"id": "b"}]', encoding="utf-8")
            second = static_eval.fingerprint(path)
        self.assertNotEqual(first["cases_sha256"], second["cases_sha256"])
        self.assertIn("constraint_set", first)

    def test_fingerprint_stable_for_same_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cases.json"
            path.write_text('[{"id": "a"}]', encoding="utf-8")
            self.assertEqual(static_eval.fingerprint(path)["cases_sha256"],
                             static_eval.fingerprint(path)["cases_sha256"])


class CheckDetailTests(unittest.TestCase):
    def test_unknown_and_not_applicable_are_distinguished(self):
        details = check_details({"days": 1, "cities": ["杭州"]}, make_result())
        self.assertEqual(details["opening_hours"]["status"], "unknown")
        self.assertEqual(details["opening_hours"]["class"], "snapshot")
        self.assertEqual(details["anchor_satisfied"]["status"], "not_applicable")
        self.assertEqual(details["meal_grounded"]["status"], "not_applicable")
        self.assertEqual(details["title_matches_evidence"]["status"], "judged")

    def test_classes_cover_every_constraint(self):
        wanted = {item.id for item in CONSTRAINT_SET.constraints} | {"clarification"}
        from evaluation.product_checks import CHECK_CLASSES

        self.assertEqual(set(CHECK_CLASSES), wanted)

    def test_summarize_reports_examined_and_unknown(self):
        record = {"checks": {"complete": True},
                  "check_details": {"complete": {"class": "static", "status": "judged", "ok": True},
                                    "opening_hours": {"class": "snapshot", "status": "unknown", "ok": None}},
                  "elapsed": 1.0}
        summary = summarize([record])
        self.assertEqual(summary["check_examined"]["complete"], 1)
        self.assertEqual(summary["check_unknown"]["opening_hours"], 1)
        self.assertEqual(summary["check_pass_rate"]["opening_hours"], None)
        self.assertEqual(summary["static_coverage"], 0.5)


if __name__ == "__main__":
    unittest.main()
