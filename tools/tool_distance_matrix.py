# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 距离矩阵工具
# --------------------------------------------


import os
import asyncio
from dotenv import load_dotenv

from utils.markdown import json2md
from utils.text import truncate_text
from qwen_agent.tools.base import BaseTool, register_tool
from utils.amap_client import amap_get
from utils.geo import haversine_km, normalize_location, split_locations

# 加载环境变量
load_dotenv()

AMAP_MAPS_API_KEY = os.environ["AMAP_MAPS_API_KEY"]


@register_tool("distance_matrix", allow_overwrite=True)
class DistanceMatrix(BaseTool):
    name = "distance_matrix"
    description = "批量计算多个起点到一个终点的驾车距离和预估耗时。"
    parameters = {
        "type": "object",
        "properties": {
            "origins": {
                "type": "string",
                "description": "起点经纬度串，多个点以 | 分隔，每点格式 lng,lat"
            },
            "destination": {
                "type": "string",
                "description": "终点经纬度，格式 lng,lat"
            },
            "type": {
                "type": "integer",
                "enum": [0, 1],
                "description": "0=直线距离，1=驾车距离，默认 1"
            },
        },
        "required": ["origins", "destination"]
    }

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)

    async def call(self, params: dict, **kwargs) -> str:
        try:
            origins = "|".join(split_locations(params["origins"]))
            destination = normalize_location(params["destination"])
            dist_type = str(params.get("type", 1))
        except Exception:
            return "[DistanceMatrix] Invalid request format: Input must be a JSON object containing 'origins' and 'destination' fields"

        url = "https://restapi.amap.com/v3/direction/distance"
        req_params = {
            "key": AMAP_MAPS_API_KEY,
            "origins": origins,
            "destination": destination,
            "type": dist_type,
        }

        result, _ = await amap_get(url, req_params)

        if result.get("status") != "1":
            msg = result.get("info", "unknown error")
            if msg == "SERVICE_NOT_AVAILABLE":
                estimated = [
                    {
                        "origin_id": index + 1,
                        "straight_line_distance_meters": round(haversine_km(origin, destination) * 1000),
                        "estimate_only": True,
                    }
                    for index, origin in enumerate(split_locations(origins))
                ]
                return truncate_text(json2md(estimated))
            return f"API response error: {msg}"

        results = result.get("results")
        if not results:
            return f"No distance data available for the given locations."

        formatted = []
        for item in results:
            formatted.append({
                "origin_id": item.get("origin_id"),
                "dest_id": item.get("dest_id"),
                "distance_meters": item.get("distance"),
                "duration_seconds": item.get("duration"),
            })

        return truncate_text(json2md(formatted))


if __name__ == "__main__":
    search = DistanceMatrix()
    params = {
        "origins": "116.48,39.90|116.43,39.92",
        "destination": "116.39,39.91",
    }
    res = asyncio.run(search.call(params))
    print(res)
