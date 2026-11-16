"""外部数据源适配：高德 POI 与网页搜索，输出统一的证据结构。"""

import os
from typing import Any
from urllib.parse import urlparse

import httpx

from product.names import clean_place_name, dedupe_names, looks_like_place
from product.provenance import build_evidence
from tools.poi import search_pois
from tools.web import search_web


def _rank_search_items(hits: Any, keyword: str, city: str) -> list[dict[str, Any]]:
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

    async def web_search(self, keyword: str, city: str, evidence_id: str) -> dict[str, Any]:
        """网页搜索：复用 tools.web 的多 provider 实现。"""
        query = (city + " " + keyword + " 地点介绍 地址")[:128]
        try:
            hits = await search_web(query, limit=8)
        except RuntimeError:
            return build_evidence(evidence_id, "web", keyword, city, "unavailable",
                                  source="网页搜索", source_kind="none",
                                  reason="未配置可用的地点搜索服务")
        except (httpx.HTTPError, ValueError):
            return build_evidence(evidence_id, "web", keyword, city, "failed",
                                  source="网页搜索", source_kind="none",
                                  reason="网页搜索暂时不可用")
        items = _rank_search_items(hits, keyword, city)
        return build_evidence(evidence_id, "web", keyword, city,
                              "ok" if items else "empty",
                              items=items, source="网页搜索", source_kind="web_search")

    async def poi(self, keyword: str, city: str, evidence_id: str) -> dict[str, Any]:
        """高德 POI：复用 tools.poi 的结构化查询，失败时降级到网页搜索。"""
        if not self.amap_key:
            return await self.web_search(keyword, city, evidence_id)
        try:
            data = await search_pois(keyword, city)
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
        except (RuntimeError, httpx.HTTPError, ValueError):
            return await self.web_search(keyword, city, evidence_id)
