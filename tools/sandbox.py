"""ChinaTravel 沙盒数据检索（离线评测用，CC-BY-4.0）。

数据由 scripts/fetch_sandbox.py 下载并转成 JSONL，默认放在 data/sandbox/。
"""

import json
import os
import re
from pathlib import Path

DEFAULT_DIR = Path(os.getenv("SANDBOX_DIR", "data/sandbox"))
_TABLES = ("attractions", "restaurants", "accommodations")
_cache: dict[str, list[dict]] = {}


def reset_cache() -> None:
    _cache.clear()


def available(directory: Path | None = None) -> bool:
    folder = Path(directory) if directory else DEFAULT_DIR
    return (folder / "attractions.jsonl").exists()


def _table(name: str) -> list[dict]:
    if name not in _cache:
        rows: list[dict] = []
        path = DEFAULT_DIR / (name + ".jsonl")
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        _cache[name] = rows
    return _cache[name]


def _tokens(text: str) -> list[str]:
    return [item for item in re.split(r"[\s,，、/]+", text or "") if item]


def _bigrams(text: str) -> list[str]:
    """中文没有空格，用 2-gram 近似“部分匹配”（如“大熊猫基地”命中“大熊猫繁育研究基地”）。"""
    return [text[index:index + 2] for index in range(len(text) - 1)]


def _score(row: dict, keyword: str) -> int:
    name = str(row.get("name") or "")
    if not name or not keyword:
        return 0
    if keyword in name:
        score = 8
    elif any(token in name for token in _tokens(keyword)):
        score = 5
    elif any(keyword in str(row.get(key) or "") for key in ("cuisine", "recommendedfood",
                                                            "type", "featurehoteltype")):
        score = 4
    elif any(word in name for word in _bigrams(keyword)):
        grams = _bigrams(keyword)
        coverage = sum(1 for word in grams if word in name) / max(1, len(grams))
        score = 3 + int(4 * coverage)
    else:
        return 0
    if len(name) <= 12:
        score += 1
    return score


def search(city: str, keyword: str, limit: int = 8) -> list[dict]:
    """按城市 + 关键词查沙盒 POI，返回与高德 items 兼容的结构。"""
    city = str(city or "").strip()
    keyword = str(keyword or "").strip()
    if not available():
        return []
    ranked: list[tuple[int, dict, str]] = []
    for table in _TABLES:
        for row in _table(table):
            if city and str(row.get("city") or "") != city:
                continue
            score = _score(row, keyword)
            if score:
                ranked.append((score, row, table))
    ranked.sort(key=lambda item: (item[0], -len(str(item[1].get("name") or ""))), reverse=True)
    items: list[dict] = []
    for _, row, table in ranked[:limit]:
        longitude, latitude = row.get("lon"), row.get("lat")
        coordinates = [longitude, latitude] if longitude is not None and latitude is not None else None
        item = {
            "name": str(row.get("name") or "")[:60],
            "address": "",
            "coordinates": coordinates,
            "city": row.get("city") or city,
            "category": row.get("type") if table == "attractions" else
                        (row.get("cuisine") or ("住宿" if table == "accommodations" else "")),
            "source_table": table,
        }
        for key in ("opentime", "endtime", "price", "recommendmintime", "recommendmaxtime",
                    "recommendedfood", "featurehoteltype"):
            if row.get(key) is not None:
                item[key] = row[key]
        items.append(item)
    return items
