"""分阶段与分支评测的规则测试：不调模型，只用合成 trace。"""

from evaluation.stage_checks import evaluator_health, summarize_trajectory


def span(name, status="ok", attributes=None, children=None, error_type=None):
    item = {"name": name, "kind": "tool" if children is not None or name in ("poi", "weather") else "stage",
            "status": status, "latency_ms": 100, "attributes": attributes or {}}
    if children:
        item["children"] = children
    if error_type:
        item["error_type"] = error_type
    return item


def build_trace(intake=None, planning=None, retrieval=None, coverage=None, answer=None):
    children = [item for item in (intake, planning, retrieval, coverage, answer) if item]
    return {"name": "run", "kind": "run", "status": "ok", "children": children}


def base_result(**overrides):
    result = {
        "status": "complete",
        "constraints": {"days": 2, "destinations": ["杭州"]},
        "plan": {"days": [{"day": 1, "city": "杭州", "poi_queries": ["西湖", "龙井村"]},
                          {"day": 2, "city": "杭州", "poi_queries": ["西溪湿地", "九溪"]}]},
        "days": [
            {"day": 1, "city": "杭州", "activities": [{"start": "09:30"}, {"start": "13:30"}]},
            {"day": 2, "city": "杭州", "activities": [{"start": "09:30"}, {"start": "13:30"}]},
        ],
        "evidence": [
            {"id": "d1-p1", "status": "ok", "items": [{"name": "西湖", "snippet": "免费", "address": ""}]},
            {"id": "d2-p1", "status": "ok", "items": [{"name": "西溪湿地", "snippet": "门票 80 元", "address": ""}]},
        ],
    }
    result.update(overrides)
    return result


CASE = {"id": "t1", "days": 2, "cities": ["杭州"], "expect": ""}


def full_trace():
    return build_trace(
        intake=span("intake", attributes={"days": 2, "cities": ["杭州"], "days_declared": True}),
        planning=span("planning", attributes={"per_day_queries": [2, 2], "nodes": 4}),
        retrieval=span("retrieval", attributes={"calls": 4, "ok": 4, "empty": 0, "failed": 0,
                                                "unavailable": 0, "critical_path_ms": 1200},
                       children=[span("poi", attributes={"day": 1})]),
        coverage=span("coverage", attributes={"per_day_ok": [2, 2], "uncovered_days": []}),
        answer=span("answer", attributes={"raw": 4, "grounded": 4, "dropped": 0, "filled": 0,
                                          "final": 4, "fabricated_cost": 0, "time_order_ok": True}),
    )


def test_full_trace_all_stages_pass():
    result = base_result(trace=full_trace())
    trajectory = summarize_trajectory(CASE, result)
    assert trajectory["stages"]["intake"]["score"] == 1.0
    assert trajectory["stages"]["planning"]["score"] == 1.0
    assert trajectory["stages"]["retrieval"]["score"] == 1.0
    assert trajectory["stages"]["answer"]["score"] == 1.0
    assert trajectory["attribution"]["primary"] == "none"
    assert trajectory["errors"] == []


def test_planning_too_few_queries_blames_model():
    trace = full_trace()
    for child in trace["children"]:
        if child["name"] == "planning":
            child["attributes"]["per_day_queries"] = [1, 2]
    trajectory = summarize_trajectory(CASE, base_result(trace=trace))
    assert trajectory["stages"]["planning"]["score"] < 1
    assert any(error["type"] == "too_few_queries" for error in trajectory["errors"])
    assert trajectory["attribution"]["primary"] == "model"
    assert trajectory["attribution"]["action"]


def test_retrieval_failure_blames_tool():
    trace = full_trace()
    for child in trace["children"]:
        if child["name"] == "retrieval":
            child["attributes"].update({"calls": 4, "ok": 1, "failed": 3, "unavailable": 1})
    trajectory = summarize_trajectory(CASE, base_result(trace=trace))
    assert trajectory["stages"]["retrieval"]["score"] < 1
    assert trajectory["attribution"]["primary"] == "tool"


def test_missing_trace_is_unknown_not_failure():
    trajectory = summarize_trajectory(CASE, base_result())
    assert trajectory["stages"]["planning"]["score"] is None
    assert trajectory["stages"]["answer"]["score"] is None
    assert trajectory["errors"] == []
    assert trajectory["attribution"]["primary"] == "none"


def test_clarify_case_only_checks_intake():
    case = {"id": "c1", "days": None, "cities": [], "expect": "needs_clarification"}
    result = {"status": "needs_clarification", "constraints": {"days": 2, "destinations": ["杭州"]},
              "trace": build_trace(intake=span("intake", "failed",
                                               {"days": 2, "cities": ["杭州"], "decision": "clarify"}))}
    trajectory = summarize_trajectory(case, result)
    assert trajectory["stages"]["intake"]["score"] == 1.0
    assert trajectory["stages"]["planning"]["score"] is None
    assert trajectory["errors"] == []


def test_branch_coverage_marks_unimplemented_as_skipped():
    trajectory = summarize_trajectory(CASE, base_result(trace=full_trace()))
    assert trajectory["branches"]["poi"]["applicable"] is True
    assert trajectory["branches"]["poi"]["rate"] == 1.0
    assert trajectory["branches"]["weather"]["applicable"] is False
    assert trajectory["branches"]["weather"]["rate"] is None


def test_fabricated_cost_and_density_fail():
    trace = full_trace()
    for child in trace["children"]:
        if child["name"] == "answer":
            child["attributes"].update({"fabricated_cost": 1, "time_order_ok": False})
    result = base_result(trace=trace)
    result["days"][0]["activities"] = [{"start": "09:30"}]
    trajectory = summarize_trajectory(CASE, result)
    failed = {check["name"] for check in trajectory["stages"]["answer"]["checks"] if check["passed"] is False}
    assert {"fabricated", "density", "time_order"} <= failed


def test_evaluator_health_flags_truncation():
    records = [
        {"judge": {"error": "裁判输出被截断", "rubric": None}},
        {"judge": {"error": "", "rubric": 0.8}},
        {"judge": {"rubric": 0.7, "agreement": 0.1}},
    ]
    health = evaluator_health(records)
    assert health["judged"] == 3
    assert health["truncated"] == 1
    assert health["reliable"] is False


def test_evaluator_health_ok():
    records = [{"judge": {"rubric": 0.8}}, {"judge": {"rubric": 0.7}}]
    health = evaluator_health(records)
    assert health["reliable"] is True
