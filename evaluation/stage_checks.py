"""把执行轨迹（trace）与终稿转成阶段检查、分支覆盖和归因。

这里的判定全部是确定性规则，不调用模型：

- `check_*`：把 trace 上的事实转成可计数的检查项；
- `summarize_trajectory`：汇总阶段分、分支覆盖、错误清单与归因；
- `evaluator_health`：统计裁判自身的健康度。

trace 缺失时返回 `passed=None`（unknown），不计入失败，也不改变既有分数。
"""

from typing import Any, Dict, List, Optional

STAGES = ("intake", "planning", "retrieval", "coverage", "answer")
BRANCHES = ("poi", "weather", "hotel", "transport")
NOT_IMPLEMENTED = ("weather", "hotel", "transport")

# 归因权重：同一症状只投一票。
SYMPTOM_WEIGHTS = {
    "tool_error": ("tool", 2),
    "timeout": ("tool", 2),
    "auth_missing": ("tool", 2),
    "too_few_queries": ("model", 2),
    "city_drift": ("model", 2),
    "missing_days": ("model", 2),
    "wrong_city": ("model", 2),
    "should_clarify": ("model", 2),
    "over_clarify": ("model", 2),
    "empty_but_tool_ok": ("data", 2),
    "ungrounded_title": ("model", 1),
    "fabricated_cost": ("model", 1),
    "density_miss": ("model", 1),
    "bad_time_order": ("model", 1),
    "uncovered_day": ("data", 2),
    "thin_evidence": ("data", 1),
    "missing_weather": ("tool", 2),
    "missing_hotel": ("tool", 2),
    "missing_transport": ("tool", 2),
    "case_conflict": ("case", 2),
    "judge_error": ("evaluator", 2),
}

ACTIONS = {
    "tool": "检查工具接口、key 与超时设置",
    "model": "检查提示词或更换模型",
    "data": "补充数据源或降低查询难度",
    "case": "修正用例本身",
    "evaluator": "更换裁判或提高输出预算",
    "orchestrator": "检查调度与依赖关系",
}


def _check(name: str, stage: str, passed: Optional[bool],
           error_type: Optional[str] = None, detail: str = "") -> Dict[str, Any]:
    return {"name": name, "stage": stage, "passed": passed,
            "error_type": error_type, "detail": detail}


def _span_of(trace: Optional[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for child in (trace or {}).get("children", []) or []:
        if child.get("name") == name:
            return child
    return None


def _unknown(names: List[str], stage: str) -> List[Dict[str, Any]]:
    return [_check(name, stage, None, "unknown", "缺少 trace") for name in names]


def check_intake(case: Dict[str, Any], result: Dict[str, Any],
                 span: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if span is None:
        return _unknown(["destination", "days", "cities", "clarification"], "intake")
    attributes = span.get("attributes") or {}
    expect_clarify = case.get("expect") == "needs_clarification"
    clarify = result.get("status") == "needs_clarification"
    checks = [_check("clarification", "intake", clarify if expect_clarify else not clarify,
                     "should_clarify" if expect_clarify and not clarify else
                     ("over_clarify" if clarify and not expect_clarify else None))]
    if expect_clarify:
        checks += [_check(name, "intake", None, "unknown", "追问用例不检查行程字段")
                   for name in ("destination", "days", "cities")]
        return checks
    destinations = attributes.get("cities") or []
    checks.append(_check("destination", "intake", bool(destinations), "wrong_city"))
    expected_days = case.get("days")
    actual_days = attributes.get("days")
    checks.append(_check("days", "intake", expected_days is None or actual_days == expected_days,
                         "missing_days", "期望 %s 天，实际 %s 天" % (expected_days, actual_days)))
    expected_cities = case.get("cities") or []
    checks.append(_check("cities", "intake",
                         all(city in destinations for city in expected_cities), "wrong_city",
                         "期望 %s，实际 %s" % (expected_cities, destinations)))
    return checks


def check_planning(case: Dict[str, Any], result: Dict[str, Any],
                   span: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if result.get("status") == "needs_clarification" or case.get("expect") == "needs_clarification":
        return [_check(name, "planning", None, "unknown", "追问用例不进入规划")
                for name in ("days", "per_day_queries", "city_drift", "duplicate_query")]
    if span is None:
        return _unknown(["days", "per_day_queries", "city_drift", "duplicate_query"], "planning")
    attributes = span.get("attributes") or {}
    per_day = attributes.get("per_day_queries") or []
    expected_days = case.get("days")
    checks = [
        _check("days", "planning", expected_days is None or len(per_day) == expected_days,
               "day_mismatch", "计划 %d 天" % len(per_day)),
        _check("per_day_queries", "planning", bool(per_day) and all(count >= 2 for count in per_day),
               "too_few_queries", "每天查询数 %s" % per_day),
    ]
    plan = result.get("plan") or {}
    planned_cities = {str(day.get("city") or "").strip() for day in plan.get("days") or []
                      if isinstance(day, dict)}
    expected_cities = set(case.get("cities") or [])
    drift = [city for city in planned_cities if city and expected_cities and
             not any(city in wanted or wanted in city for wanted in expected_cities)]
    checks.append(_check("city_drift", "planning", not drift, "city_drift", "越界城市 %s" % drift))
    queries = [str(query) for day in plan.get("days") or [] if isinstance(day, dict)
               for query in day.get("poi_queries") or []]
    duplicates = sorted({query for query in queries if queries.count(query) > 1})
    checks.append(_check("duplicate_query", "planning", not duplicates, "duplicate_query",
                         "重复查询 %s" % duplicates[:3]))
    return checks


def check_retrieval(span: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if span is None:
        return _unknown(["success_rate", "auth_missing", "critical_path"], "retrieval")
    attributes = span.get("attributes") or {}
    calls = max(0, int(attributes.get("calls") or 0))
    ok = int(attributes.get("ok") or 0)
    unavailable = int(attributes.get("unavailable") or 0)
    rate = (ok / calls) if calls else 0.0
    return [
        _check("success_rate", "retrieval", calls > 0 and rate >= 0.8, "tool_error",
               "成功率 %.2f" % rate),
        _check("auth_missing", "retrieval", unavailable == 0, "auth_missing",
               "配置缺失 %d 条" % unavailable),
        _check("critical_path", "retrieval",
               float(attributes.get("critical_path_ms") or 0) <= 30000, "timeout",
               "关键路径 %sms" % attributes.get("critical_path_ms")),
    ]


def check_coverage(span: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if span is None:
        return _unknown(["no_gap", "daily_evidence"], "coverage")
    attributes = span.get("attributes") or {}
    per_day = attributes.get("per_day_ok") or []
    uncovered = attributes.get("uncovered_days") or []
    return [
        _check("no_gap", "coverage", not uncovered, "uncovered_day", "未覆盖 %s" % uncovered),
        _check("daily_evidence", "coverage", bool(per_day) and all(count >= 1 for count in per_day),
               "thin_evidence", "每天证据数 %s" % per_day),
    ]


def check_answer(case: Dict[str, Any], result: Dict[str, Any],
                 span: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if result.get("status") == "needs_clarification" or case.get("expect") == "needs_clarification":
        return [_check(name, "answer", None, "unknown", "追问用例不进入回答")
                for name in ("grounded", "fabricated", "density", "time_order", "drop_rate")]
    if span is None:
        return _unknown(["grounded", "fabricated", "density", "time_order", "drop_rate"], "answer")
    attributes = span.get("attributes") or {}
    raw = int(attributes.get("raw") or 0)
    grounded = int(attributes.get("grounded") or 0)
    dropped = int(attributes.get("dropped") or 0)
    final = int(attributes.get("final") or 0)
    density_ok = True
    for day in result.get("days") or []:
        count = len(day.get("activities") or [])
        if count < 2 or count > 3:
            density_ok = False
    return [
        _check("grounded", "answer", raw > 0 and grounded / max(1, raw) >= 0.9, "ungrounded_title",
               "落证据 %d/%d" % (grounded, raw)),
        _check("fabricated", "answer", int(attributes.get("fabricated_cost") or 0) == 0,
               "fabricated_cost"),
        _check("density", "answer", density_ok, "density_miss"),
        _check("time_order", "answer", bool(attributes.get("time_order_ok")), "bad_time_order"),
        _check("drop_rate", "answer", raw > 0 and dropped / max(1, raw) <= 0.3, "density_miss",
               "丢弃 %d/%d" % (dropped, raw)),
        _check("filled", "answer", None, "unknown", "补足 %s 条" % attributes.get("filled")),
        _check("final_nonempty", "answer", final > 0, "density_miss", "最终 %d 条" % final),
    ]


def _branch_coverage(case: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    days = case.get("days") or len(result.get("days") or []) or 0
    evidence = result.get("evidence") or []
    ok_ids = {str(item.get("id")) for item in evidence
              if item.get("status") == "ok" and item.get("items")}
    coverage: Dict[str, Any] = {}
    for branch in BRANCHES:
        covered = []
        for day in range(1, int(days) + 1):
            if any(evidence_id.startswith("d%d-" % day) for evidence_id in ok_ids):
                covered.append(day)
        applicable = branch not in NOT_IMPLEMENTED and bool(days)
        coverage[branch] = {
            "applicable": applicable,
            "covered_days": covered,
            "rate": (len(covered) / int(days)) if days and branch not in NOT_IMPLEMENTED else None,
            "reason": "未接入该工具" if branch in NOT_IMPLEMENTED else "",
        }
    return coverage


def _collect_errors(checks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    errors = []
    for check in checks:
        if check["passed"] is False and check.get("error_type") and check["error_type"] != "unknown":
            errors.append({"stage": check["stage"], "type": check["error_type"],
                           "detail": check.get("detail", "")})
    return errors


def attribute(checks: List[Dict[str, Any]], case: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按症状加权投票，返回主因、置信度与建议动作。"""
    votes: Dict[str, int] = {}
    contributing: List[str] = []
    for error in _collect_errors(checks):
        source, weight = SYMPTOM_WEIGHTS.get(error["type"], ("orchestrator", 1))
        votes[source] = votes.get(source, 0) + weight
        if error["type"] == "clarification" or (case or {}).get("expect") == "needs_clarification":
            continue
        contributing.append(error["type"])
    if not votes:
        return {"primary": "none", "confidence": 0.0, "contributing": [], "action": ""}
    primary, weight = max(votes.items(), key=lambda item: item[1])
    total = sum(votes.values())
    return {"primary": primary, "confidence": round(weight / total, 2),
            "contributing": sorted(set(contributing) - {primary})[:5],
            "action": ACTIONS.get(primary, "")}


def summarize_trajectory(case: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """汇总阶段分、分支覆盖与归因；供评测记录与线上遥测共用。"""
    trace = result.get("trace") if isinstance(result, dict) else None
    checks: List[Dict[str, Any]] = []
    checks += check_intake(case, result, _span_of(trace, "intake"))
    checks += check_planning(case, result, _span_of(trace, "planning"))
    checks += check_retrieval(_span_of(trace, "retrieval"))
    checks += check_coverage(_span_of(trace, "coverage"))
    checks += check_answer(case, result, _span_of(trace, "answer"))

    stages: Dict[str, Any] = {}
    for stage in STAGES:
        items = [check for check in checks if check["stage"] == stage]
        applicable = [check for check in items if check["passed"] is not None]
        stages[stage] = {
            "score": round(sum(1 for check in applicable if check["passed"]) / len(applicable), 3)
            if applicable else None,
            "checks": items,
        }
    attribution = attribute(checks, case)
    return {
        "stages": stages,
        "branches": _branch_coverage(case, result),
        "errors": _collect_errors(checks),
        "attribution": attribution,
        "trace_version": (trace or {}).get("name") and 1 or 0,
    }


def evaluator_health(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """统计裁判自身的失败与分歧，用于判断结论是否可信。"""
    judged = [record for record in records if record.get("judge")]
    errors = [str(record["judge"].get("error") or "") for record in judged]
    truncated = sum(1 for error in errors if "截断" in error)
    parse_failed = sum(1 for error in errors if "解析" in error)
    agreements = [record["judge"].get("agreement") for record in judged
                  if record["judge"].get("agreement") is not None]
    mean_agreement = round(sum(agreements) / len(agreements), 3) if agreements else None
    total = len(judged)
    truncated_rate = (truncated / total) if total else 0.0
    reliable = truncated_rate <= 0.1 and (mean_agreement is None or mean_agreement <= 0.15)
    return {"judged": total, "truncated": truncated, "parse_failed": parse_failed,
            "mean_agreement": mean_agreement, "reliable": reliable}
