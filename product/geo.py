"""经纬度解析、距离与市内交通时间估算。

坐标来自高德 POI（字符串 "经度,纬度"）；网页搜索通常没有坐标，调用方需自行兜底。
"""

import math
from typing import Any


def parse_coord(item: dict[str, Any]) -> tuple[float, float] | None:
    """返回 (纬度, 经度)；没有合法坐标时返回 None。"""
    raw = item.get("coordinates")
    if isinstance(raw, str) and "," in raw:
        parts = [part.strip() for part in raw.split(",")[:2]]
        try:
            lon, lat = float(parts[0]), float(parts[1])
        except (TypeError, ValueError):
            return None
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            return lat, lon
        return None
    lat, lon = item.get("lat") or item.get("latitude"), item.get("lng") or item.get("lon")
    try:
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def haversine_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lat1, lon1 = first
    lat2, lon2 = second
    radius = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    a = math.sin(d_lat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d_lon / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def route_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    """直线距离按 1.3 倍折算路网距离。"""
    return haversine_km(first, second) * 1.3


def transit_minutes(first: tuple[float, float], second: tuple[float, float]) -> int:
    """短途步行、中距离骑行、远距离打车，各留一点等车时间。"""
    distance = route_km(first, second)
    if distance <= 1.2:
        return max(5, round(distance / 4.5 * 60))
    if distance <= 3.0:
        return max(6, round(distance / 12 * 60))
    return max(8, round(distance / 22 * 60)) + 5


def order_nearest(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从最早开始、且有坐标的点出发，按最近邻重排；无坐标的保持原序放末尾。"""
    located = [(index, activity, parse_coord(activity)) for index, activity in enumerate(activities)]
    with_coord = [(index, activity, coord) for index, activity, coord in located if coord]
    without = [activity for _, activity, coord in located if not coord]
    if len(with_coord) < 2:
        return list(activities)
    with_coord.sort(key=lambda item: str(item[1].get("start") or "23:59"))
    ordered = [with_coord.pop(0)]
    while with_coord:
        _, _, current = ordered[-1]
        with_coord.sort(key=lambda item: haversine_km(current, item[2]))
        ordered.append(with_coord.pop(0))
    return [activity for _, activity, _ in ordered] + without
