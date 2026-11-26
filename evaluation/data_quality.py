"""静态评测的数据质量审计与门禁。

沙盒是快照，不是实时地图：坐标基本齐全，营业时间有占位，地址全空，
价格大量为 0（未知）。这里把“这批用例依赖的城市有没有可用数据”做成
可复现的门禁，避免用一份残缺快照得出漂亮但不可信的分数。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(os.getenv("SANDBOX_DIR", "data/sandbox"))
TABLE_FILES = {
    "attractions": "attractions.jsonl",
    "restaurants": "restaurants.jsonl",
    "accommodations": "accommodations.jsonl",
}
_PLACEHOLDER_OPEN = {"", "00:00", "00:00:00"}
_PLACEHOLDER_CLOSE = {"", "24:00", "23:59", "23:59:59"}


def _rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _truthy_hours(row: dict[str, Any]) -> bool:
    start = str(row.get("opentime") or "").strip()
    end = str(row.get("endtime") or "").strip()
    return start not in _PLACEHOLDER_OPEN and end not in _PLACEHOLDER_CLOSE


def _truthy_price(row: dict[str, Any]) -> bool:
    try:
        return float(row.get("price") or 0) > 0
    except (TypeError, ValueError):
        return False


def _city(row: dict[str, Any]) -> str:
    return str(row.get("city") or "").strip()


def audit(directory: Path | None = None) -> dict[str, Any]:
    """按城市统计三张表的行数、坐标、营业时间、价格覆盖。"""
    folder = Path(directory) if directory else DEFAULT_DIR
    cities: dict[str, dict[str, Any]] = {}
    for table, name in TABLE_FILES.items():
        for row in _rows(folder / name):
            city = _city(row)
            if not city:
                continue
            bucket = cities.setdefault(city, {})
            stat = bucket.setdefault(table, {"rows": 0, "coords": 0, "hours": 0, "price": 0})
            stat["rows"] += 1
            if row.get("lon") is not None and row.get("lat") is not None:
                stat["coords"] += 1
            if _truthy_hours(row):
                stat["hours"] += 1
            if _truthy_price(row):
                stat["price"] += 1
    return {"directory": str(folder), "cities": cities}


def _rate(value: int, total: int) -> float:
    return round(value / total, 3) if total else 0.0


def summary(audit_result: dict[str, Any]) -> dict[str, Any]:
    """把审计结果压成报告用的一行摘要。"""
    totals: dict[str, dict[str, int]] = {}
    for city in audit_result.get("cities", {}).values():
        for table, stat in city.items():
            bucket = totals.setdefault(table, {"rows": 0, "coords": 0, "hours": 0, "price": 0})
            for key in bucket:
                bucket[key] += stat.get(key, 0)
    out = {}
    for table, stat in totals.items():
        out[table] = {
            "rows": stat["rows"],
            "coords": _rate(stat["coords"], stat["rows"]),
            "hours": _rate(stat["hours"], stat["rows"]),
            "price": _rate(stat["price"], stat["rows"]),
        }
    return {"totals": out, "cities": len(audit_result.get("cities", {}))}


def gate(audit_result: dict[str, Any], cases: list[dict[str, Any]],
         min_attractions: int = 30, min_coords: float = 0.95) -> list[str]:
    """返回问题列表；空列表表示这批用例依赖的城市都有足够数据。"""
    cities = audit_result.get("cities", {})
    wanted = sorted({str(city) for case in cases for city in case.get("cities") or []})
    problems: list[str] = []
    for city in wanted:
        stat = cities.get(city)
        if not stat:
            problems.append(f"{city}：快照里没有这个城市")
            continue
        attractions = stat.get("attractions") or {"rows": 0, "coords": 0}
        if attractions["rows"] < min_attractions:
            problems.append(f"{city}：景点只有 {attractions['rows']} 条（至少 {min_attractions}）")
        coords = _rate(attractions.get("coords", 0), attractions["rows"])
        if coords < min_coords:
            problems.append(f"{city}：景点坐标覆盖 {coords:.0%}（至少 {min_coords:.0%}）")
    return problems
