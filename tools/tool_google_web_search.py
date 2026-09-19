# -*- coding: utf-8 -*-
# --------------------------------------------
# 文件描述: google搜索工具 
# --------------------------------------------


import os
import asyncio
import json
import itertools

import aiohttp
from dotenv import load_dotenv
from qwen_agent.tools.base import BaseTool, register_tool


# 加载环境变量
load_dotenv()

SERPAPI_API_KEY = os.environ["SERPAPI_API_KEY"].split(",")
GOOGLE_MAX_RETRIES = int(os.getenv("GOOGLE_MAX_RETRIES", 2))


class RoundRobin:
    def __init__(self, keys):
        # 创建一个无限循环的迭代器
        self.key_cycle = itertools.cycle(keys)

    def get_key(self):
        # 使用 itertools.cycle
        return next(self.key_cycle)


# 轮训
_rr_keys = RoundRobin(SERPAPI_API_KEY)


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

    async def google_search_with_serp(self, query: str, max_results: int=5):
        def contains_chinese_basic(text: str) -> bool:
            return any("\u4e00" <= char <= "\u9fff" for char in text)

        if contains_chinese_basic(query):
            payload = {"q": query, "location": "China", "gl": "cn", "hl": "zh-cn", "num": max_results}
        else:
            payload = {"q": query, "location": "United States", "gl": "us", "hl": "en", "num": max_results}


        last_exc = None
        async with aiohttp.ClientSession() as session:
            for attempt in range(GOOGLE_MAX_RETRIES):
                try:
                    headers = {"X-API-KEY": _rr_keys.get_key(), "Content-Type": "application/json"}
                    async with session.post(
                        "https://google.serper.dev/search",
                        json=payload,
                        headers=headers,
                    ) as resp:
                        if resp.status != 200:
                            continue

                        text = await resp.text()
                        try:
                            results = json.loads(text)
                        except Exception:
                            return f"[Search] Failed to parse response for '{query}'."

                        if "organic" not in results:
                            return f"No results found for query: '{query}'. Use a less specific query."

                        web_snippets = []
                        for idx, page in enumerate(results.get("organic", []), start=1):
                            date_published = (
                                f"\nDate published: {page['date']}"
                                if page.get("date")
                                else ""
                            )
                            source = (
                                f"\nSource: {page['source']}"
                                if page.get("source")
                                else ""
                            )
                            snippet = (
                                f"\n{page['snippet']}" if page.get("snippet") else ""
                            )
                            redacted_version = f"{idx}. [{page.get('title', '')}]({page.get('link', '')}){date_published}{source}\n{snippet}"
                            redacted_version = redacted_version.replace(
                                "Your browser can't play this video.", ""
                            )
                            web_snippets.append(redacted_version)

                        content = (
                            f"A Google search for '{query}' found {len(web_snippets)} results:\n\n## Web Results\n"
                            + "\n\n".join(web_snippets)
                        )
                        return content
                except Exception as e:
                    last_exc = e
                    await asyncio.sleep(1)
                    continue

        return f"Google search Timeout or error ({last_exc}); return None, Please try again later."

    async def search_with_serp(self, query: str):
        return await self.google_search_with_serp(query)

    async def call(self, params: str | dict, **kwargs) -> str:  # type: ignore[override]
        try:
            query = params["query"]
        except Exception:
            return "[WebSearch] Invalid request format: Input must be a JSON object containing 'query' field"

        if isinstance(query, str):
            return await self.search_with_serp(query)

        assert isinstance(query, list)
        tasks = [self.search_with_serp(q) for q in query]
        responses = await asyncio.gather(*tasks)
        return "\n=======\n".join(responses)


if __name__ == "__main__":
    search = WebSearch()
    params = {"query": "春熙路 美食"}
    res = asyncio.run(search.call(params))
    print(res)
