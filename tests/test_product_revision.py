"""增量修改：目标日期解析、计划构造与只重排指定日期。"""

import asyncio
import json
import unittest
from datetime import UTC, datetime

from product.orchestrator import AgentWorkflow, _finalize_days
from product.planner import queries_for_day
from product.postprocess import update_constraints
from product.revision import (
    compact_days,
    day_of_evidence,
    days_to_revision_plan,
    describe_targets,
    merge_days,
    out_of_range_days,
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
        "fetched_at": datetime.now(UTC).isoformat(),
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


class LazyModel(FakeModel):
    """两次规划都照抄旧计划。"""

    def _second_query(self, index):
        return "灵隐寺"


def test_revision_marks_noop_when_nothing_changed(tmp_path):
    store = Store(str(tmp_path / "noop.db"))
    tools = FakeTools()
    workflow = AgentWorkflow(store, LazyModel(), tools)
    trip = store.create_trip("从上海去杭州玩两天")
    job = store.claim()
    store.finish(job, asyncio.run(workflow.run(job)))

    store.revise(trip["trip_id"], "第二天换成安静的地方")
    job = store.claim()
    result = asyncio.run(workflow.run(job))
    assert result["revision_applied"] is False
    assert "没有找到新的可追溯地点" in result["notes"][0]


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
    assert second["revision_applied"] is True
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


def test_revision_payload_includes_history_and_target_current(tmp_path):
    store = Store(str(tmp_path / "history.db"))
    tools = FakeTools()
    model = FakeModel()
    workflow = AgentWorkflow(store, model, tools)
    conversation = store.create_conversation("杭州周末")
    cid = conversation["conversation_id"]

    store.continue_conversation(cid, "从上海去杭州玩两天")
    first_job = store.claim()
    store.finish(first_job, asyncio.run(workflow.run(first_job)))
    store.record_reply(first_job["id"], store.latest_result(cid))

    store.continue_conversation(cid, "第二天换成安静的地方")
    second_job = store.claim()
    asyncio.run(workflow.run(second_job))

    payload = model.answer_payloads[-1]
    history = payload["history"]
    # 上文用于理解指代：有用户原话和上一轮回复，但不含本轮这句话。
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert "杭州" in history[0]["content"]
    assert all("第二天换成安静的地方" not in item["content"] for item in history)
    # 目标日当前安排：模型要知道第 2 天原来排了什么。
    current = payload["target_days_current"]
    assert [day["day"] for day in current] == [2]
    assert current[0]["activities"]


if __name__ == "__main__":
    unittest.main()


class ExplodingModel:
    """如果走到模型调用就立刻失败，用来证明越界日期被确定性拦下。"""

    async def complete(self, role, system, user, max_tokens=1200):
        raise AssertionError("不应调用模型：%s" % role)


def test_out_of_range_day_is_clarified_without_model_call(tmp_path):
    store = Store(str(tmp_path / "range.db"))
    tools = FakeTools()
    workflow = AgentWorkflow(store, FakeModel(), tools)
    trip = store.create_trip("杭州两天，轻松一点，预算1000元")
    job = store.claim()
    store.finish(job, asyncio.run(workflow.run(job)))

    store.revise(trip["trip_id"], "第五天去乌镇")
    job = store.claim()
    result = asyncio.run(AgentWorkflow(store, ExplodingModel(), tools).run(job))
    assert result["status"] == "needs_clarification"
    assert "2 天" in result["question"] and "5" in result["question"]
    assert result["days"] == []


def test_out_of_range_days_only_reports_beyond_total():
    assert out_of_range_days("第五天去乌镇", 2) == [5]
    assert out_of_range_days("第二天换成博物馆", 2) == []
    assert out_of_range_days("day 4 加一天", 3) == [4]


def test_finalize_keeps_untouched_days_verbatim():
    kept_day = {"day": 1, "city": "杭州", "activities": [
        {"title": "西湖", "start": "09:30"}, {"title": "灵隐寺", "start": "13:30"}]}
    cleaned = [{"day": 2, "city": "杭州", "activities": [{"title": "龙井村", "start": "20:00", "evidence_id": "d2-p1"}]}]
    days = _finalize_days(cleaned, [_evidence("d2-p1", "九溪", "杭州")], [{"day": 2, "city": "杭州"}],
                          keep_days=[kept_day])
    assert [day["day"] for day in days] == [1, 2]
    assert [activity["title"] for activity in days[0]["activities"]] == ["西湖", "灵隐寺"]
    assert any(activity["title"] == "龙井村" and activity["start"] == "20:00"
               for activity in days[1]["activities"])
    assert len(days[1]["activities"]) == 2  # 目标日仍会补足到 2 个


def test_update_constraints_detects_budget_change():
    assert update_constraints({"budget": 1200}, "第二天换成博物馆，并把预算提到2000") == {"budget": 2000}
    assert update_constraints({"budget": 1500}, "预算改成两千") == {"budget": 2000}
    assert update_constraints({"budget": 1200}, "第二天换成博物馆") == {}


def test_update_constraints_detects_days_change():
    assert update_constraints({"days": 2}, "再加一天") == {"days": 3}
    assert update_constraints({"days": 2}, "改成3天") == {"days": 3}
    assert update_constraints({"days": 2}, "预算提到 2000，再加一天") == {"budget": 2000, "days": 3}


def test_budget_change_escalates_to_full_replan(tmp_path):
    store = Store(str(tmp_path / "escalate.db"))
    workflow = AgentWorkflow(store, FakeModel(), FakeTools())
    trip = store.create_trip("从上海去杭州玩两天")
    job = store.claim()
    store.finish(job, asyncio.run(workflow.run(job)))

    store.revise(trip["trip_id"], "第二天换成博物馆，并把预算提到2000")
    job = store.claim()
    result = asyncio.run(workflow.run(job))
    assert result["constraints"]["budget"] == 2000
    queries = {item["id"]: item["query"] for item in result["evidence"]}
    assert "d1-p1" in queries and "d2-p1" in queries


def test_update_constraints_detects_pace_change():
    assert update_constraints({"pace": "normal"}, "整体放慢一点") == {"pace": "relaxed"}
    assert update_constraints({"pace": "relaxed"}, "还是想紧凑些") == {"pace": "packed"}
    assert update_constraints({"pace": "relaxed"}, "慢慢逛就好") == {}
