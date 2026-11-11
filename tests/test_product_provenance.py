"""证据来源统一标记的单元测试。"""

import asyncio
from datetime import datetime

import pytest

from product.orchestrator import AgentWorkflow, ResearchTools
from product.provenance import build_evidence, normalize_all, normalize_evidence
from product.store import Store


REQUIRED_FIELDS = ("id", "kind", "query", "city", "status", "source", "source_kind",
                   "verification_level", "simulated", "fetched_at", "reason", "items", "reused")


def assert_complete(evidence, status=None):
    for field in REQUIRED_FIELDS:
        assert field in evidence, field
    if status is not None:
        assert evidence["status"] == status
    # 历史记录没有查询时间时保持为空，页面显示“查询时间未知”，不编造时间。
    if evidence["fetched_at"]:
        datetime.fromisoformat(evidence["fetched_at"])


def test_build_evidence_marks_map_poi_as_address_verified():
    evidence = build_evidence("d1-p1", "poi", "西湖", "杭州", "ok",
                              items=[{"name": "西湖", "address": "杭州"}],
                              source="高德地图 POI", source_kind="map_poi")
    assert_complete(evidence, "ok")
    assert evidence["verification_level"] == "address_verified"
    assert evidence["simulated"] is False


def test_build_evidence_keeps_web_sources_as_source_linked():
    evidence = build_evidence("d1-p1", "web", "西湖", "杭州", "ok",
                              items=[{"name": "西湖风景区", "address": ""}],
                              source="DeepSeek 网页搜索", source_kind="web_search")
    assert evidence["verification_level"] == "source_linked"


def test_failed_evidence_never_claims_verification():
    evidence = build_evidence("d1-p1", "web", "西湖", "杭州", "failed",
                              source="DeepSeek 网页搜索", source_kind="web_search",
                              verification_level="source_linked", reason="网页搜索暂时不可用")
    assert_complete(evidence, "failed")
    assert evidence["verification_level"] in ("none", "unverified")
    assert evidence["reason"]
    assert evidence["items"] == []


def test_simulated_evidence_is_never_verified():
    evidence = build_evidence("d1-p1", "train", "高铁", "杭州", "ok",
                              items=[{"name": "G1234"}], source="模拟车次",
                              source_kind="simulated", simulated=True,
                              verification_level="address_verified")
    assert evidence["simulated"] is True
    assert evidence["verification_level"] == "unverified"


def test_build_evidence_rejects_unknown_status():
    with pytest.raises(ValueError):
        build_evidence("d1-p1", "poi", "西湖", "杭州", "success")


def test_build_evidence_rejects_unknown_source_kind():
    with pytest.raises(ValueError):
        build_evidence("d1-p1", "poi", "西湖", "杭州", "ok", source_kind="crawler")


def test_normalize_evidence_upgrades_legacy_record():
    legacy = {"id": "d1-p1", "kind": "poi", "query": "西湖", "city": "杭州",
              "status": "ok", "items": [{"name": "西湖"}]}
    evidence = normalize_evidence(legacy)
    assert_complete(evidence, "ok")
    assert evidence["source_kind"] == "map_poi"
    assert evidence["verification_level"] == "address_verified"
    assert evidence["fetched_at"] == ""
    assert evidence["reused"] is False


def test_normalize_all_keeps_existing_values():
    items = normalize_all([{"id": "d1-p1", "kind": "web", "query": "西湖", "city": "杭州",
                            "status": "ok", "items": [], "source": "DeepSeek 网页搜索",
                            "source_kind": "web_search", "fetched_at": "2026-11-11T08:00:00+00:00",
                            "reused": True}])
    assert items[0]["source"] == "DeepSeek 网页搜索"
    assert items[0]["fetched_at"] == "2026-11-11T08:00:00+00:00"
    assert items[0]["reused"] is True


def test_tool_without_keys_reports_unavailable(monkeypatch):
    for name in ("AMAP_MAPS_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    evidence = asyncio.run(ResearchTools().poi("西湖", "杭州", "d1-p1"))
    assert_complete(evidence, "unavailable")
    assert evidence["source_kind"] == "none"
    assert evidence["verification_level"] == "none"
    assert "未配置" in evidence["reason"]


class FakeModel:
    async def complete(self, role, system, user, max_tokens=1200):
        contents = {
            "需求 Agent": '{"destinations":["杭州"],"days":1}',
            "规划 Agent": '{"days":[{"day":1,"city":"杭州","poi_queries":["西湖"]}]}',
            "回答 Agent": '{"summary":"杭州一日游","days":[{"day":1,"city":"杭州","activities":[{"title":"西湖","description":"散步","evidence_id":"d1-p1"}]}],"notes":[]}',
        }
        return {"role": role, "provider": "fake", "model": "fake", "content": contents[role]}


class LegacyTools:
    """模拟尚未改造的旧工具：只返回最少字段。"""

    async def poi(self, keyword, city, evidence_id):
        return {"id": evidence_id, "kind": "poi", "query": keyword, "city": city,
                "status": "ok", "items": [{"name": "西湖", "address": "杭州"}]}


def test_workflow_normalizes_legacy_tool_output(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    store.create_trip("从上海去杭州玩一天")
    result = asyncio.run(AgentWorkflow(store, FakeModel(), LegacyTools()).run(store.claim()))
    evidence = result["evidence"][0]
    assert_complete(evidence, "ok")
    assert evidence["verification_level"] == "address_verified"
