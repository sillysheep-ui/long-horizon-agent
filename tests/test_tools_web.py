"""tools.web 的 provider 选择与结果归一化。"""

import asyncio

import pytest

from tools import web


def test_search_web_requires_provider(monkeypatch):
    for name in ("FIRECRAWL_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(web, "_firecrawl_available", lambda: False)

    with pytest.raises(RuntimeError, match="未配置可用的搜索服务"):
        asyncio.run(web.search_web("西湖"))


def test_search_web_prefers_firecrawl_then_deepseek(monkeypatch):
    calls = []

    async def empty_firecrawl(query, limit):
        calls.append("firecrawl")
        return None

    async def deepseek(query, limit):
        calls.append("deepseek")
        return [{"title": "西湖", "url": "https://example.com", "snippet": "s"}]

    monkeypatch.setattr(web, "has_provider", lambda: True)
    monkeypatch.setattr(web, "_firecrawl_items", empty_firecrawl)
    monkeypatch.setattr(web, "_deepseek_items", deepseek)

    items = asyncio.run(web.search_web("西湖"))
    assert [item["title"] for item in items] == ["西湖"]
    assert calls == ["firecrawl", "deepseek"]


def test_normalize_drops_incomplete_items():
    items = web._normalize([
        {"title": "有效", "url": "https://example.com", "description": "摘要"},
        {"title": "", "url": "https://example.com"},
        {"title": "非 http", "url": "ftp://example.com"},
        "not-a-dict",
    ])
    assert items == [{"title": "有效", "url": "https://example.com", "snippet": "摘要"}]
