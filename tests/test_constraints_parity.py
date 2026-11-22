"""迁移对照：约束引擎的判定必须与旧实现逐条一致。

依赖本地产生的评测记录（results/ 被 gitignore）；没有记录时跳过，
CI 上由单测保证语义，本地大回归时由这个测试守住行为不变。
"""

from __future__ import annotations

import glob
import json

import pytest

from evaluation.product_checks import _legacy_checks, engine_checks


def _stored_cases():
    for path in sorted(glob.glob("results/product_eval/*/records.json")):
        try:
            payload = json.loads(open(path, encoding="utf-8").read())
        except (OSError, ValueError):
            continue
        for record in payload:
            result = record.get("result")
            if not result or result.get("status") == "needs_clarification":
                continue
            yield record.get("id"), {"query": record.get("query")}, result


def test_engine_matches_legacy_checks():
    cases = list(_stored_cases())
    if not cases:
        pytest.skip("没有本地评测记录")
    mismatches: list[str] = []
    for case_id, case, result in cases:
        legacy = _legacy_checks(case, result)
        engine = engine_checks(case, result)
        for name, value in engine.items():
            if name in legacy and legacy[name] != value:
                mismatches.append(f"{case_id}/{name}: legacy={legacy[name]} engine={value}")
    assert not mismatches, mismatches[:10]


def test_constraint_set_covers_migrated_checks():
    from evaluation.product_checks import CONSTRAINT_SET

    ids = {constraint.id for constraint in CONSTRAINT_SET.constraints}
    expected = {
        "complete", "day_count", "density", "time_order", "no_overlap", "day_load",
        "unique_titles", "title_clean", "city_coverage", "evidence_ok",
        "periods", "activity_length", "free_time", "no_fabricated_cost",
    }
    missing = expected - ids
    assert not missing, missing
