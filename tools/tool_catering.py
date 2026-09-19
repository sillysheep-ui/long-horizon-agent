# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 美食搜索工具
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


@register_tool("catering_search", allow_overwrite=True)
class CateringSearch(BaseTool):
    name = "catering_search"
    description = "搜索指定区域内的餐饮美食信息，支持菜系过滤，返回名称、地址、经纬度、评分。"
    parameters = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "中心点经纬度或明确地点名称；不确定坐标时传目的地名称，禁止猜测经纬度"
            },
            "radius": {
                "type": "integer",
                "description": "搜索半径（米），0-50000，默认3000"
            },
            "keyword": {
                "type": "string",
                "description": "可选，菜系或美食类型（如火锅、川菜、海鲜）"
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
        try:
            return normalize_location(value), ""
        except ValueError:
            geocode_params = {"key": AMAP_MAPS_API_KEY, "address": str(value).strip()}
            if region:
                geocode_params["city"] = region
            result, _ = await amap_get(
                "https://restapi.amap.com/v3/geocode/geo", geocode_params
            )
            geocodes = result.get("geocodes") or []
            if result.get("status") != "1" or not geocodes:
                return "", f"餐饮搜索中心无法定位: {value}"
            try:
                return normalize_location(geocodes[0].get("location", "")), str(
                    geocodes[0].get("formatted_address", "")
                )
            except ValueError:
                return "", f"餐饮搜索中心返回无效坐标: {value}"

    async def call(self, params: dict, **kwargs) -> str:
        try:
            location_value = params["location"]
            radius = max(1, min(50000, int(params.get("radius", 3000))))
            keyword = str(params.get("keyword") or "美食").strip()[:80]
            region = params.get("region", None)
        except Exception:
            return "[CateringSearch] Invalid request format: Input must be a JSON object containing 'location' field"

        location, resolved_address = await self._resolve_location(location_value, region)
        if not location:
            return f"[CateringSearch] {resolved_address}"

        url = "https://restapi.amap.com/v5/place/around"
        req_params = {
            "key": AMAP_MAPS_API_KEY,
            "location": location,
            "radius": radius,
            "keywords": keyword,
        }
        if region:
            req_params["region"] = region

        result, _ = await amap_get(url, req_params)

        if result.get("status") != "1":
            msg = result.get("info", "unknown error")
            if msg == "INVALID_PARAMS":
                fallback_params = {
                    "key": AMAP_MAPS_API_KEY,
                    "location": location,
                    "radius": 3000,
                    "keywords": "美食",
                }
                result, _ = await amap_get(url, fallback_params)
                if result.get("status") == "1" and result.get("pois"):
                    return truncate_text(json2md(result["pois"]))
            return f"API response error: {msg}"

        pois = result.get("pois")
        if not pois:
            return f"No catering data available for {req_params}."

        prefix = f"餐饮搜索中心解析为：{resolved_address}（{location}）\n\n" if resolved_address else ""
        return truncate_text(prefix + json2md(pois))


if __name__ == "__main__":
    search = CateringSearch()
    params = {"location": "104.06,30.67", "keyword": "火锅"}
    res = asyncio.run(search.call(params))
    print(res)
