"""高德 POI 查询的结构化实现。

不依赖 qwen_agent，供 `tools/tool_poi_search.py` 与产品层共同复用，避免同一套
高德调用逻辑写两遍。
"""

import os
from typing import Any

from utils.amap_client import amap_get

POI_URL = "https://restapi.amap.com/v5/place/text"
GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"


def _max_results() -> int:
    return int(os.environ.get("MAX_POI_RESULTS", 3))


async def search_pois(address: str, region: str | None = None) -> dict[str, Any]:
    """按文本查地点，返回 ``{"pois": [...]}`` 或 ``{"geocodes": [...]}``。

    未配置 key 或接口报错时抛 ``RuntimeError``，消息沿用原工具文案。
    """
    key = os.environ.get("AMAP_MAPS_API_KEY", "")
    if not key:
        raise RuntimeError("未配置 AMAP_MAPS_API_KEY")

    params: dict[str, Any] = {
        "key": key,
        "keywords": address,
        "page_size": _max_results(),
        "show_fields": "business",
    }
    if region:
        params["region"] = region

    result, _ = await amap_get(POI_URL, params)
    if result.get("status") != "1":
        raise RuntimeError("API response error: %s" % result.get("info", "unknown error"))

    pois = result.get("pois") or []
    if pois:
        return {"pois": pois}

    geocode_params: dict[str, Any] = {"key": key, "address": address}
    if region:
        geocode_params["city"] = region
    geocode, _ = await amap_get(GEOCODE_URL, geocode_params)
    geocodes = geocode.get("geocodes") or []
    if not geocodes:
        raise RuntimeError("No POI or geocode data available.")
    return {"geocodes": geocodes}
