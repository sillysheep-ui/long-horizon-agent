"""ChinaTravel 沙盒数据检索（离线评测用，CC-BY-4.0）。

数据由 scripts/fetch_sandbox.py 下载并转成 JSONL，默认放在 data/sandbox/。

沙盒是三份 Parquet 转来的静态快照，和实时接口有几处天然差异：
- 城市名可能带“市/地区”后缀，也可能是拼音；
- 景点粒度细到“产房/别墅/分馆”，需要收敛到父级；
- 价格可能为空，营业时间可能用 00:00–24:00 占位；
- 没有地址与描述文本。

这里统一做归一化，产品侧只看到和地图 POI 相近的结构。
"""

import hashlib
import json
import os
import re
from pathlib import Path

DEFAULT_DIR = Path(os.getenv("SANDBOX_DIR", "data/sandbox"))
_TABLES = ("attractions", "restaurants", "accommodations")
_cache: dict[str, list[dict]] = {}

# 景点粒度太细时，这些后缀多半是“某个园区里的一个点”，落到父级更有用。
_CHILD_SUFFIXES = ("产房", "别墅区", "馆区", "园区", "分馆", "展区", "分店")
# 城市名归一化：去掉行政区后缀，拼音走别名。
_CITY_SUFFIXES = ("市", "地区", "自治州", "盟", "县", "区")
_CITY_ALIASES = {
    "hangzhou": "杭州", "suzhou": "苏州", "shanghai": "上海", "beijing": "北京",
    "guangzhou": "广州", "shenzhen": "深圳", "chengdu": "成都", "chongqing": "重庆",
    "nanjing": "南京", "wuhan": "武汉",
}
# 口语词到数据里真实字段值的映射：沙盒没有“摄影”“苍蝇馆子”这类标签。
_SYNONYMS = {
    "摄影": ("公园", "湖", "古镇", "老街", "山", "夜景", "湿地"),
    "拍": ("公园", "湖", "古镇", "老街", "山", "夜景"),
    "老街": ("古镇", "步行街", "街区", "历史文化"),
    "古镇": ("老街", "古村", "历史文化"),
    "苍蝇馆子": ("小吃", "川菜", "火锅", "面馆", "烧烤", "家常菜"),
    "小吃": ("小吃", "美食街", "夜市"),
    "早茶": ("茶点", "粤菜", "广式"),
    "博物馆": ("博物院", "陈列馆", "纪念馆", "美术馆"),
    "公园": ("园林", "植物园", "湿地", "动物园"),
    "爬山": ("山", "风景区", "森林公园"),
    "看海": ("海滩", "海滨", "岛", "湾"),
}
# 占位营业时间：等价于“未知”，不能拿来判断是否开门。
_PLACEHOLDER_OPEN = {"00:00", "00:00:00"}
_PLACEHOLDER_CLOSE = {"24:00", "23:59", "23:59:59"}


def reset_cache() -> None:
    _cache.clear()


def available(directory: Path | None = None) -> bool:
    folder = Path(directory) if directory else DEFAULT_DIR
    return (folder / "attractions.jsonl").exists()


def cities() -> set[str]:
    """沙盒覆盖的城市（10 个），用于评测分池。"""
    if not available():
        return set()
    return {str(row.get("city") or "") for row in _table("attractions") if row.get("city")}


def version() -> dict:
    """数据集版本：文件哈希 + 修改时间，写进评测报告保证可复现。"""
    path = DEFAULT_DIR / "attractions.jsonl"
    if not path.exists():
        return {}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    payload = {"sha256": digest, "mtime": int(path.stat().st_mtime), "rows": len(_table("attractions"))}
    meta = DEFAULT_DIR / "META.json"
    if meta.exists():
        try:
            extra = json.loads(meta.read_text(encoding="utf-8"))
        except ValueError:
            extra = {}
        for key in ("source", "downloaded_at"):
            if extra.get(key):
                payload[key] = extra[key]
    return payload


def normalize_city(city: str) -> str:
    text = str(city or "").strip()
    if not text:
        return ""
    if text.lower() in _CITY_ALIASES:
        return _CITY_ALIASES[text.lower()]
    for suffix in _CITY_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


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


def _expand(keyword: str) -> list[str]:
    terms = [keyword]
    for term in _SYNONYMS.get(keyword, ()):
        if term not in terms:
            terms.append(term)
    return terms


def _is_child_spot(name: str) -> bool:
    return any(name.endswith(suffix) for suffix in _CHILD_SUFFIXES)


def _score(row: dict, keyword: str) -> int:
    name = str(row.get("name") or "")
    if not name or not keyword:
        return 0
    best = 0
    for term in _expand(keyword):
        if term in name:
            score = 8
        elif any(token in name for token in _tokens(term)):
            score = 5
        elif any(term in str(row.get(key) or "") for key in ("cuisine", "recommendedfood",
                                                              "type", "featurehoteltype")):
            score = 4
        elif any(word in name for word in _bigrams(term)):
            grams = _bigrams(term)
            coverage = sum(1 for word in grams if word in name) / max(1, len(grams))
            score = 3 + int(4 * coverage)
        else:
            score = 0
        best = max(best, score)
    if best and len(name) <= 12:
        best += 1
    return best


def _opening(row: dict) -> tuple[str | None, str | None]:
    """把占位时间当作“未知”，避免用它判断是否开门。"""
    start = str(row.get("opentime") or "").strip()
    end = str(row.get("endtime") or "").strip()
    if start in _PLACEHOLDER_OPEN and end in _PLACEHOLDER_CLOSE:
        return None, None
    return (start or None), (end or None)


def search(city: str, keyword: str, limit: int = 8) -> list[dict]:
    """按城市 + 关键词查沙盒 POI，返回与高德 items 兼容的结构。"""
    city = normalize_city(city)
    keyword = str(keyword or "").strip()
    if not available() or not keyword:
        return []
    ranked: list[tuple[int, dict, str]] = []
    for table in _TABLES:
        for row in _table(table):
            if city and normalize_city(str(row.get("city") or "")) != city:
                continue
            name = str(row.get("name") or "")
            if table == "attractions" and _is_child_spot(name):
                continue
            score = _score(row, keyword)
            if score:
                ranked.append((score, row, table))
    ranked.sort(key=lambda item: (item[0], -len(str(item[1].get("name") or ""))), reverse=True)
    items: list[dict] = []
    seen: set[str] = set()
    for _, row, table in ranked:
        name = str(row.get("name") or "")[:60]
        if name in seen:
            continue
        seen.add(name)
        longitude, latitude = row.get("lon"), row.get("lat")
        coordinates = [longitude, latitude] if longitude is not None and latitude is not None else None
        start, end = _opening(row) if table == "attractions" else (
            str(row.get("opentime") or "") or None, str(row.get("endtime") or "") or None)
        item = {
            "name": name,
            "address": "",
            "coordinates": coordinates,
            "city": row.get("city") or city,
            "category": row.get("type") if table == "attractions" else
                        (row.get("cuisine") or ("住宿" if table == "accommodations" else "")),
            "source_table": table,
            "opentime": start,
            "endtime": end,
        }
        price = row.get("price")
        if price is not None and str(price).strip() not in ("", "None", "nan"):
            item["price"] = price
        for key in ("recommendmintime", "recommendmaxtime", "recommendedfood", "featurehoteltype"):
            if row.get(key) is not None:
                item[key] = row[key]
        items.append(item)
        if len(items) >= limit:
            break
    return items
