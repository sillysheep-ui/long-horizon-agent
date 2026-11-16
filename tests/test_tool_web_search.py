"""tools/tool_web_search：复用 tools.web 的 Firecrawl 分支（无第三方依赖的环境也能测）。"""

import asyncio
import sys
import types

import pytest


def _stub_qwen_agent():
    if "qwen_agent.tools.base" in sys.modules:
        return
    qwen_agent = types.ModuleType("qwen_agent")
    tools_pkg = types.ModuleType("qwen_agent.tools")
    base = types.ModuleType("qwen_agent.tools.base")

    class BaseTool:
        def __init__(self, cfg=None):
            self.cfg = cfg

    def register_tool(name, allow_overwrite=False):
        def deco(cls):
            return cls
        return deco

    base.BaseTool = BaseTool
    base.register_tool = register_tool
    tools_pkg.base = base
    qwen_agent.tools = tools_pkg
    sys.modules["qwen_agent"] = qwen_agent
    sys.modules["qwen_agent.tools"] = tools_pkg
    sys.modules["qwen_agent.tools.base"] = base


@pytest.fixture()
def web_tool(monkeypatch):
    _stub_qwen_agent()
    from tools import tool_web_search as module
    monkeypatch.setattr(module, "_firecrawl_available", lambda: True)
    return module


def test_unconfigured_returns_hint(monkeypatch):
    _stub_qwen_agent()
    from tools import tool_web_search as module
    monkeypatch.setattr(module, "_firecrawl_available", lambda: False)
    tool = module.WebSearch()
    text = asyncio.run(tool.firecrawl_search("西湖"))
    assert "FIRECRAWL_API_KEY" in text


def test_formats_items_as_markdown(web_tool, monkeypatch):
    async def fake_items(query, limit):
        assert query == "西湖"
        return [{"title": "西湖景区", "url": "https://example.com", "snippet": "5A"}]

    monkeypatch.setattr(web_tool, "_firecrawl_items", fake_items)
    text = asyncio.run(web_tool.WebSearch().firecrawl_search("西湖"))
    assert "A web search for '西湖' returned:" in text
    assert "[西湖景区](https://example.com)" in text
    assert "snippet: 5A" in text


def test_empty_results_message(web_tool, monkeypatch):
    async def no_items(query, limit):
        return None

    monkeypatch.setattr(web_tool, "_firecrawl_items", no_items)
    text = asyncio.run(web_tool.WebSearch().firecrawl_search("西湖"))
    assert "No results found" in text
