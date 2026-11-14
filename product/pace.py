"""行程节奏（pace）识别与分档规则，供编排器与评测共用。"""

from typing import Any

PACE_WORDS = {
    "relaxed": ("轻松", "松弛", "悠闲", "慢慢", "不赶", "佛系", "少走", "休息", "缓"),
    "packed": ("紧凑", "特种兵", "多逛", "尽量多", "充实", "满满", "一天当两天", "多去", "多安排"),
}

# (每天最少活动数, 每天最多活动数, 全天最少小时, 全天最多小时)
PACE_LIMITS = {
    "relaxed": (2, 3, 3.0, 6.0),
    "normal": (2, 3, 3.0, 8.0),
    "packed": (3, 4, 5.0, 10.0),
}

PACE_RULES = {
    "relaxed": "用户偏好松弛：每天 2 个活动，全天活动总时长 3-6 小时，留足休息和用餐时间，不要塞满。",
    "normal": "用户没有特别说明节奏：每天 2-3 个活动，全天活动总时长 3-8 小时。",
    "packed": "用户偏好紧凑：每天 3-4 个活动，全天活动总时长 5-10 小时，减少空档。",
}

NEGATIONS = ("不要", "别", "不想", "不必", "不用")


def detect_pace(*texts: Any) -> str:
    """从用户原话判断节奏；带否定的紧凑词按松弛处理。"""
    text = " ".join(str(item or "") for item in texts)
    for word in PACE_WORDS["relaxed"]:
        if word in text:
            return "relaxed"
    for word in PACE_WORDS["packed"]:
        position = text.find(word)
        if position < 0:
            continue
        prefix = text[max(0, position - 4):position]
        if any(token in prefix for token in NEGATIONS):
            return "relaxed"
        return "packed"
    return "normal"
