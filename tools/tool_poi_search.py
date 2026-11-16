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
from tools.poi import search_pois

# 加载环境变量
load_dotenv()

AMAP_MAPS_API_KEY = os.environ.get("AMAP_MAPS_API_KEY", "")
MAX_POI_RESULTS = int(os.environ.get("MAX_POI_RESULTS", 3))


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

        try:
            data = await search_pois(address, region)
        except RuntimeError as exc:
            raise Exception(str(exc))

        payload = data.get("pois") or data.get("geocodes") or []
        return truncate_text(json2md(payload[:MAX_POI_RESULTS]))


if __name__ == "__main__":
    search = POISearch()
    params = {"address": "春熙路", "region": "成都市"}
    res = asyncio.run(search.call(params))
    print(res)
