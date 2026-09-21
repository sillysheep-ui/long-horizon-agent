import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("AMAP_MAPS_API_KEY", "test-key")

try:
    import qwen_agent.tools.base  # noqa: F401
except ModuleNotFoundError:
    base_module = types.ModuleType("qwen_agent.tools.base")

    class BaseTool:
        def __init__(self, cfg=None):
            self.cfg = cfg

    def register_tool(*args, **kwargs):
        return lambda cls: cls

    base_module.BaseTool = BaseTool
    base_module.register_tool = register_tool
    tools_module = types.ModuleType("qwen_agent.tools")
    tools_module.base = base_module
    package = types.ModuleType("qwen_agent")
    package.tools = tools_module
    sys.modules["qwen_agent"] = package
    sys.modules["qwen_agent.tools"] = tools_module
    sys.modules["qwen_agent.tools.base"] = base_module

from tools.tool_around_search import AroundSearch

from tools.tool_route_planning import resolve_point


class AroundSearchFallbackTest(unittest.IsolatedAsyncioTestCase):
    def test_expands_broad_culture_keyword(self):
        keywords = AroundSearch._fallback_keywords("科教 文化")
        self.assertIn("博物馆", keywords)
        self.assertIn("图书馆", keywords)
        self.assertLessEqual(len(keywords), 5)

    async def test_resolves_place_name_before_search(self):
        responses = [
            ({
                "status": "1",
                "geocodes": [{
                    "location": "114.100000,22.550000",
                    "formatted_address": "广东省深圳市华日汽车检测站",
                }],
            }, False),
            ({
                "status": "1",
                "pois": [{"id": "p1", "name": "某博物馆", "location": "114.11,22.56"}],
            }, False),
        ]
        mocked = AsyncMock(side_effect=responses)
        with patch("tools.tool_around_search.amap_get", mocked):
            result = await AroundSearch().call({
                "location": "华日汽车检测站",
                "region": "深圳",
                "keyword": "博物馆",
            })
        self.assertIn("中心地点解析为", result)
        self.assertIn("某博物馆", result)
        search_params = mocked.await_args_list[1].args[1]
        self.assertEqual(search_params["location"], "114.100000,22.550000")

    async def test_retries_empty_compound_category_with_specific_terms(self):
        async def fake_get(url, params):
            keyword = params.get("keywords")
            if keyword == "博物馆":
                return {"status": "1", "pois": [{"id": "p1", "name": "城市博物馆"}]}, False
            return {"status": "1", "pois": []}, False

        with patch("tools.tool_around_search.amap_get", AsyncMock(side_effect=fake_get)):
            result = await AroundSearch().call({
                "location": "114.100000,22.550000",
                "keyword": "科教 文化",
            })
        self.assertIn("已按具体类别补查", result)
        self.assertIn("城市博物馆", result)

    async def test_route_resolves_named_point(self):
        mocked = AsyncMock(return_value=({"status": "1", "geocodes": [{
            "location": "113.293000,22.805000", "formatted_address": "广东省佛山市顺德区"
        }]}, False))
        with patch("tools.tool_route_planning.amap_get", mocked):
            location, address = await resolve_point("顺德")
        self.assertEqual(location, "113.293000,22.805000")
        self.assertIn("顺德区", address)


if __name__ == "__main__":
    unittest.main()
