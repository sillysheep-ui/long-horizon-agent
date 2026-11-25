"""产品工作流：需求 → 计划 → 并行查询 → 核验 → 回答 → 收尾。"""

import asyncio
import json
import os
import re
import time
from typing import Any

from inference.tool_dag import ToolDAGScheduler
from product.anchor import detect_anchor
from product.coverage import assess_days, build_repair_plan
from product.critic import review_async as review_itinerary
from product.crosscheck import cross_checks
from product.destinations import unknown_destinations
from product.global_plan import build_skeleton
from product.memory import build_history
from product.model_gateway import ModelGateway, call_info, parse_json
from product.pace import count_bounds, detect_pace, requested_count
from product.plan_rules import validate_plan
from product.plancheck import check_day_plan
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
    _declares_duration,
    _finalize_days,
    budget_conflict,
    cities_fit_days,
    clean_lodging,
    clean_meals,
    clean_named_places,
    clean_people,
    clean_room_type,
    clean_rooms,
    clean_transport,
    day_hours,
    detect_day_windows,
    latin_hints,
    match_activity,
    update_constraints,
)
from product.profile import profile_block
from product.prompts import ANSWER_REPAIR_SUFFIX, INTAKE_SYSTEM_PROMPT, build_answer_system
from product.provenance import normalize_all
from product.repair import pick_meals, repair_meal_times
from product.revcheck import check_requirements
from product.revision import (
    apply_move,
    compact_days,
    days_to_revision_plan,
    describe_targets,
    merge_days,
    out_of_range_days,
    parse_move,
    parse_target_days,
    split_by_targets,
)
from product.revparse import ATTRIBUTE_LABELS, parse_revision
from product.schemas import parse_answer, parse_intake, parse_plan
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
from product.transit import add_transfers


def _rule_violations(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"day": issue["day"], "kind": issue["code"], "detail": issue["detail"]}
            for issue in issues]


def _schema_stage(stages: list[dict[str, Any]], name: str, issues: list[str]) -> None:
    """结构问题只记录不阻断：数据已经归一成安全形状，语义校验在后面照常跑。"""
    if issues:
        stages.append(_span("schema:%s" % name, "stage", "warn", 0.0, {"issues": issues[:6]}))


class AgentWorkflow:
    def __init__(self, store: Store, model: ModelGateway | None = None,
                 tools: ResearchTools | None = None) -> None:
        self.store = store
        self.model = model or ModelGateway()
        self.tools = tools or ResearchTools()

    def _event(self, job: dict[str, Any], stage: str, message: str,
               extra: dict[str, Any] | None = None) -> None:
        self.store.event(job["id"], "progress", {"stage": stage, "message": message, **(extra or {})})

    async def _conversation_context(self, job: dict[str, Any], current_text: str,
                                    calls: list[dict[str, Any]]) -> tuple[list[dict[str, str]], str | None]:
        """取对话上文：预算内给全部原文，超出后给最近几条 + 早期摘要。"""
        conversation_id = self.store.conversation_of_job(job["id"])
        if not conversation_id:
            return [], None
        context = await build_history(self.model, self.store, conversation_id,
                                     exclude_job=job["id"], calls=calls)
        current = current_text.strip()
        messages = [message for message in context["messages"] if message["content"] != current]
        return messages[-8:], context.get("summary")

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

        move = (parse_move(revision_text, len(prior_days))
                if revision_text and prior_days and prior.get("constraints") else None)
        if move:
            applied = apply_move(prior_days, move["from"], move["to"],
                                 (prior.get("constraints") or {}).get("day_windows"))
            if applied:
                moved_days = add_transfers(applied["days"])
                titles = "、".join(str(title) for title in applied["titles"][:3] if title)
                note = "已把第%d天的 %d 个安排挪到第%d天%s" % (
                    move["from"], applied["kept"], move["to"],
                    ("：" + titles) if titles else "")
                if applied["dropped"]:
                    note += "；另有 %d 个因当天时间不足未排入" % applied["dropped"]
                note += "；其余天保持不变。第%d天暂无安排。" % move["from"]
                target_day = next((day for day in moved_days
                                   if int(day.get("day") or 0) == move["to"]), {})
                if len(target_day.get("activities") or []) >= 5:
                    note += "第%d天现有 %d 个活动，建议再精简。" % (
                        move["to"], len(target_day["activities"]))
                self._event(job, "intake", note)
                stages.append(_span("intake", "stage", "ok", 0.0,
                                    {"revision": True, "move": move, "decision": "move"}))
                prior_notes = _as_list(prior.get("notes"), 8)
                return finish("ok", status="complete", summary=str(prior.get("summary") or ""),
                              days=moved_days, notes=[note, *prior_notes][:8],
                              constraints=prior.get("constraints"), plan=prior.get("plan"),
                              evidence=prior.get("evidence"), model_calls=calls,
                              revision_applied=True)
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
        prior_windows = (prior.get("constraints") or {}).get("day_windows")
        windows = prior_windows or detect_day_windows(query, revision_text, days=max(1, len(prior_days)))
        if targets:
            constraints = dict(prior["constraints"])
            constraints["day_windows"] = windows
            revised_anchor = detect_anchor(revision_text)
            if revised_anchor["reason"] != "default":
                constraints["anchor"] = revised_anchor["anchor"]
            days_count = len(prior_days)
            requirement_days = list(targets)
            changes = update_constraints(constraints, revision_text)
            parsed_revision = await parse_revision(self.model, revision_text, len(prior_days),
                                                   constraints, calls)
            if parsed_revision["attributes"]:
                labels = [ATTRIBUTE_LABELS[name] for name in parsed_revision["attributes"]]
                constraints["revision_requirements"] = labels
            for key, value in parsed_revision["changes"].items():
                if key == "destinations" and key not in changes:
                    value = list(dict.fromkeys([*_as_list(constraints.get("destinations"), 4), *value]))
                changes.setdefault(key, value)
            if changes.get("destinations") and targets:
                replace_city = re.search(
                    r"(换成|改成|改为|目的地(?:改成|换成)|城市(?:改成|换成))", revision_text)
                if not replace_city:
                    merged = list(dict.fromkeys([*_as_list(constraints.get("destinations"), 4),
                                                 *changes["destinations"]]))
                    changes["destinations"] = merged
            if changes:
                constraints.update(changes)
                if "days" in changes:
                    days_count = int(changes["days"])
                    constraints["days"] = days_count
                    windows = detect_day_windows(query, revision_text, days=days_count)
                    constraints["day_windows"] = windows
                targets = list(range(1, days_count + 1))
                constraints["revision_requirement_days"] = requirement_days
                self._event(job, "intake", "检测到整体条件变化，改为整单重排", {"changes": changes})
            destinations = _as_list(constraints.get("destinations"), 4)
            if budget_conflict(query, days_count) or budget_conflict(revision_text, days_count):
                stages.append(_span("intake", "stage", "failed", 0.0,
                                    {"revision": True, "target_days": targets, "days": days_count,
                                     "decision": "clarify"}, error_type="missed_budget"))
                return finish("ok", status="needs_clarification",
                              question="预算和住宿标准对不上，想先调整哪一项？",
                              constraints=constraints, model_calls=calls, days=[], evidence=[])
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
            intake_system = INTAKE_SYSTEM_PROMPT
            remembered = profile_block(self.store)
            if remembered:
                intake_system = INTAKE_SYSTEM_PROMPT + "\n\n" + remembered
                self._event(job, "intake", "参考了已记住的长期偏好")
            intake = await self.model.complete("需求 Agent", intake_system, intake_query, max_tokens=500)
            calls.append(call_info(intake))
            constraints, schema_issues = parse_intake(parse_json(intake["content"]))
            _schema_stage(stages, "intake", schema_issues)
            hints = latin_hints(intake_query)
            if not _as_list(constraints.get("destinations"), 4) and hints.get("destinations"):
                constraints["destinations"] = hints["destinations"]
            if not constraints.get("days") and hints.get("days"):
                constraints["days"] = hints["days"]
            if not constraints.get("budget") and hints.get("budget"):
                constraints["budget"] = hints["budget"]
            constraints["named_places"] = clean_named_places(
                constraints.get("named_places"), intake_query)
            destinations = _as_list(constraints.get("destinations"), 4)
            declared = _declares_duration(intake_query) or bool(hints.get("days"))
            unknown = unknown_destinations(destinations) if destinations else []
            if destinations and len(unknown) == len(destinations):
                stages.append(_span("intake", "stage", "failed",
                                    (time.monotonic() - intake_started) * 1000,
                                    {"cities": destinations, "unknown": unknown,
                                     "decision": "clarify"}, error_type="wrong_city"))
                return finish("ok", status="needs_clarification",
                              question="没查到「%s」这个地方，是想去别的城市吗？告诉我城市名，或者它在哪个省。" % unknown[0],
                              constraints=constraints, model_calls=calls, days=[], evidence=[])
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
            windows = detect_day_windows(intake_query, revision_text, days=days_count)
            constraints["day_windows"] = windows
            constraints["requested_count"] = requested_count(intake_query, revision_text)
            constraints["pace"] = detect_pace(intake_query, revision_text)
            anchor = detect_anchor(intake_query, revision_text)
            constraints["anchor"] = anchor["anchor"]
            constraints["people"] = clean_people(constraints.get("people"))
            constraints["rooms"] = clean_rooms(constraints.get("rooms"))
            constraints["room_type"] = clean_room_type(constraints.get("room_type"))
            constraints["transport"] = clean_transport(constraints.get("transport"))
            if constraints["people"]:
                self._event(job, "intake", "同行 %d 人%s%s" % (
                    constraints["people"],
                    "，%d 间%s" % (constraints["rooms"], constraints["room_type"] or "房")
                    if constraints["rooms"] else "",
                    "，城际交通：%s" % constraints["transport"] if constraints["transport"] else ""))
            if anchor["anchor"] != "sight":
                self._event(job, "intake", "行程锚定：%s" % (
                    "饮食优先" if anchor["anchor"] == "food" else "住宿优先"))
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
        calls.append(call_info(plan_response))
        plan, schema_issues = parse_plan(parse_json(plan_response["content"]))
        _schema_stage(stages, "planning", schema_issues)
        if targets and prior.get("plan"):
            previous_plan = prior["plan"]
            if all(queries_for_day(plan, day) == queries_for_day(previous_plan, day) for day in targets):
                self._event(job, "planning", "修改意见没有被规划体现，正在重试")
                retry_response = await self.model.complete("规划 Agent", PLANNER_SYSTEM_PROMPT,
                    build_planner_prompt(query, constraints, previous_plan,
                                         {"days": targets, "instruction": revision_text,
                                          "note": "上一次的查询词与原方案相同，请换用不同的地点关键词以体现修改意见"}),
                    max_tokens=1000)
                calls.append(call_info(retry_response))
                retry_plan, retry_issues = parse_plan(parse_json(retry_response["content"]))
                _schema_stage(stages, "planning_retry", retry_issues)
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

        plan_mode = os.getenv("PLAN_MODE", "model").strip().lower()
        skeleton_info = None
        if plan_mode == "global":
            skeleton_started = time.monotonic()
            try:
                skeleton_info = build_skeleton(constraints, days, evidence)
            except Exception:  # noqa: BLE001 - 骨架是增强路径，失败要走回退而不是打断整单
                skeleton_info = {"days": [], "candidates": {}, "ok": False,
                                 "reason": "骨架构建异常", "route_km": 0.0}
            stages.append(_span("global_plan", "stage", "ok" if skeleton_info["ok"] else "warn",
                                (time.monotonic() - skeleton_started) * 1000,
                                {"mode": "global", "days": len(skeleton_info["days"]),
                                 "candidates": skeleton_info["candidates"],
                                 "route_km": skeleton_info["route_km"],
                                 "reason": skeleton_info["reason"]}))
            if not skeleton_info["ok"]:
                self._event(job, "planning", "候选点不足，按逐天方案继续", {"reason": skeleton_info["reason"]})

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
                          evidence=evidence, model_calls=calls, verification=verification,
                          skeleton=skeleton_info)

        self._event(job, "answer", "正在根据已核实地点组织行程")
        answer_started = time.monotonic()
        answer = {}
        answer_payload: dict[str, Any] = {}
        answer_failed = False
        pace = constraints.get("pace") or detect_pace(query, revision_text)
        answer_system = build_answer_system(
            pace, targets, (constraints.get("revision_requirements") or None),
            (constraints.get("revision_requirement_days") or None))
        try:
            answer_payload = {"query": query, "constraints": constraints, "plan": plan, "evidence": good}
            if targets:
                answer_payload["revision"] = revision_text
                answer_payload["target_days"] = targets
                answer_payload["keep_days"] = compact_days(
                    [day for day in prior_days if int(day.get("day") or 0) not in targets]
                )
                answer_payload["target_days_current"] = compact_days(
                    [day for day in prior_days if int(day.get("day") or 0) in targets]
                )
                history, summary = await self._conversation_context(job, revision_text, calls)
                answer_payload["history"] = history
                if summary:
                    answer_payload["conversation_summary"] = summary
            answer_response = await self.model.complete("回答 Agent", answer_system,
                json.dumps(answer_payload, ensure_ascii=False), max_tokens=3000)
            calls.append(call_info(answer_response))
            answer, schema_issues = parse_answer(parse_json(answer_response["content"]))
            _schema_stage(stages, "answer", schema_issues)
        except (ValueError, RuntimeError):
            answer_failed = True
            self._event(job, "answer", "回答格式不符合协议，正在使用来源数据构造保守方案")
        rule_issues = validate_plan(answer.get("days"), good, constraints) if not answer_failed else []
        critique = {"score": None, "verdict": "skipped", "issues": [], "source": "skipped"}
        if not answer_failed:
            critique = await review_itinerary(self.model, answer.get("days"), constraints, calls)
        hard = check_day_plan(answer.get("days"), constraints)["violations"] + _rule_violations(rule_issues)
        violations = hard + [{"day": item["day"], "kind": item["kind"], "detail": item["detail"]}
                             for item in critique["issues"]]
        plan_repair = {"attempted": False, "before": len(violations), "after": len(violations)}
        semantic = [issue for issue in critique["issues"]]
        critique_score = critique.get("score")
        should_repair = bool(hard) or (
            bool(semantic) and (critique_score is None or critique_score < 6.5))
        if should_repair and answer_payload:
            self._event(job, "answer", "模型排期有 %d 处硬伤，让它自己重排一次" % len(violations))
            try:
                previous = compact_days([day for day in answer.get("days") or []
                                         if isinstance(day, dict)])
                fix_payload = {**answer_payload, "previous_days": previous, "issues": violations}
                fix_system = answer_system + ANSWER_REPAIR_SUFFIX % json.dumps(
                    violations, ensure_ascii=False)
                fix_response = await asyncio.wait_for(
                    self.model.complete("回答 Agent", fix_system,
                                        json.dumps(fix_payload, ensure_ascii=False), max_tokens=3000),
                    timeout=90)
                calls.append(call_info(fix_response))
                candidate, candidate_issues = parse_answer(parse_json(fix_response["content"]))
                _schema_stage(stages, "answer_repair", candidate_issues)
                rules_after = validate_plan(candidate.get("days"), good, constraints)
                after = check_day_plan(candidate.get("days"), constraints)["violations"] \
                    + _rule_violations(rules_after)
                plan_repair.update({"attempted": True, "after": len(after)})
                if not candidate_issues and len(after) <= len(violations):
                    answer = candidate
                    violations = after
                    rule_issues = rules_after
                    plan_repair["applied"] = True
            except (ValueError, RuntimeError, TimeoutError):
                plan_repair.update({"attempted": True, "error": True})
        plan_ok = not check_day_plan(answer.get("days"), constraints)["violations"]
        valid_names = {e["id"]: {item["name"] for item in e.get("items", [])} for e in good}
        valid_coords = {(e["id"], item["name"]): item.get("coordinates")
                        for e in good for item in e.get("items", [])
                        if item.get("name") and item.get("coordinates")}
        cleaned_days = []
        evidence_by_id = {e["id"]: e for e in evidence}
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
                matched_activities.append({**activity, "evidence_id": matched[0], "title": matched[1],
                                           "coordinates": valid_coords.get((matched[0], matched[1]))})
            hours_for_day = day_hours(windows, day.get("day"))
            entry = {"day": day.get("day"), "city": day.get("city"),
                     "activities": _clean_activities(
                         matched_activities,
                         limit=count_bounds(hours_for_day, pace, constraints.get("requested_count"))[1],
                         evidence=evidence_by_id)}
            meals = clean_meals(day.get("meals"), evidence_by_id)
            if meals:
                entry["meals"] = meals
            lodging = clean_lodging(day.get("lodging_area"))
            if lodging:
                entry["lodging_area"] = lodging
            cleaned_days.append(entry)
        if not any(d["activities"] for d in cleaned_days):
            answer_failed = True
            cleaned_days = []
            for day in days:
                day_evidence = [e for e in good if e["id"].startswith("d%d-" % day["day"]) and e.get("items")]
                activities = _clean_activities([{
                    "title": e["items"][0]["name"], "evidence_id": e["id"],
                    "coordinates": e["items"][0].get("coordinates"),
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
                keep_days=kept_days, windows=windows, pace=pace,
                requested=constraints.get("requested_count"), keep_order=plan_ok,
                preserve_times=plan_mode == "model", backfill=plan_mode != "model")
        else:
            cleaned_days = _finalize_days(cleaned_days, good, days, windows=windows, pace=pace,
                                          requested=constraints.get("requested_count"),
                                          keep_order=plan_ok,
                                          preserve_times=plan_mode == "model",
                                          backfill=plan_mode != "model")
        if plan_mode != "model":
            if constraints.get("anchor") in ("food", "stay"):
                for day in cleaned_days:
                    picked = pick_meals(day, evidence)
                    if picked:
                        day["meals"] = picked
            for day in cleaned_days:
                repair_meal_times(day)
        cleaned_days = add_transfers(cleaned_days)
        final_count = sum(len(day.get("activities", [])) for day in cleaned_days)
        fabricated = _fabricated_costs(cleaned_days, evidence)
        dropped = max(0, raw_count - grounded_count)
        filled = max(0, final_count - grounded_count)
        stages.append(_span("answer", "stage", "degraded" if (answer_failed or dropped or filled) else "ok",
                            (time.monotonic() - answer_started) * 1000,
                            {"raw": raw_count, "grounded": grounded_count, "dropped": dropped,
                             "filled": filled, "final": final_count,
                             "fabricated_cost": fabricated, "time_order_ok": order_ok,
                             "plan_ok": plan_ok, "plan_repair": plan_repair, "critique": critique},
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
        cross = cross_checks(cleaned_days, constraints)
        cross_notes = {
            "meal_near": "有一餐离当天活动超过 1.5 公里，建议就近调整。",
            "meal_time": "用餐时间和当天活动有重叠，建议错开。",
            "budget_fits": "按行程里写明的票价与人均合计，已接近或超过预算，建议核对。",
        }
        notes = _as_list(answer.get("notes"), 8)
        for name, ok in cross.items():
            if not ok and name in cross_notes:
                notes.append(cross_notes[name])
        if revision_applied is False:
            notes.insert(0, "本次修改没有找到新的可追溯地点，暂时保留了原安排；可以换个说法再试。")
        if answer_failed:
            notes.insert(0, "回答阶段使用保守方案，安排基于可追溯来源，具体细节需出行前核对。")
        for item in verification["days"]:
            if item["uncovered"]:
                notes.append("第%d天没有获取到可追溯来源，当天安排仅作参考。" % item["day"])
        revision_check = None
        requirements = _as_list(constraints.get("revision_requirements"), 4)
        if targets and requirements:
            target_day_numbers = {int(day) for day in
                                  (constraints.get("revision_requirement_days") or targets)}
            target_days_data = [d for d in cleaned_days
                                if int(d.get("day") or 0) in target_day_numbers]
            revision_check = check_requirements(requirements, target_days_data)
            if revision_check["unmet"]:
                notes.insert(0, "这次修改没有完全满足「%s」，可以再具体说一句想换成什么。"
                                % "、".join(revision_check["unmet"]))
        if critique["issues"] and not plan_repair.get("applied"):
            notes.insert(0, "评审提醒：%s" % critique["issues"][0]["detail"])
        notes = notes[:8]
        root_status = "degraded" if any(span["status"] in ("degraded", "warn", "failed") for span in stages) else "ok"
        return finish(root_status,
                      status="complete" if any(d["activities"] for d in cleaned_days) else "partial",
                      summary=str(answer.get("summary") or "已根据可追溯地点来源整理行程。"),
                      days=cleaned_days, notes=notes, constraints=constraints,
                      plan=plan, evidence=evidence, model_calls=calls, verification=verification,
                      cross_checks=cross, revision_applied=revision_applied,
                      revision_check=revision_check, skeleton=skeleton_info,
                      plan_rules={"mode": plan_mode, "issues": len(rule_issues),
                                  "codes": sorted({issue["code"] for issue in rule_issues})})
