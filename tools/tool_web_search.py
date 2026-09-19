# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 网页搜索工具 
# --------------------------------------------


import asyncio
import itertools
import os
from typing import Any, Dict, Iterable, List

from dotenv import load_dotenv
from firecrawl import Firecrawl
from qwen_agent.tools.base import BaseTool, register_tool


# 加载环境变量
load_dotenv()

SEARCH_MAX_RETRIES = int(os.getenv("SEARCH_MAX_RETRIES", 2))
SEARCH_TIMEOUT_SECONDS = float(os.getenv("SEARCH_TIMEOUT_SECONDS", "35"))
FIRECRAWL_SEARCH_LIMIT = int(os.getenv("FIRECRAWL_SEARCH_LIMIT", 5))


def _to_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "model_dump"):
        try:
            return item.model_dump()
        except Exception:
            pass
    if hasattr(item, "dict"):
        try:
            return item.dict()
        except Exception:
            pass
    return {
        key: value
        for key, value in vars(item).items()
        if not key.startswith("_")
    } if hasattr(item, "__dict__") else {}


def _format_ref_item(index: int, item: Dict[str, Any]) -> str:
    title = item.get("title") or item.get("name") or ""
    url = item.get("url") or item.get("link") or ""
    description = item.get("description") or ""
    date = item.get("date") or item.get("publish_time") or item.get("time") or ""
    source = item.get("website") or item.get("web_anchor") or item.get("source") or ""
    lines = [f"{index}. [{title}]({url})" if url else f"{index}. {title or 'Untitled'}"]
    if description:
        lines.append(f"snippet: {description}")
    if date:
        lines.append(f"date published: {date}")
    if source:
        lines.append(f"source: {source}")
    return "\n".join(lines)


@register_tool("search", allow_overwrite=True)
class WebSearch(BaseTool):
    name = "search"
    description = "执行批量 Google Search：提供 query 数组，一次调用检索每个查询前5个结果。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "array",
                "items": {"type": "string"},
                "description": "查询字符串数组。",
            },
        },
        "required": ["query"],
    }

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.firecrawl_client = Firecrawl(api_key=os.environ["FIRECRAWL_API_KEY"])

    def _firecrawl_search_once(self, query: str) -> str:
        result = self.firecrawl_client.search(
            query=query,
            limit=FIRECRAWL_SEARCH_LIMIT,
            timeout=int(SEARCH_TIMEOUT_SECONDS * 1000),
        )

        web_items = [_to_dict(item) for item in (getattr(result, "web", None) or [])]
        news_items = [_to_dict(item) for item in (getattr(result, "news", None) or [])]
        image_items = [_to_dict(item) for item in (getattr(result, "images", None) or [])]

        if not web_items and not news_items and not image_items:
            return f"No results found for query: '{query}'. Use a less specific query."

        parts = [f"A web search for '{query}' returned:"]
        if web_items:
            formatted_refs = [
                _format_ref_item(i, ref)
                for i, ref in enumerate(web_items, start=1)
            ]
            parts.append("\n## Web Results\n" + "\n\n".join(formatted_refs))
        if news_items:
            start = len(web_items) + 1
            formatted_news = [
                _format_ref_item(i, ref)
                for i, ref in enumerate(news_items, start=start)
            ]
            parts.append("\n## News Results\n" + "\n\n".join(formatted_news))
        if image_items:
            image_lines = []
            start = len(web_items) + len(news_items) + 1
            for i, item in enumerate(image_items, start=start):
                title = item.get("title") or item.get("alt") or "Image"
                url = item.get("url") or item.get("image_url") or item.get("src") or ""
                image_lines.append(f"{i}. [{title}]({url})" if url else f"{i}. {title}")
            parts.append("\n## Image Results\n" + "\n".join(image_lines))
        return "\n".join(parts)

    async def firecrawl_search(self, query: str) -> str:
        last_exc: Exception | None = None
        for _ in range(SEARCH_MAX_RETRIES):
            try:
                return await asyncio.to_thread(self._firecrawl_search_once, query)
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(1)
        return f"Firecrawl search Timeout or error ({type(last_exc).__name__}: {last_exc}); return None, Please try again later."

    async def call(self, params: str | dict, **kwargs) -> str:  # type: ignore[override]
        try:
            query = params["query"]
        except Exception:
            return "[WebSearch] Invalid request format: Input must be a JSON object containing 'query' field"

        if isinstance(query, str):
            return await self.firecrawl_search(query)

        assert isinstance(query, list)
        tasks = [self.firecrawl_search(q) for q in query]
        responses = await asyncio.gather(*tasks)
        return "\n=======\n".join(responses)


if __name__ == "__main__":
    search = WebSearch()
    params = {"query": ["春熙路 美食"]}
    print(asyncio.run(search.call(params)))

