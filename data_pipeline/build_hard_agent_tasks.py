#!/usr/bin/env python3
"""构建用于采集真实动态轨迹的 L3/L4 旅行 Agent 压力测试任务。"""

from __future__ import annotations

import json
from pathlib import Path


CASES = [
    ("成都", "九寨沟", "周末两天", "自驾、避开高风险路段、沿途安排一晚住宿"),
    ("上海", "黄山", "下周末", "高铁优先、若天气不适合登山需给出替代方案"),
    ("北京", "阿尔山", "五一假期", "公共交通优先、总预算 3500 元以内"),
    ("广州", "阳朔", "三天两晚", "亲子出行、雨天不能安排户外漂流"),
    ("西安", "甘南", "四天", "自驾、每日驾驶不超过 5 小时、兼顾高原适应"),
    ("杭州", "景德镇", "周末", "高铁出行、需要陶瓷体验与夜间返程备选"),
    ("重庆", "武隆", "两天", "带老人、步行负担低、遇降雨改室内活动"),
    ("昆明", "大理", "三天", "预算 2200 元、民宿和交通总价超预算时重规划"),
]


def l3_task(index, origin, destination, duration, constraints):
    return {
        "id": f"l3_{index:02d}",
        "tier": "L3_conditional_planning",
        "query": f"请规划从{origin}到{destination}{duration}的行程。要求：{constraints}。请先核验路线、天气和关键 POI；若任一结果与约束冲突，调整后续查询与方案，不要套用固定路线。",
        "required_constraints": ["路线/交通", "天气", "关键 POI 或住宿", "至少一个结果驱动的后续调整"],
        "runtime_branch_signals": ["天气与户外活动冲突", "交通时长或预算超限", "POI/住宿不可用或距离不符"],
        "min_distinct_tool_types": 3,
        "min_conditional_decisions": 1,
    }


def l4_task(index, origin, destination, duration, constraints):
    return {
        "id": f"l4_{index:02d}",
        "tier": "L4_recovery_and_conflict",
        "query": f"我要从{origin}去{destination}玩{duration}，需求是：{constraints}。请给出可执行计划，并在工具结果为空、地点不匹配、天气冲突或预算超限时，主动重规划或说明无法确认的部分；不要用未经核验的信息补全。",
        "required_constraints": ["多约束覆盖", "至少一次证据核验", "失败/冲突时修复或保守终止"],
        "runtime_branch_signals": ["工具空结果或业务错误", "地点地理歧义", "证据间冲突", "约束无法同时满足"],
        "min_distinct_tool_types": 3,
        "min_conditional_decisions": 2,
    }


def main():
    tasks = []
    for i, case in enumerate(CASES, 1):
        tasks.append(l3_task(i, *case))
        tasks.append(l4_task(i, *case))
    assert len({task["id"] for task in tasks}) == len(tasks)
    assert sum(task["tier"].startswith("L3") for task in tasks) == 8
    assert sum(task["tier"].startswith("L4") for task in tasks) == 8
    output = Path("data/final/agent_prm_hard_tasks_v1.jsonl")
    with output.open("w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")
    print(json.dumps({"output": str(output), "total": len(tasks), "l3": 8, "l4": 8}, ensure_ascii=False))


if __name__ == "__main__":
    main()
