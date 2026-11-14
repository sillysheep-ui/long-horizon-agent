"""行程节奏（pace）识别与分档规则，供编排器与评测共用。

节奏区分的主口径是“每件事干多久、两件事之间留多久”，
每天的活动数量由可用时间和这两者自然决定，而不是先定个数。
"""

from typing import Any

PACE_WORDS = {
    "relaxed": ("轻松", "松弛", "悠闲", "慢慢", "不赶", "佛系", "少走", "休息", "缓"),
    "packed": ("紧凑", "特种兵", "多逛", "尽量多", "充实", "满满", "一天当两天", "多去", "多安排"),
}

# 每个活动时长（小时）、活动之间的间隔（分钟）、每天可用时长（小时）
PACE_PLAN = {
    "relaxed": {"activity": (2.0, 3.5), "gap": (45, 120), "day": (3.0, 8.0),
                "count": (2, 3), "free": (0.18, 1.0)},
    "normal": {"activity": (1.5, 2.5), "gap": (30, 75), "day": (3.0, 8.5),
               "count": (2, 3), "free": (0.10, 0.65)},
    "packed": {"activity": (1.0, 1.5), "gap": (15, 45), "day": (3.5, 10.0),
               "count": (3, 4), "free": (0.0, 0.55)},
}

# (每天最少活动数, 每天最多活动数, 全天最少小时, 全天最多小时)：数量的兜底边界
PACE_LIMITS = {level: (plan["count"][0], plan["count"][1], plan["day"][0], plan["day"][1])
               for level, plan in PACE_PLAN.items()}

PACE_RULES = {
    "relaxed": ("用户偏好松弛：每个活动安排 2-3.5 小时，两个活动之间留 45-120 分钟（休息、吃饭、路上），"
                "全天总时长 3-8 小时；宁可少去一个地方，也不要压缩单个活动的停留时间。"),
    "normal": ("用户没有特别说明节奏：每个活动 1.5-2.5 小时，间隔 30-75 分钟，"
               "全天总时长 3-8.5 小时。"),
    "packed": ("用户偏好紧凑：每个活动 1-1.5 小时，间隔 15-45 分钟，全天总时长 5-10 小时；"
               "全天总时长 4-10 小时；确实顺路再多安排，但不要挤掉吃饭和交通时间。"),
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
