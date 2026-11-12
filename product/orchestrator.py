"""明确职责和共享状态的旅行规划工作流。"""

import json
import os
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from inference.tool_dag import ToolDAGScheduler
from product.model_gateway import ModelGateway, parse_json
from product.planner import PLANNER_SYSTEM_PROMPT, build_planner_prompt, days_to_tool_plan
from product.provenance import build_evidence, normalize_all
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
        name = title
        for suffix in ("_百度百科", " - 百度百科", " - 搜词条", "-百度百科"):
            if suffix in name:
                name = name.split(suffix, 1)[0]
        score = (6 if keyword in title else 0) + (3 if short in title else 0)
        score += (2 if city in title else 0) + (1 if keyword in snippet else 0)
        score += (4 if parsed.hostname.endswith("gov.cn") else 0)
        candidates.append((score, {"name": name[:100], "source_title": title[:140],
                                   "address": "", "url": url, "snippet": snippet,
                                   "coordinates": None}))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in candidates[:3]]


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
        self._event(job, "intake", "正在理解日期、目的地和预算")
        intake = await self.model.complete("需求 Agent",
            "你负责提取旅行需求。只输出 JSON 对象，字段：departure_city 字符串、destinations 字符串数组、days 1-7 整数、budget 字符串、preferences 字符串数组、missing_required 字符串数组。未给出的值用空字符串或空数组；绝不猜测城市。",
            query, max_tokens=500)
        calls.append({k: intake[k] for k in ("role", "provider", "model")})
        constraints = parse_json(intake["content"])
        destinations = _as_list(constraints.get("destinations"), 4)
        if not destinations:
            return {"status": "needs_clarification", "question": "你想去哪个城市或地区？",
                    "constraints": constraints, "model_calls": calls, "days": [], "evidence": []}
        try:
            days_count = min(7, max(1, int(constraints.get("days") or 1)))
        except (TypeError, ValueError):
            days_count = 1
        constraints["days"] = days_count

        self._event(job, "planning", "正在拆解每日行程和查询任务")
        prior = job.get("previous") or {}
        plan_response = await self.model.complete("规划 Agent", PLANNER_SYSTEM_PROMPT,
            build_planner_prompt(query, constraints, prior.get("plan")), max_tokens=1000)
        calls.append({k: plan_response[k] for k in ("role", "provider", "model")})
        plan = parse_json(plan_response["content"])
        days = plan.get("days")
        tool_plan = days_to_tool_plan(days, days_count, destinations)

        self._event(job, "research", "正在并行查询地点来源", {"tool_count": len(tool_plan.nodes)})
        executor = ResearchExecutor(self.tools, normalize_all(prior.get("evidence", [])))
        scheduler = ToolDAGScheduler(executor=executor, max_concurrency=4, fail_fast=False)
        dag_result = await scheduler.run(tool_plan)
        evidence = normalize_all(executor.collect(dag_result, tool_plan))

        good = [e for e in evidence if e.get("status") == "ok"]
        self._event(job, "verification", "正在检查计划是否有可追溯的地点来源",
                    {"supported_queries": len(good), "total_queries": len(evidence)})
        if not good:
            return {"status": "partial", "question": "地点查询暂不可用，无法给出可信的具体行程。",
                    "constraints": constraints, "plan": plan, "days": [],
                    "evidence": evidence, "model_calls": calls,
                    "elapsed_seconds": round(time.monotonic() - started, 2)}

        self._event(job, "answer", "正在根据已核实地点组织行程")
        answer = {}
        answer_failed = False
        try:
            answer_response = await self.model.complete("回答 Agent",
                "根据给定证据生成行程。只输出 JSON：{\"summary\":\"简短概述\",\"days\":[{\"day\":1,\"city\":\"城市\",\"activities\":[{\"title\":\"地点\",\"description\":\"安排\",\"evidence_id\":\"d1-p1\"}]}],\"notes\":[\"未核实事项\"]}。title 必须精确等于证据 items 中一个 name；evidence_id 必须指向成功证据。kind=web 仅代表有可追溯网页来源，不代表精确地址或营业状态已核实；不要据此声称地理位置已验证。没有可靠价格、营业时间、路线或票务证据时明确写未知，不得编造。火车/飞机模拟数据如出现必须明确标注模拟。",
                json.dumps({"query": query, "constraints": constraints, "plan": plan,
                            "evidence": good}, ensure_ascii=False), max_tokens=3000)
            calls.append({k: answer_response[k] for k in ("role", "provider", "model")})
            answer = parse_json(answer_response["content"])
        except (ValueError, RuntimeError):
            answer_failed = True
            self._event(job, "answer", "回答格式不符合协议，正在使用来源数据构造保守方案")
        valid_names = {e["id"]: {item["name"] for item in e.get("items", [])} for e in good}
        cleaned_days = []
        for day in answer.get("days", []) if isinstance(answer.get("days"), list) else []:
            activities = []
            for activity in day.get("activities", []) if isinstance(day, dict) else []:
                if not isinstance(activity, dict):
                    continue
                matched = match_activity(activity, valid_names)
                if matched:
                    activities.append({**activity, "evidence_id": matched[0], "title": matched[1]})
            cleaned_days.append({"day": day.get("day"), "city": day.get("city"),
                                 "activities": activities})
        if not any(d["activities"] for d in cleaned_days):
            answer_failed = True
            cleaned_days = []
            for day in days:
                day_evidence = [e for e in good if e["id"].startswith("d%d-" % day["day"]) and e.get("items")]
                activities = [{"title": e["items"][0]["name"], "evidence_id": e["id"],
                               "description": "可作为当天的候选地点；请通过来源页面核对地址、开放时间和交通。"}
                              for e in day_evidence]
                cleaned_days.append({"day": day["day"], "city": day["city"], "activities": activities})
        notes = _as_list(answer.get("notes"), 8)
        if answer_failed:
            notes.insert(0, "回答阶段使用保守方案，安排基于可追溯来源，具体细节需出行前核对。")
        return {"status": "complete" if any(d["activities"] for d in cleaned_days) else "partial",
                "summary": str(answer.get("summary") or "已根据可追溯地点来源整理行程。"), "days": cleaned_days,
                "notes": notes, "constraints": constraints,
                "plan": plan, "evidence": evidence, "model_calls": calls,
                "elapsed_seconds": round(time.monotonic() - started, 2)}
