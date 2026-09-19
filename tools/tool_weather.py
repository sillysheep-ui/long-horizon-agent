# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 天气查询工具 
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


@register_tool("weather_search", allow_overwrite=True)
class WeatherSearch(BaseTool):
    name = "weather_search"
    description = "根据城市名称查询指定城市天气。"
    parameters = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "城市名称"
            }
        },
        "required": ["city"]
    }


    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)


    async def call(self, params: dict, **kwargs) -> str:  # type: ignore[override]
        try:
            city = params["city"]
        except Exception:
            return "[WeatherSearch] Invalid request format: Input must be a JSON object containing 'city' field"

        url = "https://restapi.amap.com/v3/weather/weatherInfo"
        params = {
            "key": AMAP_MAPS_API_KEY,
            "city": city,
            "extensions": "all",
        }

        result, _ = await amap_get(url, params)

        if result.get("status") != "1":
            msg = result.get("info", "unknown error")
            raise Exception(f"API response error: {msg}")

        forecasts = result.get("forecasts")
        if not forecasts:
            geocode, _ = await amap_get(
                "https://restapi.amap.com/v3/geocode/geo",
                {"address": city},
            )
            geocodes = geocode.get("geocodes") or []
            adcode = geocodes[0].get("adcode") if geocodes else ""
            if adcode and adcode != city:
                result, _ = await amap_get(url, {
                    "city": adcode, "extensions": "all",
                })
                forecasts = result.get("forecasts")
            if not forecasts:
                raise Exception("No forecast data available after city/adcode fallback.")

        def format_cast(cast):
            return {
                "date": cast["date"],
                "dayweather": cast["dayweather"],
                "nightweather": cast["nightweather"],
                "daytemp": cast["daytemp"],
                "nighttemp": cast["nighttemp"],
                "daywind": cast["daywind"],
                "nightwind": cast["nightwind"],
                "daypower": cast["daypower"],
                "nightpower": cast["nightpower"],
            }

        def format_forecast(forecast):
            return {
                "city": forecast["city"],
                "province": forecast["province"],
                "casts": [format_cast(cast) for cast in forecast["casts"]],
            }

        forecasts = [format_forecast(forecast) for forecast in forecasts]
        return truncate_text(json2md(forecasts))

if __name__ == "__main__":
    search = WeatherSearch()
    params = {"city": "北京"}
    res = asyncio.run(search.call(params))
    print(res)
