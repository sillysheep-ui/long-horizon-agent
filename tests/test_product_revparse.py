"""修改单解析：模型给候选，规则做校验。"""

import asyncio
import json

from product.prompts import build_answer_system
from product.revparse import parse_revision, validate_revision


def test_validate_keeps_valid_fields():
    parsed = {"action": "modify", "target_days": [2],
              "attributes": ["quiet", "museum"], "changes": {"budget": 2000},
              "confidence": 0.9}
    out = validate_revision(parsed, 3, {"budget": 1500})
    assert out["target_days"] == [2]
    assert out["attributes"] == ["quiet", "museum"]
    assert out["changes"] == {"budget": 2000}


def test_validate_drops_out_of_range_days():
    parsed = {"target_days": [0, 4, 2], "confidence": 0.9}
    assert validate_revision(parsed, 3, {})["target_days"] == [2]


def test_validate_drops_unknown_attributes():
    parsed = {"attributes": ["随便", "quiet", "quiet"], "confidence": 0.9}
    assert validate_revision(parsed, 2, {})["attributes"] == ["quiet"]


def test_validate_rejects_unknown_city():
    parsed = {"changes": {"city": "火星"}, "confidence": 0.9}
    assert validate_revision(parsed, 2, {"destinations": ["杭州"]})["changes"] == {}
    parsed = {"changes": {"city": "苏州"}, "confidence": 0.9}
    assert validate_revision(parsed, 2, {"destinations": ["杭州"]})["changes"] == {"destinations": ["苏州"]}


def test_low_confidence_returns_empty():
    parsed = {"target_days": [2], "attributes": ["quiet"], "confidence": 0.3}
    out = validate_revision(parsed, 3, {})
    assert out["target_days"] == [] and out["attributes"] == []


def test_parse_revision_uses_model_and_validates():
    class FakeModel:
        async def complete(self, role, system, user, max_tokens=1200):
            payload = {"target_days": [2], "attributes": ["quiet"], "confidence": 0.8}
            return {"content": json.dumps(payload), "provider": "fake", "model": "fake", "role": role}

    calls = []
    out = asyncio.run(parse_revision(FakeModel(), "第二天安静一点", 3, {}, calls))
    assert out["target_days"] == [2]
    assert out["attributes"] == ["quiet"]
    assert calls and calls[0]["role"] == "修改解析"


def test_answer_prompt_includes_requirements():
    text = build_answer_system("normal", [2], ["安静"])
    assert "本次修改的额外要求：安静" in text
