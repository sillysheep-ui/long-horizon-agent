"""增量修改：目标日期解析、计划构造与只重排指定日期。"""

import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from product.orchestrator import AgentWorkflow
from product.planner import queries_for_day
from product.revision import (
    compact_days,
    day_of_evidence,
    days_to_revision_plan,
    describe_targets,
    merge_days,
    parse_target_days,
    split_by_targets,
)
from product.store import Store


def _evidence(evidence_id, keyword, city):
    return {
        "id": evidence_id,
        "kind": "poi",
        "query": keyword,
        "city": city,
        "status": "ok",
        "source": "高德",
        "source_kind": "map_poi",
        "verification_level": "address_verified",
        "simulated": False,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "items": [{"name": city + keyword, "address": city + "某路", "city": city}],
        "reused": False,
    }


class FakeTools:
    def __init__(self):
        self.calls = []

    async def poi(self, keyword, city, evidence_id):
        self.calls.append(evidence_id)
        return _evidence(evidence_id, keyword, city)


class FakeModel:
    """第一次规划灵隐寺，第二次（修改后）规划九溪烟树。"""

    def __init__(self):
        self.roles = []
        self.answer_payloads = []

    def _second_query(self, index):
        return "九溪烟树" if index > 1 else "灵隐寺"

    async def complete(self, role, system, user, max_tokens=1200):
        self.roles.append(role)
        if role == "需求 Agent":
            content = json.dumps({"departure_city": "上海", "destinations": ["杭州"], "days": 2,
                                  "budget": "1500", "preferences": [], "missing_required": [],
                                  "conflicts": []}, ensure_ascii=False)
        elif role == "规划 Agent":
            second = self._second_query(self.roles.count("规划 Agent"))
            content = json.dumps({"days": [
                {"day": 1, "city": "杭州", "poi_queries": ["西湖"]},
                {"day": 2, "city": "杭州", "poi_queries": [second]},
            ]}, ensure_ascii=False)
        else:
            payload = json.loads(user)
            self.answer_payloads.append(payload)
            days = []
            for item in payload["evidence"]:
                number = int(item["id"].split("-")[0][1:])
                name = item["items"][0]["name"]
                days.append({"day": number, "city": item["city"], "activities": [{
                    "title": name, "display_title": name, "start": "09:30", "duration": "2 小时",
                    "description": "沿湖走一段", "transport": "地铁", "cost": "未核实",
                    "evidence_id": item["id"]}]})
            content = json.dumps({"summary": "按来源整理", "days": days, "notes": []}, ensure_ascii=False)
        return {"role": role, "provider": "fake", "model": "fake", "content": content}


def test_parse_target_days_chinese_and_last_day():
    assert parse_target_days("第二天换成安静的地方", 3) == [2]
    assert parse_target_days("第一天和第三天都想改", 3) == [1, 3]
    assert parse_target_days("最后一天别排太满", 3) == [3]
    assert parse_target_days("day 2 换个酒店", 3) == [2]


def test_parse_target_days_ignores_out_of_range_and_countdown():
    assert parse_target_days("第五天换个地方", 3) == []
    assert parse_target_days("倒数第二天想吃好点", 3) == []
    assert parse_target_days("随便改改", 3) == []
    assert parse_target_days("第十天", 10) == [10]


def test_merge_days_keeps_other_days():
    previous = [{"day": 1, "activities": ["a"]}, {"day": 2, "activities": ["b"]}]
    new = [{"day": 2, "activities": ["c"]}]
    merged = merge_days(previous, new, [2])
    assert [day["day"] for day in merged] == [1, 2]
    assert merged[0]["activities"] == ["a"]
    assert merged[1]["activities"] == ["c"]


def test_revision_plan_only_targets_named_day():
    days = [{"day": 1, "city": "杭州", "poi_queries": ["西湖"]},
            {"day": 2, "city": "杭州", "poi_queries": ["灵隐寺", "九溪"]}]
    plan = days_to_revision_plan(days, [2], 2, ["杭州"])
    assert [node.id for node in plan.nodes] == ["d2-p1", "d2-p2"]
    assert describe_targets([2]) == "第2天"


class StubbornModel(FakeModel):
    """第一次修改照抄旧计划，被重试后才真正改。"""

    def _second_query(self, index):
        return "灵隐寺" if index <= 2 else "九溪烟树"


def test_planner_retries_when_revision_changes_nothing(tmp_path):
    store = Store(str(tmp_path / "retry.db"))
    tools = FakeTools()
    model = StubbornModel()
    workflow = AgentWorkflow(store, model, tools)
    trip = store.create_trip("从上海去杭州玩两天")
    job = store.claim()
    store.finish(job, asyncio.run(workflow.run(job)))

    store.revise(trip["trip_id"], "第二天换成安静的地方")
    job = store.claim()
    result = asyncio.run(workflow.run(job))
    assert model.roles.count("规划 Agent") == 3
    queries = {item["id"]: item["query"] for item in result["evidence"]}
    assert queries["d2-p1"] == "九溪烟树"


def test_queries_for_day():
    plan = {"days": [{"day": 1, "poi_queries": ["西湖"]}, {"day": 2, "poi_queries": ["灵隐寺"]}]}
    assert queries_for_day(plan, 2) == ["灵隐寺"]
    assert queries_for_day(plan, 3) == []


def test_compact_days_drops_long_fields():
    days = [{"day": 1, "city": "杭州", "activities": [
        {"title": "西湖", "start": "09:30", "duration": "2 小时",
         "description": "很长的描述", "transport": "地铁", "cost": "免费"}]}]
    compacted = compact_days(days)
    assert compacted[0]["activities"] == [{"title": "西湖", "start": "09:30", "duration": "2 小时"}]


def test_split_by_targets_and_day_of_evidence():
    evidence = [_evidence("d1-p1", "西湖", "杭州"), _evidence("d2-p1", "灵隐寺", "杭州")]
    keep, replace = split_by_targets(evidence, [2])
    assert [item["id"] for item in keep] == ["d1-p1"]
    assert [item["id"] for item in replace] == ["d2-p1"]
    assert day_of_evidence("d3-p2") == 3
    assert day_of_evidence("bad") == 0


def test_workflow_only_replans_named_day(tmp_path):
    store = Store(str(tmp_path / "revision.db"))
    tools = FakeTools()
    model = FakeModel()
    workflow = AgentWorkflow(store, model, tools)

    trip = store.create_trip("从上海去杭州玩两天")
    first_job = store.claim()
    first = asyncio.run(workflow.run(first_job))
    store.finish(first_job, first)
    assert first["status"] == "complete"
    assert [item["query"] for item in first["evidence"]] == ["西湖", "灵隐寺"]

    store.revise(trip["trip_id"], "第二天换成安静的地方，别安排寺庙")
    second_job = store.claim()
    assert second_job["revision"] == "第二天换成安静的地方，别安排寺庙"
    second = asyncio.run(workflow.run(second_job))
    store.finish(second_job, second)

    # 只重查第 2 天，第 1 天不重新查询。
    assert tools.calls == ["d1-p1", "d2-p1", "d2-p1"]
    assert model.roles.count("需求 Agent") == 1
    queries = {item["id"]: item["query"] for item in second["evidence"]}
    assert queries["d1-p1"] == "西湖"
    assert queries["d2-p1"] == "九溪烟树"
    assert [day["day"] for day in second["days"]] == [1, 2]
    assert second["days"][0]["activities"] == first["days"][0]["activities"]
    titles = [activity["title"] for activity in second["days"][1]["activities"]]
    assert any("九溪" in title for title in titles)
    # 回答阶段能看到未修改日期的成品，避免重复和节奏冲突。
    kept = model.answer_payloads[-1]["keep_days"]
    assert [day["day"] for day in kept] == [1]
    assert [item["title"] for item in kept[0]["activities"]] == [
        item["title"] for item in first["days"][0]["activities"]
    ]
    assert set(kept[0]["activities"][0]) == {"title", "start", "duration"}


def test_workflow_falls_back_when_no_day_named(tmp_path):
    store = Store(str(tmp_path / "fallback.db"))
    tools = FakeTools()
    model = FakeModel()
    workflow = AgentWorkflow(store, model, tools)
    trip = store.create_trip("从上海去杭州玩两天")
    job = store.claim()
    store.finish(job, asyncio.run(workflow.run(job)))

    store.revise(trip["trip_id"], "预算改成两千")
    job = store.claim()
    result = asyncio.run(workflow.run(job))
    # 没有点名日期：退回整单重排（重新抽取需求），旧证据在 24h 内复用。
    assert model.roles.count("需求 Agent") == 2
    assert tools.calls.count("d1-p1") == 1
    reused = {item["id"]: item["reused"] for item in result["evidence"]}
    assert reused["d1-p1"] is True
    assert result["status"] == "complete"


if __name__ == "__main__":
    unittest.main()
