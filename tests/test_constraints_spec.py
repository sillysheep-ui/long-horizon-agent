"""约束集规格与加载测试。"""

from __future__ import annotations

import json

import pytest

from product.constraints.spec import load_set, validate


def test_load_default_set():
    constraint_set = load_set()
    assert constraint_set.version
    assert constraint_set.by_id("density") is not None
    assert constraint_set.by_id("opening_hours") is not None
    assert len(constraint_set.constraints) >= 6


def test_validate_rejects_unknown_operator():
    with pytest.raises(ValueError, match="算子未知"):
        validate({"id": "x", "category": "hard", "severity": "hard", "type": "no_such_op"})


def test_validate_rejects_bad_category():
    with pytest.raises(ValueError, match="category"):
        validate({"id": "x", "category": "misc", "severity": "hard", "type": "time_order"})


def test_load_set_rejects_duplicate_ids(tmp_path):
    payload = {
        "version": "0.0.0",
        "constraints": [
            {"id": "dup", "category": "hard", "severity": "hard", "type": "time_order"},
            {"id": "dup", "category": "hard", "severity": "hard", "type": "no_overlap"},
        ],
    }
    path = tmp_path / "set.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_set(path)


def test_load_set_rejects_empty(tmp_path):
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"version": "1", "constraints": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="不能为空"):
        load_set(path)
