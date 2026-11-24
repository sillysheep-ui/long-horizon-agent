"""ChinaTravel 沙盒检索：结构化字段、关键词打分与产品接入。"""

import asyncio
import json
from pathlib import Path

import pytest

import product.tool_adapters as adapters
from product.tool_adapters import ResearchTools
from tools import sandbox


def _write(directory: Path, name: str, rows: list[dict]) -> None:
    with (directory / (name + ".jsonl")).open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture()
def sandbox_dir(tmp_path, monkeypatch):
    _write(tmp_path, "attractions", [
        {"city": "杭州", "name": "西湖风景名胜区", "type": "风景名胜", "lat": 30.24, "lon": 120.15,
         "opentime": "00:00", "endtime": "24:00", "price": 0,
         "recommendmintime": 1.0, "recommendmaxtime": 3.0},
        {"city": "杭州", "name": "灵隐寺", "type": "寺庙", "lat": 30.24, "lon": 120.10,
         "opentime": "06:30", "endtime": "17:45", "price": 0,
         "recommendmintime": 0.5, "recommendmaxtime": 1.5},
        {"city": "苏州", "name": "拙政园", "type": "园林", "lat": 31.32, "lon": 120.63,
         "opentime": "07:30", "endtime": "17:30", "price": 80,
         "recommendmintime": 1.5, "recommendmaxtime": 2.5},
        {"city": "杭州", "name": "城西公园", "type": "公园", "lat": 30.29, "lon": 120.05,
         "opentime": "06:00", "endtime": "21:00", "price": 0},
        {"city": "杭州", "name": "灵隐寺法物流通处分馆", "type": "寺庙", "lat": 30.24, "lon": 120.10,
         "opentime": "08:00", "endtime": "17:00", "price": 0},
    ])
    _write(tmp_path, "restaurants", [
        {"city": "杭州", "name": "杭州酒家", "cuisine": "杭帮菜", "lat": 30.25, "lon": 120.16,
         "price": 120, "opentime": "11:00", "endtime": "21:00", "recommendedfood": "西湖醋鱼/东坡肉"},
        {"city": "杭州", "name": "楠火锅", "cuisine": "火锅", "lat": 30.28, "lon": 120.17,
         "price": 90, "opentime": "11:00", "endtime": "23:00", "recommendedfood": "毛肚"},
    ])
    monkeypatch.setattr(sandbox, "DEFAULT_DIR", tmp_path)
    sandbox.reset_cache()
    yield tmp_path
    sandbox.reset_cache()


def test_search_matches_name_and_returns_fields(sandbox_dir):
    items = sandbox.search("杭州", "西湖")
    assert items and items[0]["name"] == "西湖风景名胜区"
    assert items[0]["coordinates"] == [120.15, 30.24]
    assert items[0]["opentime"] is None
    assert items[0]["price"] == 0
    assert items[0]["source_table"] == "attractions"


def test_search_matches_cuisine_keyword(sandbox_dir):
    items = sandbox.search("杭州", "火锅")
    assert [item["name"] for item in items] == ["楠火锅"]
    assert items[0]["category"] == "火锅"


def test_search_respects_city(sandbox_dir):
    assert sandbox.search("苏州", "西湖") == []
    assert sandbox.search("苏州", "拙政园")[0]["price"] == 80


def test_search_without_sandbox_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox, "DEFAULT_DIR", tmp_path / "missing")
    sandbox.reset_cache()
    assert sandbox.available() is False
    assert sandbox.search("杭州", "西湖") == []


def test_poi_uses_sandbox_provider(sandbox_dir, monkeypatch):
    monkeypatch.setenv("POI_PROVIDER", "sandbox")
    monkeypatch.setattr(adapters, "sandbox_available", lambda: True)
    evidence = asyncio.run(ResearchTools().poi("西湖", "杭州", "d1-p1"))

    assert evidence["status"] == "ok"
    assert evidence["source"] == "ChinaTravel 沙盒"
    assert evidence["source_kind"] == "sandbox"
    assert evidence["verification_level"] == "source_linked"
    assert evidence["items"][0]["name"] == "西湖风景名胜区"
    assert evidence["items"][0]["coordinates"] == [120.15, 30.24]


def test_city_name_is_normalized(sandbox_dir):
    assert sandbox.search("杭州市", "西湖")
    assert sandbox.search("Hangzhou", "西湖")
    assert sandbox.search("苏州", "西湖") == []


def test_child_spots_are_filtered(sandbox_dir):
    names = [item["name"] for item in sandbox.search("杭州", "灵隐寺")]
    assert names == ["灵隐寺"]


def test_synonym_expands_keyword(sandbox_dir):
    items = sandbox.search("杭州", "摄影")
    assert any(item["name"] == "城西公园" for item in items)


def test_version_includes_dataset_metadata(tmp_path, monkeypatch):
    import json

    from tools import sandbox

    (tmp_path / "attractions.jsonl").write_text('{"name": "西湖", "city": "杭州"}\n', encoding="utf-8")
    (tmp_path / "META.json").write_text(
        json.dumps({"source": "demo", "downloaded_at": "2026-11-20T21:20:00+08:00"}), encoding="utf-8")
    monkeypatch.setattr(sandbox, "DEFAULT_DIR", tmp_path)
    sandbox.reset_cache()
    payload = sandbox.version()
    assert payload["source"] == "demo"
    assert payload["downloaded_at"].startswith("2026-11-20")
    assert payload["rows"] == 1
