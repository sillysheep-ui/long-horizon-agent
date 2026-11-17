"""会话意图：新行程、改行程、问行程、闲聊。

先用确定性规则（可测、可解释），规则拿不准时再由调用方决定是否交给模型。
"""

import re
from typing import Any

from product.revision import parse_target_days

INTENTS = ("new_trip", "revise_trip", "ask", "chat")

NEW_TRIP_PATTERNS = (
    r"重新规划", r"重新安排", r"再来一次", r"重来", r"换个(地方|城市|目的地)",
    r"换一个(地方|城市|目的地)", r"改成去", r"改为去", r"计划去", r"打算去", r"想去",
)

REVISE_PATTERNS = (
    r"改成", r"改为", r"换成", r"换到", r"不要", r"别去", r"删掉", r"去掉", r"加上",
    r"增加", r"加(一个|个|上|点|些)", r"调整", r"第[一二三四五六七八九十\d]+天", r"预算", r"紧凑", r"轻松",
    r"半天", r"大半天", r"多(加|安排)", r"少(排|安排)",
)

ASK_PATTERNS = (
    r"[？?]", r"吗[。?？]?$", r"呢[。?？]?$", r"多少", r"几点", r"多远", r"多久",
    r"怎么走", r"为什么", r"哪(里|个|天)", r"是不是", r"有没有", r"推荐", r"介绍", r"是什么",
)

MODIFY_VERBS = ("改成", "改为", "换成", "换到", "不要", "别去", "删掉", "去掉", "加上", "增加", "加", "调整", "多", "少")


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def classify_intent(content: str, has_trip: bool) -> dict[str, Any]:
    """返回 {intent, reason, target_days}；没有历史行程时一律视为新行程。"""
    text = (content or "").strip()
    if not has_trip:
        return {"intent": "new_trip", "reason": "会话里还没有行程", "target_days": []}
    if not text:
        return {"intent": "chat", "reason": "空消息", "target_days": []}
    days = parse_target_days(text, total_days=30) or []
    if _matches(NEW_TRIP_PATTERNS, text) and not _matches(MODIFY_VERBS, text):
        return {"intent": "new_trip", "reason": "出现换目的地/重新规划的说法", "target_days": []}
    if _matches(ASK_PATTERNS, text) and not _matches(MODIFY_VERBS, text):
        return {"intent": "ask", "reason": "在问行程本身的情况", "target_days": days}
    if days and _matches(MODIFY_VERBS, text):
        return {"intent": "revise_trip", "reason": "点名了要改的日期", "target_days": days}
    if _matches(REVISE_PATTERNS, text):
        return {"intent": "revise_trip", "reason": "出现调整行程的说法", "target_days": days}
    return {"intent": "chat", "reason": "既不像改行程也不像提问", "target_days": []}


def describe_trip(result: dict[str, Any] | None, target_days: list[int] | None = None) -> str:
    """用已有行程拼一段确定性回答，不调用模型。"""
    if not result:
        return "还没有可以回答的行程。先告诉我从哪出发、玩几天、喜欢什么吧。"
    days = result.get("days") or []
    if not days:
        question = result.get("question") or result.get("summary") or "还缺一些关键信息，能再补充一下吗？"
        return str(question)
    wanted = set(target_days or [])
    lines = []
    for day in days:
        number = int(day.get("day") or 0)
        if wanted and number not in wanted:
            continue
        activities = day.get("activities") or []
        parts = [f"{item.get('start', '')} {item.get('title', '')}".strip() for item in activities]
        lines.append(f"第{number}天：" + "；".join(parts))
    if not lines:
        return "你问的那天不在当前行程里。"
    return "\n".join(lines) + "\n\n想改哪天直接说，比如“第二天换成博物馆”。"
