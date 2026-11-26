"""静态评测套件：用例规模、冻结范围、数据质量门禁。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from evaluation import data_quality
from evaluation.product_checks import check_details
from evaluation.product_eval import judge_client_for
from evaluation.spec import load_spec, validate

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "evaluation" / "static_cases.json").read_text())


class StaticCasesTest(unittest.TestCase):
    def test_size_and_frozen_holdout(self):
        self.assertEqual(len(CASES), 100)
        self.assertEqual(sum(1 for c in CASES if c["split"] == "test"), 80)
        self.assertEqual(sum(1 for c in CASES if c["split"] == "holdout"), 20)
        self.assertEqual(len({c["id"] for c in CASES}), 100)

    def test_cases_are_well_formed(self):
        cities = {"北京", "上海", "杭州", "成都", "重庆", "广州", "深圳", "南京", "苏州", "武汉"}
        for case in CASES:
            self.assertIn(case["difficulty"], {"easy", "medium", "hard"})
            self.assertTrue(1 <= case["days"] <= 4)
            self.assertTrue(set(case["cities"]) <= cities)
            for key in ("query", "tags"):
                self.assertTrue(case[key])
            self.assertIn("static", case["tags"])

    def test_spec_passes_validation(self):
        spec = load_spec(ROOT / "evaluation" / "specs" / "static.yaml")
        self.assertEqual(validate(spec), [])


def _write_table(folder: Path, name: str, rows: list[dict]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")


class JudgeBackendTest(unittest.TestCase):
    def test_local_backend_uses_local_endpoint(self):
        args = SimpleNamespace(judge_backend="local", judge_model="")
        with patch.dict(os.environ, {"LOCAL_MODEL_BASE_URL": "http://127.0.0.1:9/v1",
                                     "LOCAL_MODEL_NAME": "tiny-judge"}, clear=False):
            client, model = judge_client_for(args)
        self.assertEqual(model, "tiny-judge")
        self.assertIn("127.0.0.1:9", str(client.base_url))

    def test_api_backend_stays_default(self):
        args = SimpleNamespace(judge_backend="api", judge_model="judge-x")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=False):
            client, model = judge_client_for(args)
        self.assertEqual(model, "judge-x")


class CostUnknownTest(unittest.TestCase):
    def test_cost_without_digits_is_unknown(self):
        case = {"id": "t", "split": "test", "query": "去杭州玩两天", "days": 2, "cities": ["杭州"]}
        result = {
            "status": "complete",
            "days": [{"day": 1, "city": "杭州", "activities": [{
                "start": "09:00", "duration": "2 小时", "title": "西湖", "description": "",
                "transport": "步行", "cost": "未核实", "evidence_id": "d1-p1"}], "meals": []},
                {"day": 2, "city": "杭州", "activities": [{
                    "start": "09:00", "duration": "2 小时", "title": "灵隐寺", "description": "",
                    "transport": "打车", "cost": "免费", "evidence_id": "d1-p1"}], "meals": []}],
            "evidence": [{"id": "d1-p1", "status": "ok", "items": [
                {"name": "西湖", "opentime": "08:00", "endtime": "17:00"},
                {"name": "灵隐寺", "opentime": "06:30", "endtime": "17:45"}]}],
        }
        details = check_details(case, result)
        self.assertEqual(details["no_fabricated_cost"]["status"], "unknown")
        self.assertIsNone(details["no_fabricated_cost"]["ok"])


class DataQualityTest(unittest.TestCase):
    def test_audit_counts_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            _write_table(folder, "attractions.jsonl", [
                {"city": "测试城", "name": "甲", "lon": 1.0, "lat": 2.0, "opentime": "09:00", "endtime": "17:00", "price": 30},
                {"city": "测试城", "name": "乙", "lon": 1.1, "lat": 2.1, "opentime": "00:00", "endtime": "24:00", "price": 0},
            ])
            _write_table(folder, "restaurants.jsonl", [
                {"city": "测试城", "name": "丙", "lon": 1.2, "lat": 2.2, "opentime": "10:00", "endtime": "22:00", "price": 88},
            ])
            _write_table(folder, "accommodations.jsonl", [])
            result = data_quality.audit(folder)
        stat = result["cities"]["测试城"]["attractions"]
        self.assertEqual(stat["rows"], 2)
        self.assertEqual(stat["coords"], 2)
        self.assertEqual(stat["hours"], 1)   # 00:00–24:00 是占位
        self.assertEqual(stat["price"], 1)

    def test_gate_reports_missing_city_and_thin_city(self):
        audit = {"cities": {"甲城": {"attractions": {"rows": 3, "coords": 3}}}}
        problems = data_quality.gate(audit, [{"cities": ["甲城"]}, {"cities": ["乙城"]}], min_attractions=30)
        self.assertTrue(any("甲城" in item for item in problems))
        self.assertTrue(any("乙城" in item for item in problems))

    def test_gate_passes_with_enough_data(self):
        audit = {"cities": {"甲城": {"attractions": {"rows": 50, "coords": 50}}}}
        self.assertEqual(data_quality.gate(audit, [{"cities": ["甲城"]}]), [])


if __name__ == "__main__":
    unittest.main()
