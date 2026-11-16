"""网页搜索的结构化实现：优先 Firecrawl，未配置或失败时降级 DeepSeek 搜索。

不依赖 qwen_agent，供工具层与产品层复用，返回统一的 ``{"title","url","snippet"}``。
"""

import asyncio
import os
from typing import Any, Dict, List, Optional

import httpx

FIRECRAWL_TIMEOUT_SECONDS = float(os.getenv("SEARCH_TIMEOUT_SECONDS", "35"))
DEEPSEEK_URL = "https://api.deepseek.com/v1/tools/search"


def deepseek_key() -> str:
    return os.getenv("DEEPSEEK_API_KEY") or (
        os.getenv("OPENAI_API_KEY", "") if "api.deepseek.com" in os.getenv("OPENAI_BASE_URL", "") else "")


def _firecrawl_available() -> bool:
    if not os.getenv("FIRECRAWL_API_KEY"):
        return False
    try:
        import firecrawl  # noqa: F401
    except ImportError:
        return False
    return True


def has_provider() -> bool:
    """是否至少配置了一种网页搜索服务。"""
    return bool(_firecrawl_available() or deepseek_key())


def _normalize(items: Any) -> List[Dict[str, str]]:
    result = []
    for item in items or []:
        if hasattr(item, "model_dump"):
            try:
                item = item.model_dump()
            except Exception:
                item = {}
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("link") or "")
        title = str(item.get("title") or item.get("name") or "")
        snippet = str(item.get("description") or item.get("snippet") or "")[:300]
        if url.startswith(("http://", "https://")) and title:
            result.append({"title": title[:140], "url": url, "snippet": snippet})
    return result


async def _firecrawl_items(query: str, limit: int) -> Optional[List[Dict[str, str]]]:
    if not _firecrawl_available():
        return None
    from firecrawl import Firecrawl

    client = Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])
    result = await asyncio.to_thread(
        client.search, query=query, limit=limit, timeout=int(FIRECRAWL_TIMEOUT_SECONDS * 1000))
    raw = list(getattr(result, "web", None) or []) + list(getattr(result, "news", None) or [])
    return _normalize(raw) or None


async def _deepseek_items(query: str, limit: int) -> Optional[List[Dict[str, str]]]:
    key = deepseek_key()
    if not key:
        return None
    async with httpx.AsyncClient(timeout=18) as client:
        response = await client.post(DEEPSEEK_URL,
                                     headers={"Authorization": "Bearer " + key},
                                     json={"query": query[:128], "count": limit, "need_content": False})
        response.raise_for_status()
        data = response.json()
    return _normalize(data.get("results")) or None


async def search_web(query: str, limit: int = 8) -> List[Dict[str, str]]:
    """按顺序尝试 Firecrawl、DeepSeek；都不可用时抛 ``RuntimeError``。"""
    if not has_provider():
        raise RuntimeError("未配置可用的搜索服务")
    for provider in (_firecrawl_items, _deepseek_items):
        try:
            items = await provider(query, limit)
        except Exception:
            items = None
        if items:
            return items
    return []
