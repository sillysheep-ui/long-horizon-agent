"""明确职责和共享状态的旅行规划工作流。"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from inference.tool_dag import ToolDAGScheduler
from product.coverage import assess_days, build_repair_plan
from product.model_gateway import ModelGateway, parse_json
from product.names import clean_place_name, dedupe_names, looks_like_place, title_matches
from product.planner import (
    PLANNER_SYSTEM_PROMPT, build_planner_prompt, days_to_tool_plan, queries_for_day,
)
from product.provenance import build_evidence, normalize_all
from product.revision import (
    compact_days, day_of_evidence, days_to_revision_plan, describe_targets,
    merge_days, out_of_range_days, parse_target_days, split_by_targets,
)
from product.store import Store
from product.tool_runtime import ResearchExecutor


def _as_list(value: Any, limit: int) -> List[str]:
    return [str(x).strip()[:80] for x in value[:limit] if str(x).strip()] if isinstance(value, list) else []


_TITLE_NOISE = re.compile(r"[\s\W_]+", re.UNICODE)


def _norm_title(text: str) -> str:
    """去掉空白、标点和大小写差异，便于和证据里的地点名比对。"""
    return _TITLE_NOISE.sub("", str(text)).casefold()


def match_activity(activity: Dict[str, Any], valid_names: Dict[str, set]) -> Optional[tuple]:
    """把模型给出的活动标题对回证据里的地点名；对不上返回 None。

    只放宽空白/标点/大小写和包含关系，不会接受证据里没有的地点。
    """
    evidence_id = activity.get("evidence_id")
    if evidence_id not in valid_names:
        return None
    title = _norm_title(activity.get("title") or "")
    if not title:
        return None
    for name in valid_names[evidence_id]:
        if _norm_title(name) == title:
            return evidence_id, name
    for name in valid_names[evidence_id]:
        normalized = _norm_title(name)
        if len(normalized) >= 4 and (normalized in title or title in normalized):
            return evidence_id, name
    return None


def _rank_search_items(hits: Any, keyword: str, city: str) -> List[Dict[str, Any]]:
    """只保留标题或摘要与目标相关的网页，并优先展示官方来源。"""
    candidates = []
    if not isinstance(hits, list):
        return []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        url, title = str(hit.get("url") or ""), str(hit.get("title") or "").strip()
        parsed = urlparse(url)
        if not title or parsed.scheme not in ("https", "http") or not parsed.hostname:
            continue
        snippet = str(hit.get("snippet") or "")[:300]
        short = keyword[:2]
        if not (keyword in title or short in title or city in title or keyword in snippet):
            continue
        name = clean_place_name(title)
        if not name:
            continue
        score = (6 if keyword in title else 0) + (3 if short in title else 0)
        score += (2 if city in title else 0) + (1 if keyword in snippet else 0)
        score += (4 if parsed.hostname.endswith("gov.cn") else 0)
        score += (3 if looks_like_place(name, keyword, city) else -2)
        score += (2 if len(name) <= 12 else 0)
        score -= (3 if len(name) > 24 else 0)
        candidates.append((score, {"name": name[:60], "source_title": title[:140],
                                   "address": "", "url": url, "snippet": snippet,
                                   "coordinates": None}))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return dedupe_names([item for _, item in candidates])[:3]


DEFAULT_STARTS = ("09:30", "13:30", "16:30")
_DURATION_HOURS = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*小时")
_DURATION_MINUTES = re.compile(r"([0-9]+)\s*分钟")


def _duration_minutes(value: Any) -> int:
    """把“2.5 小时”“90 分钟”转成分钟数，识别不了按 90 分钟算。"""
    text = str(value or "")
    match = _DURATION_HOURS.search(text)
    if match:
        return max(15, int(float(match.group(1)) * 60))
    match = _DURATION_MINUTES.search(text)
    if match:
        return max(15, int(match.group(1)))
    return 90


def _minutes_of(start: str) -> int:
    hour, _, minute = str(start or "").partition(":")
    try:
        return int(hour) * 60 + int(minute)
    except ValueError:
        return 0


def _hhmm(minutes: int) -> str:
    minutes = max(0, min(23 * 60 + 59, minutes))
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def _fit_schedule(activities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按开始时间排序，并把与前一项重叠的开始时间顺延到前一项结束。"""
    ordered = sorted(activities, key=lambda item: _minutes_of(item.get("start")))
    result = []
    previous_end = -1
    for activity in ordered:
        start = _minutes_of(activity.get("start"))
        if start < previous_end:
            start = previous_end
        previous_end = start + _duration_minutes(activity.get("duration"))
        result.append({**activity, "start": _hhmm(start)})
    return result
_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _clip(value: Any, limit: int, fallback: str) -> str:
    text = str(value or "").strip()
    return text[:limit] if text else fallback


def _clean_activities(activities: List[Dict[str, Any]], limit: int = 3,
                      evidence: Optional[Dict[str, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """统一活动字段：时间缺失就按顺序给默认值，标题必须落在证据地点名上。"""
    result = []
    for position, activity in enumerate(activities[:limit]):
        start = str(activity.get("start") or "").strip()
        if not _TIME_PATTERN.match(start):
            start = DEFAULT_STARTS[min(position, len(DEFAULT_STARTS) - 1)]
        source = (evidence or {}).get(activity.get("evidence_id")) or {}
        names = [str(item.get("name") or "") for item in source.get("items", []) if item.get("name")]
        display = str(activity.get("display_title") or "").strip()[:24]
        if display and names and not title_matches(display, names):
            display = ""
        title = display or (clean_place_name(names[0]) if names else clean_place_name(activity.get("title")) )
        result.append({
            "title": title,
            "start": start,
            "duration": _clip(activity.get("duration"), 20, "1.5 小时"),
            "description": _clip(activity.get("description"), 400, "请通过来源页面核对细节。"),
            "transport": _clip(activity.get("transport"), 60, "交通未核实"),
            "cost": _clip(activity.get("cost"), 40, "未核实"),
            "evidence_id": activity.get("evidence_id"),
        })
    return result


DURATION_PATTERN = re.compile(r"(\d+|[一二两三四五六七八九十])\s*(天|日|晚)|周末|半天|一日游")
BUDGET_PATTERN = re.compile(r"(预算|人均)[^\d一二三四五六七八九十]{0,6}(\d+(?:\.\d+)?|[一二三四五六七八九十百千]+)\s*(万|千|w|k)?")


def _declares_duration(text: str) -> bool:
    return bool(DURATION_PATTERN.search(str(text or "")))


_CN_DIGITS = "零一二三四五六七八九"


def chinese_number(text: str) -> int:
    """把“五百”“一千二”这类写法转成整数，失败返回 0。"""
    total, section, number = 0, 0, 0
    for char in text or "":
        if char in _CN_DIGITS:
            number = _CN_DIGITS.index(char)
        elif char == "十":
            section += (number or 1) * 10
            number = 0
        elif char == "百":
            section += (number or 1) * 100
            number = 0
        elif char == "千":
            section += (number or 1) * 1000
            number = 0
        else:
            return 0
    return total + section + number


def budget_conflict(query: str, days: int) -> bool:
    """预算与“五星/豪华”明显不匹配时算冲突。"""
    match = BUDGET_PATTERN.search(query or "")
    if not match or not any(word in query for word in ("五星", "豪华", "高档", "别墅")):
        return False
    raw = match.group(2)
    amount = chinese_number(raw) if raw[0] in _CN_DIGITS else float(raw)
    unit = match.group(3) or ""
    if unit in ("万", "w"):
        amount *= 10000
    elif unit in ("千", "k"):
        amount *= 1000
    return amount / max(1, days) < 600


def cities_fit_days(destinations: List[str], days: int) -> bool:
    """一天最多跨一个城市，城市数超过天数就认为约束矛盾。"""
    return len(destinations) <= max(1, int(days))


def _finalize_days(cleaned_days: List[Dict[str, Any]], good: List[Dict[str, Any]],
                   plan_days: List[Dict[str, Any]],
                   keep_days: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """按天去重、补足到 2 个活动并按时间排序，避免出现空天或倒序。

    keep_days 是本次不动、原样保留的日期；只对 plan_days 里的日期做整理。
    """
    kept = [dict(day) for day in (keep_days or [])]
    by_day = {day.get("day"): day for day in cleaned_days if day.get("day")}
    used = {str(activity.get("title") or "").strip()
            for day in kept for activity in day.get("activities", [])}
    result = []
    for planned in plan_days:
        day = by_day.get(planned.get("day")) or {"day": planned.get("day"), "city": planned.get("city")}
        activities = []
        for activity in day.get("activities", []):
            title = str(activity.get("title") or "").strip()
            if not title or title in used:
                continue
            used.add(title)
            activities.append({**activity, "title": title})
        if len(activities) < 3:
            for evidence in good:
                if not evidence["id"].startswith("d%d-" % planned["day"]):
                    continue
                for item in evidence.get("items", []):
                    name = clean_place_name(item.get("name"))
                    if not name or name in used:
                        continue
                    used.add(name)
                    activities.append({
                        "title": name,
                        "start": DEFAULT_STARTS[min(len(activities), len(DEFAULT_STARTS) - 1)],
                        "duration": "1.5 小时",
                        "description": "作为当天的备选安排，建议停留约 1.5 小时；开放时间与交通以来源页面为准。",
                        "transport": "交通未核实",
                        "cost": "未核实",
                        "evidence_id": evidence["id"],
                    })
                    if len(activities) >= 3:
                        break
                if len(activities) >= 3:
                    break
        result.append({"day": planned["day"], "city": day.get("city") or planned.get("city"),
                       "activities": _fit_schedule(activities)[:3]})
    result.extend(kept)
    result.sort(key=lambda item: int(item.get("day") or 0))
    return result


def _span(name: str, kind: str, status: str, latency_ms: float = 0.0,
          attributes: Optional[Dict[str, Any]] = None,
          events: Optional[List[Dict[str, Any]]] = None,
          children: Optional[List[Dict[str, Any]]] = None,
          error_type: Optional[str] = None) -> Dict[str, Any]:
    """构造一个执行轨迹节点（trace span），只记录事实，不做判定。"""
    span: Dict[str, Any] = {"name": name, "kind": kind, "status": status,
                            "latency_ms": int(latency_ms), "attributes": attributes or {}}
    if events:
        span["events"] = events
    if children:
        span["children"] = children
    if error_type:
        span["error_type"] = error_type
    return span


def _tool_spans(dag_result: Dict[str, Any], plan: Any) -> List[Dict[str, Any]]:
    """把调度器的节点结果转成工具子 span。"""
    nodes = {node.id: node for node in plan.nodes}
    spans: List[Dict[str, Any]] = []
    for item in dag_result.get("results", []):
        node = nodes.get(str(item.get("node_id")))
        day = None
        if node is not None:
            matched = re.match(r"^d(\d+)-", node.id)
            day = int(matched.group(1)) if matched else None
        parsed: Any = None
        try:
            parsed = json.loads(item.get("output") or "")
        except (TypeError, ValueError):
            parsed = None
        evidence_status = parsed.get("status") if isinstance(parsed, dict) else None
        status, error_type = "ok", None
        if item.get("status") == "skipped":
            status, error_type = "blocked", "tool_error"
        elif item.get("status") != "success":
            status, error_type = "failed", "tool_error"
        elif evidence_status == "unavailable":
            status, error_type = "failed", "auth_missing"
        elif evidence_status == "failed":
            status, error_type = "failed", "tool_error"
        elif evidence_status == "empty":
            status, error_type = "warn", "empty_result"
        if isinstance(parsed, dict) and parsed.get("reused"):
            status = "degraded"
        attributes = {"day": day, "cache_hit": bool(item.get("cache_hit")),
                      "attempts": int(item.get("attempts") or 0),
                      "reused": bool(parsed.get("reused")) if isinstance(parsed, dict) else False}
        events = [{"name": error_type, "reason": str(item.get("error") or "")[:120]}] if error_type else None
        spans.append(_span(node.tool if node is not None else "tool", "tool", status,
                           float(item.get("latency_ms") or 0.0), attributes,
                           events=events, error_type=error_type))
    return spans


def _critical_path_ms(plan: Any, dag_result: Dict[str, Any]) -> float:
    """按依赖关系计算最长路径耗时。"""
    latency = {str(item.get("node_id")): float(item.get("latency_ms") or 0.0)
               for item in dag_result.get("results", [])}
    nodes = {node.id: node for node in plan.nodes}
    memo: Dict[str, float] = {}

    def longest(node_id: str) -> float:
        if node_id in memo:
            return memo[node_id]
        node = nodes.get(node_id)
        upstream = max((longest(dep) for dep in node.depends_on if dep in nodes), default=0.0) if node else 0.0
        memo[node_id] = latency.get(node_id, 0.0) + upstream
        return memo[node_id]

    return max((longest(node_id) for node_id in nodes), default=0.0)


def _fabricated_costs(days: List[Dict[str, Any]], evidence: List[Dict[str, Any]]) -> int:
    """活动费用里出现、但证据文本中没有的数字，算编造。"""
    corpus = " ".join(
        " ".join(str(entry.get(part) or "") for part in ("name", "snippet", "address"))
        for evidence_item in evidence for entry in evidence_item.get("items", [])
    )
    count = 0
    for day in days:
        for activity in day.get("activities", []):
            cost = str(activity.get("cost") or "")
            for number in re.findall(r"\d+(?:\.\d+)?", cost):
                if number not in corpus:
                    count += 1
                    break
    return count


def _time_order_ok(days: List[Dict[str, Any]]) -> bool:
    for day in days:
        starts = [str(activity.get("start") or "") for activity in day.get("activities", [])]
        if starts != sorted(starts):
            return False
    return True


class ResearchTools:
    def __init__(self) -> None:
        self.amap_key = os.getenv("AMAP_MAPS_API_KEY", "")
        self.search_key = os.getenv("DEEPSEEK_API_KEY") or (
            os.getenv("OPENAI_API_KEY", "") if "api.deepseek.com" in os.getenv("OPENAI_BASE_URL", "") else "")

    async def web_search(self, keyword: str, city: str, evidence_id: str) -> Dict[str, Any]:
        if not self.search_key:
            return build_evidence(evidence_id, "web", keyword, city, "unavailable",
                                  source="网页搜索", source_kind="none",
                                  reason="未配置可用的地点搜索服务")
        try:
            async with httpx.AsyncClient(timeout=18) as client:
                response = await client.post("https://api.deepseek.com/v1/tools/search",
                    headers={"Authorization": "Bearer " + self.search_key},
                    json={"query": (city + " " + keyword + " 地点介绍 地址")[0:128], "count": 8,
                          "need_content": False})
                response.raise_for_status()
                data = response.json()
            items = _rank_search_items(data.get("results"), keyword, city)
            return build_evidence(evidence_id, "web", keyword, city,
                                  "ok" if items else "empty",
                                  items=items, source="DeepSeek 网页搜索", source_kind="web_search")
        except (httpx.HTTPError, ValueError, KeyError):
            return build_evidence(evidence_id, "web", keyword, city, "failed",
                                  source="DeepSeek 网页搜索", source_kind="web_search",
                                  reason="网页搜索暂时不可用")

    async def poi(self, keyword: str, city: str, evidence_id: str) -> Dict[str, Any]:
        if not self.amap_key:
            return await self.web_search(keyword, city, evidence_id)
        try:
            async with httpx.AsyncClient(timeout=12) as client:
                response = await client.get("https://restapi.amap.com/v5/place/text", params={
                    "key": self.amap_key, "keywords": keyword, "region": city, "page_size": 3})
                response.raise_for_status()
                data = response.json()
            if data.get("status") != "1":
                return await self.web_search(keyword, city, evidence_id)
            items = []
            for poi in data.get("pois") or []:
                location = str(poi.get("location", "")).split(",")
                coordinates = None
                if len(location) == 2:
                    try:
                        coordinates = [float(location[0]), float(location[1])]
                    except ValueError:
                        pass
                items.append({"name": poi.get("name", ""), "address": poi.get("address", ""),
                              "coordinates": coordinates, "city": poi.get("cityname") or city,
                              "adcode": poi.get("adcode", "")})
            return build_evidence(evidence_id, "poi", keyword, city,
                                  "ok" if items else "empty",
                                  items=items, source="高德地图 POI", source_kind="map_poi")
        except (httpx.HTTPError, ValueError):
            return await self.web_search(keyword, city, evidence_id)


class AgentWorkflow:
    def __init__(self, store: Store, model: Optional[ModelGateway] = None,
                 tools: Optional[ResearchTools] = None) -> None:
        self.store = store
        self.model = model or ModelGateway()
        self.tools = tools or ResearchTools()

    def _event(self, job: Dict[str, Any], stage: str, message: str,
               extra: Optional[Dict[str, Any]] = None) -> None:
        self.store.event(job["id"], "progress", {"stage": stage, "message": message, **(extra or {})})

    async def run(self, job: Dict[str, Any]) -> Dict[str, Any]:
        started = time.monotonic()
        query = job["query"]
        calls: List[Dict[str, str]] = []
        stages: List[Dict[str, Any]] = []
        prior = job.get("previous") or {}
        revision_text = str(job.get("revision") or "").strip()
        prior_days = prior.get("days") if isinstance(prior.get("days"), list) else []
        targets = (
            parse_target_days(revision_text, len(prior_days))
            if revision_text and prior_days and prior.get("constraints") else []
        )

        def finish(trace_status: str, **result: Any) -> Dict[str, Any]:
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
            intake_started = time.monotonic()
            intake = await self.model.complete("需求 Agent",
                "你负责提取旅行需求。只输出 JSON 对象，字段：departure_city 字符串、destinations 字符串数组、days 1-7 整数、budget 字符串、preferences 字符串数组、missing_required 字符串数组、conflicts 字符串数组。未给出的值用空字符串或空数组；绝不猜测城市。用户已在某地出差/停留、或只说了城市名加天数，该城市也算 destinations；只有完全看不出目的地时才留空。conflicts 用一句话列出需求中的硬冲突（例如“五天带老人孩子预算 1000 元”“人均五百还想住五星酒店”“没说明玩几天”），没有冲突就给空数组。",
                query, max_tokens=500)
            calls.append({k: intake[k] for k in ("role", "provider", "model")})
            constraints = parse_json(intake["content"])
            destinations = _as_list(constraints.get("destinations"), 4)
            declared = _declares_duration(query)
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
            conflicts = _as_list(constraints.get("conflicts"), 3)
            if not declared:
                stages.append(_span("intake", "stage", "failed",
                                    (time.monotonic() - intake_started) * 1000,
                                    {**intake_attributes, "days": days_count, "decision": "clarify"},
                                    error_type="missing_days"))
                return finish("ok", status="needs_clarification", question="打算玩几天？",
                              constraints=constraints, model_calls=calls, days=[], evidence=[])
            budget_clash = budget_conflict(query, days_count)
            if conflicts or budget_clash:
                reason = "；".join(conflicts[:2]) or "预算和住宿标准对不上"
                stages.append(_span("intake", "stage", "failed",
                                    (time.monotonic() - intake_started) * 1000,
                                    {**intake_attributes, "days": days_count, "decision": "clarify",
                                     "conflicts": conflicts},
                                    error_type="missed_budget" if budget_clash else "should_clarify"))
                return finish("ok", status="needs_clarification", question="需求里有冲突：%s。想先调整哪一项？" % reason,
                              constraints=constraints, model_calls=calls, days=[], evidence=[])
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
        keep_evidence: List[Dict[str, Any]] = []
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
        answer_system = (
            "根据给定证据生成行程。只输出 JSON：{\"summary\":\"概述这趟行程的安排思路和取舍\","
            "\"days\":[{\"day\":1,\"city\":\"城市\",\"activities\":[{\"title\":\"证据里的地点名\","
            "\"display_title\":\"展示用短名\",\"start\":\"09:30\",\"duration\":\"2.5 小时\","
            "\"description\":\"具体做什么、为什么适合用户\",\"transport\":\"从上一站怎么过来\","
            "\"cost\":\"门票或人均\",\"evidence_id\":\"d1-p1\"}]}],\"notes\":[\"出行前需核对的事项\"]}。"
            "规则：1) title 必须精确等于证据 items 中一个 name，evidence_id 必须指向该条成功证据；"
            "display_title 是展示用的简短中文名（不超过 16 字，如“湖滨咖啡馆”），不得虚构新地点；"
            "2) 每天 3 个活动（当天证据不足时可少于 3 个），按当天时间顺序排列，start 用 24 小时制，"
            "相邻活动不能重叠（前一项 start + duration 不得晚于后一项 start），跨天不要重复同一地点；"
            "3) description 写具体动作与理由（看什么、走哪段、为什么符合用户偏好），不要写“感受氛围”“湖光山色”这类空话；"
            "4) transport 根据证据里的地址和城市信息给出行建议，推断不了就写“交通未核实”；"
            "5) cost 只写证据里出现的价格，没有就写“未核实”，不得编造价格、营业时间、班次；"
            "6) kind=web 只代表有可追溯网页来源，不代表地址或营业状态已核实；"
            "7) notes 最多 6 条，写出行前必须核对的事项。"
        )
        if targets:
            answer_system += (
                " 本次只修改%s：只输出这些天的 days 条目，其他天不要输出。"
                " keep_days 是已经定稿、本次不动的日期：不要重复其中的地点，新安排要与它们的节奏和交通衔接。"
                % describe_targets(targets)
            )
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
                                 "activities": _clean_activities(matched_activities, evidence={e["id"]: e for e in evidence})})
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
                keep_days=kept_days)
        else:
            cleaned_days = _finalize_days(cleaned_days, good, days)
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
        revision_applied: Optional[bool] = None
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
