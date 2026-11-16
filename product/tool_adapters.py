"""外部数据源适配：高德 POI 与 DeepSeek 网页搜索，输出统一的证据结构。"""

import os
from typing import Any, Dict, List
from urllib.parse import urlparse

import httpx

from product.names import clean_place_name, dedupe_names, looks_like_place
from product.provenance import build_evidence


def _rank_search_items(hits: Any, keyword: str, city: str) -> List[Dict[str, Any]]:
    """只保留标题或摘要与目标相关的网页，并优先展示官方来源。"""
    candidates = []
    if not isinstance(hits, list):
        return []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url, title = str(hit.get("url") or ""), str(hit.get("title") or "").strip()
        parsed = urlparse(url)
        if not title or parsed.scheme not in ("https", "http") or not parsed.hostname:
            continue
        snippet = str(hit.get("snippet") or "")[:300]
        short = keyword[:2]
        if not (keyword in title or short in title or city in title or keyword in snippet):
            continue
        name = clean_place_name(title)
        if not name:
            continue
        score = (6 if keyword in title else 0) + (3 if short in title else 0)
        score += (2 if city in title else 0) + (1 if keyword in snippet else 0)
        score += (4 if parsed.hostname.endswith("gov.cn") else 0)
        score += (3 if looks_like_place(name, keyword, city) else -2)
        score += (2 if len(name) <= 12 else 0)
        score -= (3 if len(name) > 24 else 0)
        candidates.append((score, {"name": name[:60], "source_title": title[:140],
                                   "address": "", "url": url, "snippet": snippet,
                                   "coordinates": None}))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return dedupe_names([item for _, item in candidates])[:3]


class ResearchTools:
    def __init__(self) -> None:
        self.amap_key = os.getenv("AMAP_MAPS_API_KEY", "")
        self.search_key = os.getenv("DEEPSEEK_API_KEY") or (
            os.getenv("OPENAI_API_KEY", "") if "api.deepseek.com" in os.getenv("OPENAI_BASE_URL", "") else "")

    async def web_search(self, keyword: str, city: str, evidence_id: str) -> Dict[str, Any]:
        if not self.search_key:
            return build_evidence(evidence_id, "web", keyword, city, "unavailable",
                                  source="网页搜索", source_kind="none",
                                  reason="未配置可用的地点搜索服务")
        try:
            async with httpx.AsyncClient(timeout=18) as client:
                response = await client.post("https://api.deepseek.com/v1/tools/search",
                    headers={"Authorization": "Bearer " + self.search_key},
                    json={"query": (city + " " + keyword + " 地点介绍 地址")[0:128], "count": 8,
                          "need_content": False})
                response.raise_for_status()
                data = response.json()
            items = _rank_search_items(data.get("results"), keyword, city)
            return build_evidence(evidence_id, "web", keyword, city,
                                  "ok" if items else "empty",
                                  items=items, source="DeepSeek 网页搜索", source_kind="web_search")
        except (httpx.HTTPError, ValueError, KeyError):
            return build_evidence(evidence_id, "web", keyword, city, "failed",
                                  source="DeepSeek 网页搜索", source_kind="web_search",
                                  reason="网页搜索暂时不可用")

    async def poi(self, keyword: str, city: str, evidence_id: str) -> Dict[str, Any]:
        if not self.amap_key:
            return await self.web_search(keyword, city, evidence_id)
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get("https://restapi.amap.com/v5/place/text", params={
                    "key": self.amap_key, "keywords": keyword, "region": city, "page_size": 3})
                response.raise_for_status()
                data = response.json()
            if data.get("status") != "1":
                return await self.web_search(keyword, city, evidence_id)
            items = []
            for poi in data.get("pois") or []:
                location = str(poi.get("location", "")).split(",")
                coordinates = None
                if len(location) == 2:
                    try:
                        coordinates = [float(location[0]), float(location[1])]
                    except ValueError:
                        pass
                items.append({"name": poi.get("name", ""), "address": poi.get("address", ""),
                              "coordinates": coordinates, "city": poi.get("cityname") or city,
                              "adcode": poi.get("adcode", "")})
            return build_evidence(evidence_id, "poi", keyword, city,
                                  "ok" if items else "empty",
                                  items=items, source="高德地图 POI", source_kind="map_poi")
        except (httpx.HTTPError, ValueError):
            return await self.web_search(keyword, city, evidence_id)
