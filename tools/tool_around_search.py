# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 周边搜工具 
# --------------------------------------------


import os
import asyncio
from dotenv import load_dotenv

from utils.markdown import json2md
from utils.text import truncate_text
from qwen_agent.tools.base import BaseTool, register_tool
from utils.amap_client import amap_get
from utils.geo import normalize_location

# 加载环境变量
load_dotenv()

AMAP_MAPS_API_KEY = os.environ["AMAP_MAPS_API_KEY"]


@register_tool("around_search", allow_overwrite=True)
class AroundSearch(BaseTool):
    name = "around_search"
    description = "以圆心+半径搜索周边地点。"
    parameters = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "中心点经纬度（lng,lat）或明确地点名称；不确定坐标时必须传地点名，禁止猜测经纬度"
            },
            "radius": {
                "type": "integer",
                "description": "半径（米），0-50000，默认5000"
            },
            "keyword": {
                "type": "string",
                "description": "可选，单个关键词"
            },
            "region": {
                "type": "string",
                "description": "可选，城市级区域（中文）"
            },
        },
        "required": ["location"]
    }


    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)

    async def _resolve_location(self, value: str, region: str | None) -> tuple[str, str]:
        """接受坐标或地点名；地点名通过高德地理编码解析，避免模型猜坐标。"""
        try:
            return normalize_location(value), ""
        except ValueError:
            params = {"key": AMAP_MAPS_API_KEY, "address": str(value).strip()}
            if region:
                params["city"] = region
            result, _ = await amap_get("https://restapi.amap.com/v3/geocode/geo", params)
            geocodes = result.get("geocodes") or []
            if result.get("status") != "1" or not geocodes:
                return "", f"中心地点无法定位: {value}"
            location = geocodes[0].get("location", "")
            try:
                return normalize_location(location), str(geocodes[0].get("formatted_address", ""))
            except ValueError:
                return "", f"中心地点返回了无效坐标: {value}"

    @staticmethod
    def _fallback_keywords(keyword: str) -> list[str]:
        text = str(keyword or "").strip()
        expanded: list[str] = []
        if any(term in text for term in ("科教", "文化")):
            expanded.extend(["博物馆", "图书馆", "科技馆", "文化馆", "美术馆"])
        expanded.extend(part for part in text.replace("/", " ").split() if part)
        return list(dict.fromkeys(item for item in expanded if item != text))[:5]


    async def call(self, params: dict, **kwargs) -> str:  # type: ignore[override]
        try:
            location_value = params["location"]
            radius = params.get("radius", 5000)
            keyword = params.get("keyword", None)
            region = params.get("region", None)
        except Exception:
            return "[AroundSearch] Invalid request format: Input must be a JSON object containing 'location' field"

        location, resolved_address = await self._resolve_location(location_value, region)
        if not location:
            return f"[AroundSearch] {resolved_address}"

        url = "https://restapi.amap.com/v5/place/around"
        params = {
            "key": AMAP_MAPS_API_KEY,
            "location": location,
            "radius": radius,
            "show_fields": "business",
        }
        if keyword:
            params["keywords"] = keyword
        if region:
            params["region"] = region

        result, _ = await amap_get(url, params)

        if result.get("status") != "1":
            msg = result.get("info", "unknown error")
            return f"API response error: {msg}"

        pois = result.get("pois")
        used_keyword = keyword
        if not pois and keyword:
            # 高德对“科教 文化”一类复合词常返回空；按具体类别并发补查。
            fallback_keywords = self._fallback_keywords(keyword)
            if fallback_keywords:
                async def search_one(item: str):
                    retry_params = dict(params)
                    retry_params["keywords"] = item
                    retry_result, _ = await amap_get(url, retry_params)
                    return item, retry_result.get("pois") or []

                batches = await asyncio.gather(*(search_one(item) for item in fallback_keywords))
                seen: set[str] = set()
                merged = []
                for item, rows in batches:
                    for row in rows:
                        identity = str(row.get("id") or row.get("name") or "")
                        if identity and identity not in seen:
                            seen.add(identity)
                            merged.append(row)
                pois = merged
                used_keyword = "、".join(fallback_keywords)
        if not pois:
            return f"No POI data available for {params}."

        prefix = ""
        if resolved_address:
            prefix += f"中心地点解析为：{resolved_address}（{location}）\n\n"
        if used_keyword != keyword:
            prefix += f"原关键词无结果，已按具体类别补查：{used_keyword}\n\n"
        return truncate_text(prefix + json2md(pois))

if __name__ == "__main__":
    search = AroundSearch()
    params = {'location': '120.081964,30.302761', 'keyword': '商店'}
    res = asyncio.run(search.call(params))
    print(res)
