# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 路线规划工具 
# --------------------------------------------


import asyncio
import os
from dotenv import load_dotenv

from utils.markdown import json2md
from utils.text import truncate_text
from qwen_agent.tools.base import BaseTool, register_tool
from utils.amap_client import amap_get
from utils.geo import haversine_km, normalize_location

# 加载环境变量
load_dotenv()

AMAP_MAPS_API_KEY = os.environ["AMAP_MAPS_API_KEY"]


async def reverse_geocode(location: str):
    url = "https://restapi.amap.com/v3/geocode/regeo"
    params = {"key": AMAP_MAPS_API_KEY, "location": location}

    result, _ = await amap_get(url, params)
    return result


async def resolve_point(value: str) -> tuple[str, str]:
    """解析经纬度或地点名，避免 Planner 为命名地点猜测坐标。"""
    try:
        return normalize_location(value), ""
    except ValueError:
        params = {"key": AMAP_MAPS_API_KEY, "address": str(value).strip()}
        result, _ = await amap_get("https://restapi.amap.com/v3/geocode/geo", params)
        geocodes = result.get("geocodes") or []
        if result.get("status") != "1" or not geocodes:
            return "", f"路线地点无法定位: {value}"
        try:
            return normalize_location(geocodes[0].get("location", "")), str(
                geocodes[0].get("formatted_address", "")
            )
        except ValueError:
            return "", f"路线地点返回无效坐标: {value}"


async def get_citycode(location: str):
    result = await reverse_geocode(location)

    try:
        citycode = result["regeocode"]["addressComponent"]["citycode"]
    except:
        citycode = None

    return citycode


async def driving_direction(
    origin: str, destination: str, waypoints: str | None = None
):
    url = "https://restapi.amap.com/v5/direction/driving?parameters"
    params = {"key": AMAP_MAPS_API_KEY, "origin": origin, "destination": destination}

    if waypoints:
        params["waypoints"] = waypoints

    result, _ = await amap_get(url, params)
    return result


async def walking_direction(origin: str, destination: str):
    url = "https://restapi.amap.com/v5/direction/walking?parameters"
    params = {"key": AMAP_MAPS_API_KEY, "origin": origin, "destination": destination}

    result, _ = await amap_get(url, params)
    return result


async def bicycling_direction(origin: str, destination: str):
    url = "https://restapi.amap.com/v5/direction/bicycling?parameters"
    params = {"key": AMAP_MAPS_API_KEY, "origin": origin, "destination": destination}

    result, _ = await amap_get(url, params)
    return result


async def electrobike_direction(origin: str, destination: str):
    url = "https://restapi.amap.com/v5/direction/electrobike?parameters"
    params = {"key": AMAP_MAPS_API_KEY, "origin": origin, "destination": destination}

    result, _ = await amap_get(url, params)
    return result


async def transit_direction(origin: str, destination: str):
    url = "https://restapi.amap.com/v5/direction/transit/integrated?parameters"

    citycode_origin, citycode_destination = await asyncio.gather(
        get_citycode(origin), get_citycode(destination)
    )

    if not citycode_origin:
        return f"City not found for transit origin for {origin} and {destination}."

    if not citycode_destination:
        return f"City not found for transit destination for {origin} and {destination}."

    params = {
        "key": AMAP_MAPS_API_KEY,
        "origin": origin,
        "destination": destination,
        "city1": citycode_origin,
        "city2": citycode_destination,
    }

    result, _ = await amap_get(url, params)
    return result


@register_tool("route_planning", allow_overwrite=True)
class RoutePlanning(BaseTool):
    name = "route_planning"
    description = "路线规划：驾车/步行/骑行/电动车/公交。"
    parameters = {
        "type": "object",
        "properties": {
            "origin": {
                "type": "string",
                "description": "起点经纬度或明确地点名称；不确定坐标时传地点名"
            },
            "destination": {
                "type": "string",
                "description": "终点经纬度或明确地点名称；不确定坐标时传地点名"
            },
            "mode": {
                "type": "string",
                "enum": ["driving", "walking", "bicycling", "electrobike", "transit"],
                "description": "路线类型，默认 driving"
            },
            "waypoints": {
                "type": "string",
                "description": "途经点，多个点以 ; 分隔，每点格式 lng,lat"
            },
        },
        "required": ["origin", "destination"]
    }


    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)


    async def call(self, params: dict, **kwargs) -> str:  # type: ignore[override]
        try:
            origin_value = params["origin"]
            destination_value = params["destination"]
            mode = params.get("mode", "driving")
            waypoints = params.get("waypoints")
        except Exception:
            return "[RoutePlanning] Invalid request format: Input must be a JSON object containing 'origin' and 'destination' field"

        (origin, origin_address), (destination, destination_address) = await asyncio.gather(
            resolve_point(origin_value), resolve_point(destination_value)
        )
        if not origin:
            return f"[RoutePlanning] {origin_address}"
        if not destination:
            return f"[RoutePlanning] {destination_address}"

        if mode == "driving":
            result = await driving_direction(origin, destination, waypoints=waypoints)
        elif mode == "walking":
            result = await walking_direction(origin, destination)
        elif mode == "bicycling":
            result = await bicycling_direction(origin, destination)
        elif mode == "electrobike":
            result = await electrobike_direction(origin, destination)
        elif mode == "transit":
            result = await transit_direction(origin, destination)

        if not isinstance(result, dict):
            return str(result)
        if result.get("status") != "1":
            msg = result.get("info", "unknown error")
            if msg == "OVER_DIRECTION_RANGE":
                direct_km = haversine_km(origin, destination)
                return (
                    "route_status: estimated_fallback\n"
                    f"straight_line_distance_km: {direct_km:.1f}\n"
                    "note: 起终点超出高德单次路线规划范围，未提供虚构驾车路线；"
                    "建议改用城市间铁路/航空，或选择中途城市后分段规划。"
                )
            raise Exception(f"API response error: {msg}")

        route = result.get("route")
        if not route:
            raise Exception(f"No route available for {params}.")

        resolved_prefix = ""
        if origin_address:
            resolved_prefix += f"起点解析为：{origin_address}（{origin}）\n"
        if destination_address:
            resolved_prefix += f"终点解析为：{destination_address}（{destination}）\n"
        if resolved_prefix:
            resolved_prefix += "\n"
        return truncate_text(resolved_prefix + json2md(route))


if __name__ == "__main__":
    search = RoutePlanning()
    params = {"origin": "104.06,30.67", "destination": "108.93,34.27", "mode": "driving", "waypoints": "105.84,32.44"}
    res = asyncio.run(search.call(params))
    print(res)
