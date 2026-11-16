# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: 网页搜索工具 
# --------------------------------------------


import asyncio
import os
from typing import Any, Dict

from dotenv import load_dotenv
from qwen_agent.tools.base import BaseTool, register_tool

from tools.web import _firecrawl_available, _firecrawl_items


# 加载环境变量
load_dotenv()

SEARCH_MAX_RETRIES = int(os.getenv("SEARCH_MAX_RETRIES", 2))
SEARCH_TIMEOUT_SECONDS = float(os.getenv("SEARCH_TIMEOUT_SECONDS", "35"))
FIRECRAWL_SEARCH_LIMIT = int(os.getenv("FIRECRAWL_SEARCH_LIMIT", 5))


def _format_ref_item(index: int, item: Dict[str, Any]) -> str:
    title = item.get("title") or item.get("name") or ""
    url = item.get("url") or item.get("link") or ""
    description = item.get("description") or item.get("snippet") or ""
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

    async def firecrawl_search(self, query: str) -> str:
        """复用 tools.web 的 Firecrawl 实现，这里只负责重试与 markdown 渲染。"""
        if not _firecrawl_available():
            return ("Firecrawl 未配置：需要 FIRECRAWL_API_KEY 并安装 firecrawl-py；"
                    "请改用其他搜索来源。")
        last_exc: Exception | None = None
        for _ in range(SEARCH_MAX_RETRIES):
            try:
                items = await _firecrawl_items(query, FIRECRAWL_SEARCH_LIMIT)
            except Exception as exc:  # 网络或 SDK 异常都重试
                last_exc = exc
                items = None
            if items:
                body = "\n\n".join(_format_ref_item(i, item) for i, item in enumerate(items, start=1))
                return f"A web search for '{query}' returned:\n\n## Web Results\n{body}"
            if last_exc is None:
                break
            await asyncio.sleep(1)
        if last_exc is not None:
            return (f"Firecrawl search Timeout or error ({type(last_exc).__name__}: {last_exc}); "
                    "return None, Please try again later.")
        return f"No results found for query: '{query}'. Use a less specific query."

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

