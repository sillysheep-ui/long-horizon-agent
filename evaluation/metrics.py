"""评测指标：micro / macro / bootstrap 区间，四个 suite 共用。"""

from __future__ import annotations

import random
from typing import Any

from evaluation.contract import CheckRecord


def micro(checks: list[CheckRecord]) -> float | None:
    """按检查条数算通过率；unknown 不计入分母。"""
    known = [item.ok for item in checks if item.ok is not None]
    if not known:
        return None
    return round(sum(1 for value in known if value) / len(known), 3)


def macro(per_case: list[list[CheckRecord]]) -> float | None:
    """按用例算：所有检查都通过的用例占比。"""
    if not per_case:
        return None
    passed = 0
    for group in per_case:
        known = [item.ok for item in group if item.ok is not None]
        if known and all(known):
            passed += 1
    return round(passed / len(per_case), 3)


def bootstrap_ci(values: list[float], n: int = 2000, seed: int = 7) -> tuple[float, float]:
    """均值的 95% 百分位区间；样本少于 2 时返回点值。"""
    numbers = [float(value) for value in values if value is not None]
    if len(numbers) < 2:
        point = numbers[0] if numbers else 0.0
        return (round(point, 3), round(point, 3))
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(n):
        sample = [rng.choice(numbers) for _ in numbers]
        means.append(sum(sample) / len(sample))
    means.sort()
    low = means[int(0.025 * n)]
    high = means[min(n - 1, int(0.975 * n))]
    return (round(low, 3), round(high, 3))


def summarize(per_case: list[list[CheckRecord]], scores: list[float] | None = None) -> dict[str, Any]:
    flat = [item for group in per_case for item in group]
    payload: dict[str, Any] = {"checks_micro": micro(flat), "checks_macro": macro(per_case)}
    if scores:
        low, high = bootstrap_ci(scores)
        payload["score_ci95"] = [low, high]
    return payload
