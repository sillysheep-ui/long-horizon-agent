"""把“第 N 天改一下”解析成定向重排范围。

只做确定性的文本解析与计划构造：识别目标日期、合并新旧行程、
为指定日期生成 poi 查询节点。识别不到日期时返回空列表，
调用方应退回整单重排。
"""

import re
from typing import Any

from inference.tool_dag import ToolDAGError, ToolPlan
from product.planner import MAX_NODES, MAX_QUERIES_PER_DAY, NODE_TIMEOUT_SECONDS

_CHINESE_DIGITS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
# “倒数第二天”里的“第二天”不算第 2 天。
_DAY_PATTERN = re.compile(r"(?<!倒数)第\s*([0-9一二两三四五六七八九十]{1,3})\s*天")
_DAY_EN_PATTERN = re.compile(r"(?:day|Day|DAY)\s*([1-9][0-9]?)")
_LAST_DAY_WORDS = ("最后一天", "最后 1 天", "末尾那天", "末日")
_EVIDENCE_DAY = re.compile(r"^d(\d+)-")


def _day_number(token: str) -> int:
    """把“2”“十一”“二十”转成整数；无法识别时返回 0。"""
    token = token.strip()
    if token.isdigit():
        return int(token)
    if token == "十":
        return 10
    if "十" in token:
        head, _, tail = token.partition("十")
        tens = _CHINESE_DIGITS.get(head, 1) if head else 1
        ones = _CHINESE_DIGITS.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _CHINESE_DIGITS.get(token, 0)


def mentioned_days(text: str) -> list[int]:
    """返回文本里点名的所有日期（不做范围过滤），去重升序。"""
    if not text:
        return []
    found = set()
    for match in _DAY_PATTERN.finditer(text):
        day = _day_number(match.group(1))
        if day >= 1:
            found.add(day)
    for match in _DAY_EN_PATTERN.finditer(text):
        found.add(int(match.group(1)))
    return sorted(found)


def out_of_range_days(text: str, total_days: int) -> list[int]:
    """返回超出 1..total_days 的日期，例如两天行程里提到的“第五天”。"""
    return [day for day in mentioned_days(text) if day > total_days]


def parse_target_days(text: str, total_days: int) -> list[int]:
    """返回文本里点名且落在 1..total_days 内的日期，去重升序。"""
    if not text or total_days < 1:
        return []
    found = {day for day in mentioned_days(text) if day <= total_days}
    if any(word in text for word in _LAST_DAY_WORDS):
        found.add(total_days)
    return sorted(found)


def day_of_evidence(evidence_id: Any) -> int:
    """从证据 id（d2-p1）里取出日期；取不到返回 0。"""
    match = _EVIDENCE_DAY.match(str(evidence_id or ""))
    return int(match.group(1)) if match else 0


def split_by_targets(evidence: list[dict[str, Any]], targets: list[int]) -> "tuple[list[dict[str, Any]], list[dict[str, Any]]]":
    """把证据分成“保留的”和“本次要替换的”两组。"""
    target_set = set(targets)
    keep, replace = [], []
    for item in evidence:
        (replace if day_of_evidence(item.get("id")) in target_set else keep).append(item)
    return keep, replace


def merge_days(previous_days: list[dict[str, Any]], new_days: list[dict[str, Any]],
               targets: list[int]) -> list[dict[str, Any]]:
    """用新结果替换目标日期，其余日期沿用旧结果，最后按天排序。"""
    target_set = set(targets)
    merged: dict[int, dict[str, Any]] = {}
    for day in previous_days:
        try:
            number = int(day.get("day"))
        except (TypeError, ValueError):
            continue
        if number not in target_set:
            merged[number] = day
    for day in new_days:
        try:
            number = int(day.get("day"))
        except (TypeError, ValueError):
            continue
        if number in target_set:
            merged[number] = day
    return [merged[key] for key in sorted(merged)]


def days_to_revision_plan(
    days: list[dict[str, Any]],
    targets: list[int],
    days_count: int,
    destinations: list[str],
    *,
    max_nodes: int = MAX_NODES,
    node_timeout_seconds: float = NODE_TIMEOUT_SECONDS,
) -> ToolPlan:
    """只为目标日期构造 poi 查询节点，节点 id 仍按全局日期编号（d2-p1）。"""
    if not targets:
        raise ValueError("没有指定要重排的日期")
    if not destinations or not str(destinations[0]).strip():
        raise ValueError("缺少目的地，无法生成查询计划")
    if not isinstance(days, list) or len(days) != days_count:
        raise ValueError("规划 Agent 返回的天数与需求不一致")

    target_set = set(targets)
    nodes: list[dict[str, Any]] = []
    for day_index, day in enumerate(days, 1):
        if day_index not in target_set or not isinstance(day, dict):
            continue
        city = str(day.get("city") or destinations[0]).strip()[:40] or destinations[0][:40]
        queries = [str(item).strip()[:80] for item in (day.get("poi_queries") or [])[:MAX_QUERIES_PER_DAY]
                   if str(item).strip()]
        if not queries:
            queries = [city + " 景点"]
        day["day"] = day_index
        day["city"] = city
        day["poi_queries"] = queries
        for query_index, keyword in enumerate(queries, 1):
            nodes.append({
                "id": "d%d-p%d" % (day_index, query_index),
                "tool": "poi",
                "arguments": {"city": city, "keyword": keyword},
                "timeout_seconds": node_timeout_seconds,
            })
    if not nodes:
        raise ValueError("没有可重排的日期")
    if len(nodes) > max_nodes:
        raise ValueError("计划超过工具调用预算")
    try:
        return ToolPlan.from_dict(
            {"goal": "定向重排第 %s 天" % "、".join(str(day) for day in targets), "nodes": nodes},
            allowed_tools={"poi"}, max_nodes=max_nodes,
        )
    except ToolDAGError as exc:
        raise ValueError("计划未通过校验: %s" % exc) from exc


def compact_days(days: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把已成稿的日期压缩成“日期 + 标题/时间”，用于给模型提供上下文。"""
    compacted = []
    for day in days:
        activities = []
        for activity in day.get("activities") or []:
            if not isinstance(activity, dict):
                continue
            activities.append({
                "title": activity.get("title"),
                "start": activity.get("start"),
                "duration": activity.get("duration"),
            })
        compacted.append({"day": day.get("day"), "city": day.get("city"), "activities": activities})
    return compacted


def describe_targets(targets: list[int]) -> str:
    return "、".join("第%d天" % day for day in targets)
