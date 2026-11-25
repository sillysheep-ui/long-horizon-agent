"""结构契约：模型输出跑偏时归一到安全形状，并把问题记进 trace。"""

import asyncio
import json
from datetime import UTC, datetime

from product.orchestrator import AgentWorkflow
from product.schemas import parse_answer, parse_intake, parse_plan
from product.store import Store


def test_intake_normalizes_string_lists_and_numbers():
    data, issues = parse_intake({
        "departure_city": "上海",
        "destinations": "杭州",
        "days": "2 天",
        "people": "3人",
        "budget": 1500,
    })
    assert data["destinations"] == ["杭州"]
    assert data["days"] == 2
    assert data["people"] == 3
    assert data["budget"] == "1500"
    assert any("destinations" in issue for issue in issues)


def test_intake_keeps_unknown_fields():
    data, _ = parse_intake({"destinations": ["杭州"], "days": 1, "custom": {"a": 1}})
    assert data["custom"] == {"a": 1}


def test_intake_non_object_becomes_empty_with_issue():
    data, issues = parse_intake(["杭州", "两天"])
    assert data == {}
    assert issues and "顶层" in issues[0]


def test_plan_drops_non_dict_days():
    data, issues = parse_plan({"days": [{"day": 1, "city": "杭州", "poi_queries": "西湖"},
                                       "第二天", None]})
    assert len(data["days"]) == 1
    assert data["days"][0]["poi_queries"] == ["西湖"]
    assert any("days[1]" in issue for issue in issues)


def test_answer_fills_defaults_and_reports_shape():
    data, issues = parse_answer({
        "summary": "两天",
        "days": [{"day": 1, "activities": [{"title": "西湖", "evidence_id": "d1-p1"}]}],
        "notes": "带身份证",
    })
    day = data["days"][0]
    assert day["activities"][0]["duration"] == ""
    assert day["meals"] == []
    assert data["notes"] == ["带身份证"]
    assert any("notes" in issue for issue in issues)


def test_answer_bad_days_does_not_crash():
    data, issues = parse_answer({"days": "三天"})
    assert data["days"] == []
    assert any("days" in issue for issue in issues)


class _StructuredTools:
    async def poi(self, keyword, city, evidence_id):
        return {
            "id": evidence_id,
            "kind": "poi",
            "query": keyword,
            "city": city,
            "status": "ok",
            "source": "沙盒",
            "source_kind": "sandbox",
            "verification_level": "source_linked",
            "simulated": False,
            "fetched_at": datetime.now(UTC).isoformat(),
            "items": [{"name": city + keyword, "address": city + "某路", "city": city}],
            "reused": False,
        }


class _LooseModel:
    """故意输出错误结构：目的地给字符串、活动字段缺失、规划多给非对象条目。"""

    async def complete(self, role, system, user, max_tokens=1200):
        if role == "需求 Agent":
            payload = {"departure_city": "上海", "destinations": "杭州", "days": "2",
                       "budget": "1500", "preferences": "自然"}
        elif role == "规划 Agent":
            payload = {"days": [
                {"day": 1, "city": "杭州", "poi_queries": ["西湖", "灵隐寺"]},
                {"day": 2, "city": "杭州", "poi_queries": ["西溪湿地", "河坊街"]},
                "第三天",
            ]}
        elif role == "行程评审":
            payload = {"score": 8, "issues": []}
        else:
            request = json.loads(user)
            evidence = [item for item in request.get("evidence") or [] if item.get("id")]
            days = {}
            for item in evidence:
                day = int(item["id"].split("-")[0][1:])
                days.setdefault(day, {"day": day, "city": item["city"], "activities": [], "meals": []})
                name = item["items"][0]["name"]
                days[day]["activities"].append({
                    "title": name, "start": "09:30" if len(days[day]["activities"]) == 0 else "14:00",
                    "duration": "2 小时", "evidence_id": item["id"],
                })
                if len(days[day]["activities"]) == 1:
                    days[day]["meals"].append({"type": "lunch", "title": name,
                                               "start": "12:30", "evidence_id": item["id"]})
            payload = {"summary": "两天杭州", "days": list(days.values()), "notes": "提前订票"}
        return {"role": role, "provider": "fake", "model": "fake",
                "content": json.dumps(payload, ensure_ascii=False)}


def test_workflow_survives_loose_json_and_records_schema_warnings(tmp_path):
    store = Store(str(tmp_path / "schema.db"))
    workflow = AgentWorkflow(store, _LooseModel(), _StructuredTools())
    conversation = store.create_conversation("新对话")
    store.continue_conversation(conversation["conversation_id"], "去杭州玩两天，预算1500")
    job = store.claim()

    result = asyncio.run(workflow.run(job))

    assert result["status"] == "complete"
    assert result["constraints"]["destinations"] == ["杭州"]
    assert result["constraints"]["days"] == 2
    trace = result["trace"]
    names = [child.get("name") for child in trace.get("children") or []]
    assert "schema:intake" in names
    intake_span = next(child for child in trace["children"] if child.get("name") == "schema:intake")
    assert any("destinations" in issue for issue in intake_span["attributes"]["issues"])
    assert "schema:planning" in names
