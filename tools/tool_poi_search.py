# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: POI搜工具 
# --------------------------------------------


import asyncio
import os
from dotenv import load_dotenv

from utils.markdown import json2md
from utils.text import truncate_text
from qwen_agent.tools.base import BaseTool, register_tool
from utils.amap_client import amap_get

# 加载环境变量
load_dotenv()

AMAP_MAPS_API_KEY = os.environ["AMAP_MAPS_API_KEY"]
MAX_POI_RESULTS = os.environ.get("MAX_POI_RESULTS", 3)


@register_tool("poi_search", allow_overwrite=True)
class POISearch(BaseTool):
    name = "poi_search"
    description = "按文本搜索地点，返回地址、经纬度、商业信息。"
    parameters = {
        "type": "object",
        "properties": {
            "address": {
                "type": "string",
                "description": "待检索地点文本（单个地址，<=80字符）"
            },
            "region": {
                "type": "string",
                "description": "可选，城市级区域（中文）"
            }
        },
        "required": ["address"]
    }


    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)


    async def call(self, params: dict, **kwargs) -> str:  # type: ignore[override]
        try:
            address = params["address"]
            region = params.get("region", None)
        except Exception:
            return "[POISearch] Invalid request format: Input must be a JSON object containing 'address' field"

        url = "https://restapi.amap.com/v5/place/text"
        params = {
            "key": AMAP_MAPS_API_KEY,
            "keywords": address,
            "page_size": MAX_POI_RESULTS, 
            "show_fields": "business",
        }
        if region:
            params["region"] = region

        result, _ = await amap_get(url, params)

        if result.get("status") != "1":
            msg = result.get("info", "unknown error")
            raise Exception(f"API response error: {msg}")

        pois = result.get("pois")
        if not pois:
            geocode_params = {"key": AMAP_MAPS_API_KEY, "address": address}
            if region:
                geocode_params["city"] = region
            geocode, _ = await amap_get(
                "https://restapi.amap.com/v3/geocode/geo",
                geocode_params,
            )
            geocodes = geocode.get("geocodes") or []
            if geocodes:
                return truncate_text(json2md(geocodes[:int(MAX_POI_RESULTS)]))
            raise Exception("No POI or geocode data available.")

        return truncate_text(json2md(pois))


if __name__ == "__main__":
    search = POISearch()
    params = {"address": "春熙路", "region": "成都市"}
    res = asyncio.run(search.call(params))
    print(res)
