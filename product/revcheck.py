"""修改要求达标检查：判断目标日是否真的满足了用户这次提的语义要求。

只做确定性判断（关键词 + 时长/数量），判不了的情况算“通过”，
避免把“安静”这种主观要求误判成失败。
"""

from __future__ import annotations

import re
from typing import Any

REQUIREMENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "安静": ("安静", "静谧", "清幽", "人少", "幽静", "竹林", "茶园", "湿地", "森林公园",
             "步道", "书院", "寺", "庵", "禅", "书屋", "竹", "茶", "溪", "谷", "坞", "林",
             "村", "园"),
    "适合孩子": ("亲子", "儿童", "孩子", "乐园", "动物园", "科技馆", "海洋馆", "博物馆",
                 "公园", "农场", "植物园"),
    "室内": ("博物馆", "美术馆", "展览", "馆", "室内", "书店", "商场", "剧院"),
    "户外": ("公园", "山", "湖", "街", "古镇", "步道", "湿地", "岛", "海滩", "草原", "峡谷"),
    "自然风光": ("山", "湖", "森林", "湿地", "公园", "溪", "岛", "草原", "峡谷", "瀑布", "江"),
    "博物馆": ("博物馆", "美术馆", "纪念馆", "展览", "博物院"),
    "美食": ("餐", "菜", "小吃", "美食", "夜市", "面", "火锅", "茶", "咖啡", "饭"),
    "拍照": ("拍照", "摄影", "夜景", "日出", "观景", "打卡", "机位"),
}

_HOURS = re.compile(r"(\d+(?:\.\d+)?)\s*小时")
_MINS = re.compile(r"(\d+)\s*分钟")
SLOW_MIN_HOURS = 1.5
SLOW_MAX_ACTIVITIES = 3
COMPACT_MIN_ACTIVITIES = 3


def _hours(activity: dict[str, Any]) -> float:
    text = str(activity.get("duration") or "")
    match = _HOURS.search(text)
    if match:
        return float(match.group(1))
    match = _MINS.search(text)
    return float(match.group(1)) / 60 if match else 0.0


def _text(activity: dict[str, Any]) -> str:
    return " ".join(str(activity.get(key) or "") for key in
                    ("title", "display_title", "description", "transport"))


def _satisfied(label: str, activities: list[dict[str, Any]]) -> bool:
    if not activities:
        return False
    if label == "慢节奏":
        return (all(_hours(item) >= SLOW_MIN_HOURS for item in activities)
                and len(activities) <= SLOW_MAX_ACTIVITIES)
    if label == "紧凑":
        return len(activities) >= COMPACT_MIN_ACTIVITIES
    words = REQUIREMENT_KEYWORDS.get(label)
    if not words:
        return True
    return any(word in _text(item) for item in activities for word in words)


def check_requirements(requirements: list[str], days: list[dict[str, Any]]) -> dict[str, Any]:
    """返回 {"met": {要求: bool}, "unmet": [...]}；目标日为空时全部判失败。"""
    met: dict[str, bool] = {}
    unmet: list[str] = []
    for label in requirements:
        activities = [activity for day in days for activity in day.get("activities", [])]
        ok = _satisfied(label, activities)
        met[label] = ok
        if not ok:
            unmet.append(label)
    return {"met": met, "unmet": unmet}
