"""产品工作流：需求 → 计划 → 并行查询 → 核验 → 回答 → 收尾。"""

import json
import time
from typing import Any

from inference.tool_dag import ToolDAGScheduler
from product.coverage import assess_days, build_repair_plan
from product.model_gateway import ModelGateway, parse_json
from product.pace import detect_pace
from product.planner import (
    PLANNER_SYSTEM_PROMPT,
    build_planner_prompt,
    days_to_tool_plan,
    queries_for_day,
)
from product.postprocess import (
    _as_list,
    _blocking_conflict,
    _clean_activities,
    detect_day_span,
    _declares_duration,
    _finalize_days,
    budget_conflict,
    cities_fit_days,
    clean_named_places,
    match_activity,
)
from product.prompts import INTAKE_SYSTEM_PROMPT, build_answer_system
from product.provenance import normalize_all
from product.revision import (
    compact_days,
    days_to_revision_plan,
    describe_targets,
    merge_days,
    out_of_range_days,
    parse_target_days,
    split_by_targets,
)
from product.store import Store
from product.tool_adapters import ResearchTools
from product.tool_runtime import ResearchExecutor
from product.trace import (
    _critical_path_ms,
    _fabricated_costs,
    _span,
    _time_order_ok,
    _tool_spans,
)


class AgentWorkflow:
    def __init__(self, store: Store, model: ModelGateway | None = None,
                 tools: ResearchTools | None = None) -> None:
        self.store = store
        self.model = model or ModelGateway()
        self.tools = tools or ResearchTools()

    def _event(self, job: dict[str, Any], stage: str, message: str,
               extra: dict[str, Any] | None = None) -> None:
        self.store.event(job["id"], "progress", {"stage": stage, "message": message, **(extra or {})})

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        query = job["query"]
        calls: list[dict[str, str]] = []
        stages: list[dict[str, Any]] = []
        prior = job.get("previous") or {}
        revision_text = str(job.get("revision") or "").strip()
        prior_days = prior.get("days") if isinstance(prior.get("days"), list) else []
        targets = (
            parse_target_days(revision_text, len(prior_days))
            if revision_text and prior_days and prior.get("constraints") else []
        )

        def finish(trace_status: str, **result: Any) -> dict[str, Any]:
            trace = _span("run", "run", trace_status, (time.monotonic() - started) * 1000,
                          {"status": result.get("status"), "stages": len(stages)}, children=stages)
            return {**result, "trace": trace,
                    "elapsed_seconds": round(time.monotonic() - started, 2)}

        if revision_text and prior_days and prior.get("constraints") and not targets:
            too_far = out_of_range_days(revision_text, len(prior_days))
            if too_far:
                self._event(job, "intake", "修改点名的日期超出当前行程天数", {"days": too_far})
                stages.append(_span("intake", "stage", "failed", 0.0,
                                    {"revision": True, "out_of_range": too_far,
                                     "days": len(prior_days), "decision": "clarify"},
                                    error_type="out_of_range_day"))
                return finish("ok", status="needs_clarification",
                              question="现在的行程是 %d 天，%s 超出了范围。想加一天，还是把这天安排到现有行程里？"
                                       % (len(prior_days), describe_targets(too_far)),
                              constraints=prior.get("constraints"), model_calls=calls, days=[], evidence=[])
        day_span = (prior.get("constraints") or {}).get("day_span") or detect_day_span(
            query, revision_text)
        activity_limit = 2 if day_span == "half" else 4
        if targets:
            constraints = dict(prior["constraints"])
            days_count = len(prior_days)
            destinations = _as_list(constraints.get("destinations"), 4)
            self._event(job, "intake", "沿用原需求，只重排%s" % describe_targets(targets))
            stages.append(_span("intake", "stage", "ok", 0.0,
                                {"revision": True, "target_days": targets, "days": days_count,
                                 "decision": "proceed"}))
        else:
            self._event(job, "intake", "正在理解日期、目的地和预算")
            intake_query = query
            if prior.get("status") == "needs_clarification" and revision_text:
                intake_query = "%s\n（上一轮追问：%s。用户回答：%s）" % (
                    query, prior.get("question") or "", revision_text)
                self._event(job, "intake", "结合上一轮追问继续理解需求")
            intake_started = time.monotonic()
            intake = await self.model.complete("需求 Agent", INTAKE_SYSTEM_PROMPT, intake_query, max_tokens=500)
            calls.append({k: intake[k] for k in ("role", "provider", "model")})
            constraints = parse_json(intake["content"])
            constraints["named_places"] = clean_named_places(
                constraints.get("named_places"), intake_query)
            day_span = detect_day_span(intake_query, revision_text)
            constraints["day_span"] = day_span
            activity_limit = 2 if day_span == "half" else 4
            destinations = _as_list(constraints.get("destinations"), 4)
            declared = _declares_duration(intake_query)
            intake_attributes = {"days": constraints.get("days"), "cities": destinations,
                                 "days_declared": declared}
            if not destinations:
                stages.append(_span("intake", "stage", "failed",
                                    (time.monotonic() - intake_started) * 1000,
                                    {**intake_attributes, "decision": "clarify"}, error_type="wrong_city"))
                return finish("ok", status="needs_clarification", question="你想去哪个城市或地区？",
                              constraints=constraints, model_calls=calls, days=[], evidence=[])
            try:
                days_count = min(7, max(1, int(constraints.get("days") or 1)))
            except (TypeError, ValueError):
                days_count = 1
            constraints["days"] = days_count
            constraints["pace"] = detect_pace(intake_query, revision_text)
            conflicts = _as_list(constraints.get("conflicts"), 3)
            if not declared:
                stages.append(_span("intake", "stage", "failed",
                                    (time.monotonic() - intake_started) * 1000,
                                    {**intake_attributes, "days": days_count, "decision": "clarify"},
                                    error_type="missing_days"))
                return finish("ok", status="needs_clarification", question="打算玩几天？",
                              constraints=constraints, model_calls=calls, days=[], evidence=[])
            budget_clash = budget_conflict(intake_query, days_count)
            blocking = [item for item in conflicts if _blocking_conflict(item)]
            if blocking or budget_clash:
                reason = "；".join(blocking[:2]) or "预算和住宿标准对不上"
                stages.append(_span("intake", "stage", "failed",
                                    (time.monotonic() - intake_started) * 1000,
                                    {**intake_attributes, "days": days_count, "decision": "clarify",
                                     "conflicts": conflicts},
                                    error_type="missed_budget" if budget_clash else "should_clarify"))
                return finish("ok", status="needs_clarification", question="需求里有冲突：%s。想先调整哪一项？" % reason,
                              constraints=constraints, model_calls=calls, days=[], evidence=[])
            if conflicts:
                self._event(job, "intake", "忽略非阻断冲突", {"conflicts": conflicts[:3]})
                constraints["conflicts"] = []
            preferences = _as_list(constraints.get("preferences"), 6)
            if not cities_fit_days(destinations, days_count) and preferences:
                trimmed = [item for item in destinations if item not in preferences]
                if trimmed and len(trimmed) < len(destinations) and cities_fit_days(trimmed, days_count):
                    self._event(job, "intake", "把偏好里的景点从目的地里移除", {"dropped": len(destinations) - len(trimmed)})
                    destinations = trimmed
                    constraints["destinations"] = trimmed
            if not cities_fit_days(destinations, days_count):
                stages.append(_span("intake", "stage", "failed",
                                    (time.monotonic() - intake_started) * 1000,
                                    {**intake_attributes, "days": days_count, "decision": "clarify"},
                                    error_type="should_clarify"))
                return finish("ok", status="needs_clarification",
                              question="%d 天安排 %d 个城市太紧，想保留哪几个？" % (days_count, len(destinations)),
                              constraints=constraints, model_calls=calls, days=[], evidence=[])
            stages.append(_span("intake", "stage", "ok", (time.monotonic() - intake_started) * 1000,
                                {**intake_attributes, "days": days_count, "decision": "proceed"}))

        self._event(job, "planning", "正在拆解每日行程和查询任务")
        planning_started = time.monotonic()
        plan_response = await self.model.complete("规划 Agent", PLANNER_SYSTEM_PROMPT,
            build_planner_prompt(
                query, constraints, prior.get("plan"),
                {"days": targets, "instruction": revision_text} if targets else None),
            max_tokens=1000)
        calls.append({k: plan_response[k] for k in ("role", "provider", "model")})
        plan = parse_json(plan_response["content"])
        if targets and prior.get("plan"):
            previous_plan = prior["plan"]
            if all(queries_for_day(plan, day) == queries_for_day(previous_plan, day) for day in targets):
                self._event(job, "planning", "修改意见没有被规划体现，正在重试")
                retry_response = await self.model.complete("规划 Agent", PLANNER_SYSTEM_PROMPT,
                    build_planner_prompt(query, constraints, previous_plan,
                                         {"days": targets, "instruction": revision_text,
                                          "note": "上一次的查询词与原方案相同，请换用不同的地点关键词以体现修改意见"}),
                    max_tokens=1000)
                calls.append({k: retry_response[k] for k in ("role", "provider", "model")})
                retry_plan = parse_json(retry_response["content"])
                if not all(queries_for_day(retry_plan, day) == queries_for_day(previous_plan, day)
                           for day in targets):
                    plan = retry_plan
        days = plan.get("days")
        if targets:
            tool_plan = days_to_revision_plan(days, targets, days_count, destinations)
            kept_plan_days = [d for d in (prior.get("plan") or {}).get("days", [])
                              if int(d.get("day") or 0) not in targets]
            plan = {**(prior.get("plan") or {}),
                    "days": sorted(kept_plan_days + [d for d in days if int(d.get("day") or 0) in targets],
                                   key=lambda d: int(d.get("day") or 0))}
            days = plan["days"]
        else:
            tool_plan = days_to_tool_plan(days, days_count, destinations)
        per_day_queries = [len(day.get("poi_queries") or []) for day in days if isinstance(day, dict)]
        stages.append(_span("planning", "stage", "ok", (time.monotonic() - planning_started) * 1000,
                            {"days_planned": len(per_day_queries), "per_day_queries": per_day_queries,
                             "nodes": len(tool_plan.nodes)}))

        self._event(job, "research", "正在并行查询地点来源", {"tool_count": len(tool_plan.nodes)})
        prior_evidence = normalize_all(prior.get("evidence", []))
        keep_evidence: list[dict[str, Any]] = []
        if targets:
            keep_evidence, _ = split_by_targets(prior_evidence, targets)
        executor = ResearchExecutor(self.tools, keep_evidence if targets else prior_evidence)
        scheduler = ToolDAGScheduler(executor=executor, max_concurrency=4, fail_fast=False)
        retrieval_started = time.monotonic()
        dag_result = await scheduler.run(tool_plan)
        evidence = normalize_all(executor.collect(dag_result, tool_plan))
        if targets:
            evidence = normalize_all(keep_evidence + evidence)
        tool_spans = _tool_spans(dag_result, tool_plan)
        critical_ms = _critical_path_ms(tool_plan, dag_result)
        node_ms = sum(float(item.get("latency_ms") or 0.0) for item in dag_result.get("results", []))
        cache_hits = sum(1 for item in dag_result.get("results", []) if item.get("cache_hit"))
        verification = assess_days(days, evidence)
        repair_plan = build_repair_plan(days, evidence)
        if repair_plan is not None:
            self._event(job, "verification", "正在补查没有可追溯来源的日期",
                        {"uncovered_days": verification["uncovered_days"]})
            repair_result = await scheduler.run(repair_plan)
            evidence = normalize_all(evidence + executor.collect(repair_result, repair_plan))
            tool_spans += _tool_spans(repair_result, repair_plan)
            critical_ms = max(critical_ms, _critical_path_ms(repair_plan, repair_result))
            node_ms += sum(float(item.get("latency_ms") or 0.0) for item in repair_result.get("results", []))
            cache_hits += sum(1 for item in repair_result.get("results", []) if item.get("cache_hit"))
            verification = assess_days(days, evidence)
        retrieval_ms = (time.monotonic() - retrieval_started) * 1000
        counts = {"ok": 0, "empty": 0, "failed": 0, "unavailable": 0}
        for item in evidence:
            status = str(item.get("status"))
            if status in counts:
                counts[status] += 1
        retrieval_status = "degraded" if (counts["failed"] or counts["unavailable"]) else (
            "warn" if (counts["empty"] or verification["uncovered_days"]) else "ok")
        stages.append(_span("retrieval", "stage", retrieval_status, retrieval_ms,
                            {"calls": len(evidence), **counts, "cache_hits": cache_hits,
                             "critical_path_ms": int(critical_ms),
                             "parallel_efficiency": round(min(1.0, node_ms / (retrieval_ms * 4)), 3)
                             if retrieval_ms else 0.0},
                            children=tool_spans,
                            error_type="tool_error" if counts["failed"] else (
                                "auth_missing" if counts["unavailable"] else None)))
        stages.append(_span("coverage", "stage", "warn" if verification["uncovered_days"] else "ok", 0.0,
                            {"per_day_ok": [item["covered"] for item in verification["days"]],
                             "uncovered_days": verification["uncovered_days"], "risk": verification["risk"]}))

        good = [e for e in evidence if e.get("status") == "ok"]
        self._event(job, "verification", "正在检查计划是否有可追溯的地点来源",
                    {"supported_queries": len(good), "total_queries": len(evidence),
                     "risk": verification["risk"], "uncovered_days": verification["uncovered_days"]})
        if not good:
            stages.append(_span("answer", "stage", "skipped", 0.0,
                                {"reason": "no_usable_evidence"}, error_type="tool_error"))
            return finish("failed", status="partial",
                          question="地点查询暂不可用，无法给出可信的具体行程。",
                          constraints=constraints, plan=plan, days=[],
                          evidence=evidence, model_calls=calls, verification=verification)

        self._event(job, "answer", "正在根据已核实地点组织行程")
        answer_started = time.monotonic()
        answer = {}
        answer_failed = False
        pace = constraints.get("pace") or detect_pace(query, revision_text)
        answer_system = build_answer_system(pace, targets)
        try:
            answer_payload = {"query": query, "constraints": constraints, "plan": plan, "evidence": good}
            if targets:
                answer_payload["revision"] = revision_text
                answer_payload["target_days"] = targets
                answer_payload["keep_days"] = compact_days(
                    [day for day in prior_days if int(day.get("day") or 0) not in targets]
                )
            answer_response = await self.model.complete("回答 Agent", answer_system,
                json.dumps(answer_payload, ensure_ascii=False), max_tokens=3000)
            calls.append({k: answer_response[k] for k in ("role", "provider", "model")})
            answer = parse_json(answer_response["content"])
        except (ValueError, RuntimeError):
            answer_failed = True
            self._event(job, "answer", "回答格式不符合协议，正在使用来源数据构造保守方案")
        valid_names = {e["id"]: {item["name"] for item in e.get("items", [])} for e in good}
        cleaned_days = []
        seen_titles = set()
        raw_count = 0
        grounded_count = 0
        for day in answer.get("days", []) if isinstance(answer.get("days"), list) else []:
            raw_activities = day.get("activities", []) if isinstance(day, dict) else []
            raw_count += sum(1 for activity in raw_activities if isinstance(activity, dict))
            matched_activities = []
            for activity in raw_activities:
                if not isinstance(activity, dict):
                    continue
                matched = match_activity(activity, valid_names)
                if not matched or matched[1] in seen_titles:
                    continue
                seen_titles.add(matched[1])
                grounded_count += 1
                matched_activities.append({**activity, "evidence_id": matched[0], "title": matched[1]})
            cleaned_days.append({"day": day.get("day"), "city": day.get("city"),
                                 "activities": _clean_activities(
                                     matched_activities, limit=activity_limit,
                                     evidence={e["id"]: e for e in evidence})})
        if not any(d["activities"] for d in cleaned_days):
            answer_failed = True
            cleaned_days = []
            for day in days:
                day_evidence = [e for e in good if e["id"].startswith("d%d-" % day["day"]) and e.get("items")]
                activities = _clean_activities([{
                    "title": e["items"][0]["name"], "evidence_id": e["id"],
                    "description": "可作为当天的候选地点；请通过来源页面核对地址、开放时间和交通。",
                    "transport": "交通未核实", "cost": "未核实"} for e in day_evidence])
                cleaned_days.append({"day": day["day"], "city": day["city"], "activities": activities})
        order_ok = _time_order_ok(cleaned_days)
        if targets:
            cleaned_days = merge_days(prior_days, cleaned_days, targets)
            order_ok = _time_order_ok(cleaned_days)
        if targets:
            target_set = set(targets)
            kept_days = [day for day in prior_days if int(day.get("day") or 0) not in target_set]
            cleaned_days = _finalize_days(
                [day for day in cleaned_days if int(day.get("day") or 0) in target_set],
                good,
                [day for day in days if int(day.get("day") or 0) in target_set],
                keep_days=kept_days, max_activities=activity_limit)
        else:
            cleaned_days = _finalize_days(cleaned_days, good, days, max_activities=activity_limit)
        final_count = sum(len(day.get("activities", [])) for day in cleaned_days)
        fabricated = _fabricated_costs(cleaned_days, evidence)
        dropped = max(0, raw_count - grounded_count)
        filled = max(0, final_count - grounded_count)
        stages.append(_span("answer", "stage", "degraded" if (answer_failed or dropped or filled) else "ok",
                            (time.monotonic() - answer_started) * 1000,
                            {"raw": raw_count, "grounded": grounded_count, "dropped": dropped,
                             "filled": filled, "final": final_count,
                             "fabricated_cost": fabricated, "time_order_ok": order_ok},
                            error_type="density_miss" if dropped else None))
        revision_applied: bool | None = None
        if targets:
            unchanged = 0
            for day_number in targets:
                old_day = next((d for d in prior_days if int(d.get("day") or 0) == day_number), {})
                new_day = next((d for d in cleaned_days if int(d.get("day") or 0) == day_number), {})
                old_titles = [a.get("title") for a in old_day.get("activities", [])]
                new_titles = [a.get("title") for a in new_day.get("activities", [])]
                if old_titles and old_titles == new_titles:
                    unchanged += 1
            revision_applied = unchanged < len(targets)
        notes = _as_list(answer.get("notes"), 8)
        if revision_applied is False:
            notes.insert(0, "本次修改没有找到新的可追溯地点，暂时保留了原安排；可以换个说法再试。")
        if answer_failed:
            notes.insert(0, "回答阶段使用保守方案，安排基于可追溯来源，具体细节需出行前核对。")
        for item in verification["days"]:
            if item["uncovered"]:
                notes.append("第%d天没有获取到可追溯来源，当天安排仅作参考。" % item["day"])
        notes = notes[:8]
        root_status = "degraded" if any(span["status"] in ("degraded", "warn", "failed") for span in stages) else "ok"
        return finish(root_status,
                      status="complete" if any(d["activities"] for d in cleaned_days) else "partial",
                      summary=str(answer.get("summary") or "已根据可追溯地点来源整理行程。"),
                      days=cleaned_days, notes=notes, constraints=constraints,
                      plan=plan, evidence=evidence, model_calls=calls, verification=verification,
                      revision_applied=revision_applied)
