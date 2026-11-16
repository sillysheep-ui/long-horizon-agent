"""产品侧工具适配器：POI 结构化映射与失败降级。"""

import asyncio

import pytest

import product.tool_adapters as adapters
from product.tool_adapters import ResearchTools
from tools import poi as poi_module


def test_poi_maps_structured_items(monkeypatch):
    async def fake_search(address, region=None):
        assert address == "西湖"
        assert region == "杭州"
        return {"pois": [{"name": "西湖风景名胜区", "address": "西湖区", "location": "120.1,30.2",
                          "cityname": "杭州市", "adcode": "330100"}]}

    monkeypatch.setenv("AMAP_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(adapters, "search_pois", fake_search)
    evidence = asyncio.run(ResearchTools().poi("西湖", "杭州", "d1-p1"))

    assert evidence["status"] == "ok"
    assert evidence["source_kind"] == "map_poi"
    assert evidence["verification_level"] == "address_verified"
    assert "西湖" in evidence["items"][0]["name"]
    assert "门票价格" not in evidence["items"][0]["name"]
    assert evidence["items"][0]["coordinates"] == [120.1, 30.2]


def test_poi_falls_back_to_web_search(monkeypatch):
    async def broken_search(address, region=None):
        raise RuntimeError("API response error: INVALID_USER_KEY")

    async def fake_web(self, keyword, city, evidence_id):
        return {"id": evidence_id, "status": "ok", "source_kind": "web_search", "items": []}

    monkeypatch.setenv("AMAP_MAPS_API_KEY", "test-key")
    monkeypatch.setattr(adapters, "search_pois", broken_search)
    monkeypatch.setattr(ResearchTools, "web_search", fake_web)
    evidence = asyncio.run(ResearchTools().poi("西湖", "杭州", "d1-p1"))

    assert evidence["source_kind"] == "web_search"


def test_search_pois_requires_key(monkeypatch):
    monkeypatch.delenv("AMAP_MAPS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AMAP_MAPS_API_KEY"):
        asyncio.run(poi_module.search_pois("西湖"))


def test_web_search_ranks_hits(monkeypatch):
    async def fake_web(query, limit=8):
        return [
            {"title": "杭州西湖风景名胜区门票价格", "url": "https://example.com/xihu",
             "snippet": "西湖 5A 景区"},
            {"title": "无关网页", "url": "https://example.com/other", "snippet": "其他城市"},
        ]

    monkeypatch.delenv("AMAP_MAPS_API_KEY", raising=False)
    monkeypatch.setattr(adapters, "search_web", fake_web)
    evidence = asyncio.run(ResearchTools().web_search("西湖", "杭州", "d1-p1"))

    assert evidence["status"] == "ok"
    assert evidence["source_kind"] == "web_search"
    assert "西湖" in evidence["items"][0]["name"]
    assert "门票价格" not in evidence["items"][0]["name"]


def test_web_search_empty_and_failed(monkeypatch):
    async def empty_web(query, limit=8):
        return []

    async def broken_web(query, limit=8):
        raise RuntimeError("未配置可用的搜索服务")

    monkeypatch.setattr(adapters, "search_web", empty_web)
    empty = asyncio.run(ResearchTools().web_search("西湖", "杭州", "d1-p1"))
    assert empty["status"] == "empty"

    monkeypatch.setattr(adapters, "search_web", broken_web)
    missing = asyncio.run(ResearchTools().web_search("西湖", "杭州", "d1-p1"))
    assert missing["status"] == "unavailable"
    assert missing["source_kind"] == "none"
