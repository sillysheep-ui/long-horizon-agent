"""声明式评测配置：加载、校验与描述。"""

from __future__ import annotations

import pytest

pytest.importorskip("yaml")

from evaluation.spec import describe, load_spec, validate  # noqa: E402

SPEC = "evaluation/specs/travel_plan.yaml"


def test_example_spec_is_valid():
    payload = load_spec(SPEC)
    assert validate(payload) == []
    assert any(item["type"] == "constraint_set" for item in describe(payload)["graders"])


def test_missing_dataset_is_reported(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: x\nversion: 1\ndataset: {path: nope.json}\ngraders: []\n", encoding="utf-8")
    errors = validate(load_spec(path))
    assert any("数据集不存在" in item for item in errors)
    assert any("graders" in item for item in errors)


def test_unknown_grader_type_is_reported(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(
        "name: x\nversion: 1\ndataset: {path: evaluation/product_cases.json}\n"
        "graders: [{type: magic}]\n", encoding="utf-8")
    errors = validate(load_spec(path))
    assert any("未知" in item for item in errors)


def test_all_specs_validate():
    from pathlib import Path

    for path in sorted(Path("evaluation/specs").glob("*.yaml")):
        payload = load_spec(path)
        assert validate(payload) == [], path.name


def test_duplicate_grader_id_is_reported(tmp_path):
    path = tmp_path / "dup.yaml"
    path.write_text(
        "name: x\nversion: 1\ndataset: {path: evaluation/product_cases.json}\n"
        "graders:\n  - {id: a, type: transcript, max_turns: 5}\n"
        "  - {id: a, type: transcript, max_turns: 5}\n", encoding="utf-8")
    errors = validate(load_spec(path))
    assert any("id 重复" in item for item in errors)
