# -*- coding: utf-8 -*-
"""高德 Web API 共享客户端：跨工具缓存、限速与临时错误重试。"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Mapping, Tuple

import httpx


TRANSIENT_INFOS = {
    "CUQPS_HAS_EXCEEDED_THE_LIMIT",
    "SERVICE_NOT_AVAILABLE",
    "UNKNOWN_ERROR",
}

_cache: Dict[str, Dict[str, Any]] = {}
_lock = None
_lock_loop = None
_last_request_at = 0.0


def _get_lock() -> asyncio.Lock:
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def _cache_key(url: str, params: Mapping[str, Any]) -> str:
    safe = {key: value for key, value in params.items() if key != "key"}
    return json.dumps([url, safe], ensure_ascii=False, sort_keys=True, default=str)


async def amap_get(
    url: str,
    params: Mapping[str, Any],
    *,
    retries: int = 3,
    min_interval_seconds: float | None = None,
) -> Tuple[Dict[str, Any], bool]:
    """返回 ``(响应 JSON, 是否缓存命中)``。仅缓存业务成功响应。"""
    global _last_request_at
    request_params = dict(params)
    request_params.setdefault("key", os.environ["AMAP_MAPS_API_KEY"])
    key = _cache_key(url, request_params)
    if key in _cache:
        return _cache[key], True

    interval = (
        float(os.environ.get("AMAP_MIN_INTERVAL_SECONDS", "0.25"))
        if min_interval_seconds is None
        else min_interval_seconds
    )
    last_result: Dict[str, Any] = {}
    for attempt in range(retries + 1):
        async with _get_lock():
            delay = interval - (time.monotonic() - _last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, params=request_params)
            _last_request_at = time.monotonic()
        response.raise_for_status()
        last_result = response.json()
        if last_result.get("status") == "1":
            _cache[key] = last_result
            return last_result, False
        info = str(last_result.get("info", "UNKNOWN_ERROR"))
        if info not in TRANSIENT_INFOS or attempt >= retries:
            return last_result, False
        await asyncio.sleep(0.5 * (2 ** attempt))
    return last_result, False


def clear_amap_cache() -> None:
    _cache.clear()
