"""行程锚定：先定哪一类，其余两类围着它适配。

- sight：景点优先（默认）
- food：饮食优先（先定必吃餐馆，景点与住宿围着餐馆排）
- stay：住宿优先（先定酒店/区域，活动围着它排）
"""

from typing import Any

ANCHORS = ("sight", "food", "stay")

FOOD_WORDS = (
    "美食", "好吃", "吃货", "餐厅", "馆子", "必吃", "小吃", "米其林",
    "早茶", "夜市", "吃为主", "以吃为主", "吃遍", "觅食", "苍蝇馆",
)

STAY_WORDS = (
    "酒店", "民宿", "度假", "泡酒店", "躺平", "宅酒店", "住得好", "住得舒服",
    "住得讲究", "亲子酒店", "温泉酒店", "海景房",
)

SIGHT_WORDS = (
    "景点", "打卡", "拍照", "风景", "博物馆", "古迹", "徒步", "爬山", "看展",
)

NEGATIONS = ("不为了吃", "不是来吃", "吃无所谓", "随便吃", "不为住", "住无所谓")


def _score(text: str, words: tuple[str, ...]) -> int:
    return sum(text.count(word) for word in words)


def detect_anchor(*texts: Any) -> dict[str, str]:
    """按关键词判断锚定类型；没有明确信号时返回默认的 sight。

    返回 {"anchor": "sight|food|stay", "reason": "..."}，reason 供事件与排查使用。
    """
    text = " ".join(str(item or "") for item in texts)
    if any(word in text for word in NEGATIONS):
        return {"anchor": "sight", "reason": "negated"}

    food = _score(text, FOOD_WORDS)
    stay = _score(text, STAY_WORDS)
    sight = _score(text, SIGHT_WORDS)

    if food == 0 and stay == 0:
        return {"anchor": "sight", "reason": "default"}
    if food > stay and food >= sight:
        return {"anchor": "food", "reason": "food=%d stay=%d sight=%d" % (food, stay, sight)}
    if stay > food and stay >= sight:
        return {"anchor": "stay", "reason": "food=%d stay=%d sight=%d" % (food, stay, sight)}
    return {"anchor": "sight", "reason": "mixed"}
