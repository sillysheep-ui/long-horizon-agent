# -*- coding: utf-8 -*-
"""地理参数规范化与不依赖外部 API 的保守距离估算。"""

from __future__ import annotations

import math
import re
from typing import List, Tuple


COORD_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def normalize_location(value: str) -> str:
    match = COORD_RE.search(str(value or ""))
    if not match:
        raise ValueError(f"无效经纬度: {value}")
    lng, lat = float(match.group(1)), float(match.group(2))
    if not (-180 <= lng <= 180 and -90 <= lat <= 90):
        raise ValueError(f"经纬度超出范围: {value}")
    return f"{lng:.6f},{lat:.6f}"


def split_locations(value: str) -> List[str]:
    return [normalize_location(item) for item in str(value).split("|") if item.strip()]


def haversine_km(origin: str, destination: str) -> float:
    lng1, lat1 = map(float, normalize_location(origin).split(","))
    lng2, lat2 = map(float, normalize_location(destination).split(","))
    radius_km = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
