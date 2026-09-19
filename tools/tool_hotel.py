# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 酒店搜索工具
# --------------------------------------------


import os
import asyncio
from dotenv import load_dotenv

from utils.markdown import json2md
from utils.text import truncate_text
from qwen_agent.tools.base import BaseTool, register_tool
from utils.amap_client import amap_get

# 加载环境变量
load_dotenv()

AMAP_MAPS_API_KEY = os.environ["AMAP_MAPS_API_KEY"]


@register_tool("hotel_search", allow_overwrite=True)
class HotelSearch(BaseTool):
    name = "hotel_search"
    description = "搜索指定区域内的酒店信息，返回名称、地址、经纬度、电话、评分。"
    parameters = {
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "中心点经纬度，经度在前，格式 lng,lat"
            },
            "radius": {
                "type": "integer",
                "description": "搜索半径（米），0-50000，默认5000"
            },
            "keyword": {
                "type": "string",
                "description": "可选，酒店名称关键词"
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

    async def call(self, params: dict, **kwargs) -> str:
        try:
            location = params["location"]
            radius = params.get("radius", 5000)
            keyword = params.get("keyword", "酒店")
            region = params.get("region", None)
        except Exception:
            return "[HotelSearch] Invalid request format: Input must be a JSON object containing 'location' field"

        url = "https://restapi.amap.com/v5/place/around"
        params = {
            "key": AMAP_MAPS_API_KEY,
            "location": location,
            "radius": radius,
            "keywords": keyword,
        }
        if region:
            params["region"] = region

        result, _ = await amap_get(url, params)

        if result.get("status") != "1":
            msg = result.get("info", "unknown error")
            return f"API response error: {msg}"

        pois = result.get("pois")
        if not pois:
            return f"No hotel data available for {params}."

        return truncate_text(json2md(pois))


if __name__ == "__main__":
    search = HotelSearch()
    params = {"location": "104.06,30.67", "keyword": "商务酒店"}
    res = asyncio.run(search.call(params))
    print(res)
