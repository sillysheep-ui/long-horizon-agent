"""会话意图：新行程、改行程、问行程、闲聊。

先用确定性规则（可测、可解释），规则拿不准时再由调用方决定是否交给模型。
"""

import re
from typing import Any

from product.revision import parse_target_days

INTENTS = ("new_trip", "revise_trip", "ask", "chat")

NEW_TRIP_PATTERNS = (
    r"重新规划", r"重新安排", r"再来一次", r"重来", r"换个(地方|城市|目的地)",
    r"换一个(地方|城市|目的地)", r"换成去", r"改成去", r"改为去", r"目的地换成", r"计划去", r"打算去", r"想去",
    r"帮我(做|规划|安排).*(行程|旅行)", r"再规划一个", r"下一趟", r"重新搞一份", r"来一份.*行程", r"重新来一份",
)

REVISE_PATTERNS = (
    r"改成", r"改为", r"换成", r"换到", r"不要", r"别去", r"删掉", r"去掉", r"加上",
    r"增加", r"加(一个|个|上|点|些)", r"调整", r"第[一二三四五六七八九十\d]+天", r"预算", r"紧凑", r"轻松",
    r"放慢", r"慢一点", r"快一点", r"加快",
    r"半天", r"大半天", r"多(加|安排)", r"少(排|安排)",
)

ASK_PATTERNS = (
    r"[？?]", r"吗[。?？]?$", r"呢[。?？]?$", r"多少", r"几点", r"多远", r"多久",
    r"怎么走", r"为什么", r"哪(里|个|天)", r"是不是", r"有没有", r"推荐", r"介绍", r"是什么",
)

CANCEL_PATTERNS = (
    r"^算了", r"不去了", r"不想去了", r"先不去", r"取消(这次|行程|安排)", r"退掉", r"不用了",
    r"^取消(吧|了|掉)?[!！。.\s]*$",
)

UNDO_PATTERNS = (
    r"退回上一版", r"恢复上一版", r"回到上一版", r"撤销", r"回退", r"上一个版本", r"上一版",
)

CHAT_PATTERNS = (
    r"^(你好|您好|在吗|你在吗|谢谢|好的|好|收到|辛苦了|哈哈+|嗯+|随便|都行|看着办|可以|不错|厉害|牛)\W*$",
)

MODIFY_VERBS = ("改成", "改为", "换成", "换到", "不要", "别去", "删掉", "去掉", "加上", "增加", "加", "调整")


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


QUESTION_PATTERN = re.compile(
    r"(吗|呢|多久|多远|远不远|近不近|怎么去|怎么走|多少钱|贵不贵|累不累|值得|好玩吗|几点|开门|关门|营业)")

FEASIBILITY_PATTERN = re.compile(
    r"(太赶|赶不赶|会不会赶|来不及|时间够|够不够|太紧|会不会太紧|强度|轻松吗|会不会累|赶得及)")


def classify_intent(content: str, has_trip: bool) -> dict[str, Any]:
    """返回 {intent, reason, target_days}；没有历史行程时一律视为新行程。"""
    text = (content or "").strip()
    if not text:
        return {"intent": "chat", "reason": "空消息", "target_days": [], "scope": "global", "action": "modify"}
    if _matches(CHAT_PATTERNS, text):
        return {"intent": "chat", "reason": "寒暄或应和", "target_days": [],
                "scope": "global", "action": "modify"}
    if not has_trip:
        if _matches(ASK_PATTERNS, text) and not _matches(MODIFY_VERBS, text):
            return {"intent": "ask", "reason": "在问行程本身的情况", "target_days": [],
                    "scope": "global", "action": "modify"}
        return {"intent": "new_trip", "reason": "会话里还没有行程", "target_days": [],
                "scope": "global", "action": "modify"}
    days = parse_target_days(text, total_days=30) or []
    if _matches(UNDO_PATTERNS, text):
        return {"intent": "revise_trip", "reason": "要退回上一版", "target_days": [],
                "scope": "global", "action": "undo"}
    if _matches(CANCEL_PATTERNS, text):
        return {"intent": "revise_trip", "reason": "要取消行程", "target_days": [],
                "scope": "global", "action": "cancel"}
    if _matches(CHAT_PATTERNS, text):
        return {"intent": "chat", "reason": "寒暄或应和", "target_days": [],
                "scope": "global", "action": "modify"}
    if re.search(r"(换成去|改成去|改为去|目的地换成)", text):
        return {"intent": "new_trip", "reason": "换了一个目的地", "target_days": [],
                "scope": "global", "action": "modify"}
    if _matches(NEW_TRIP_PATTERNS, text) and not _matches(MODIFY_VERBS, text):
        return {"intent": "new_trip", "reason": "出现换目的地/重新规划的说法", "target_days": [],
                "scope": "global", "action": "modify"}
    if _matches(ASK_PATTERNS, text) and not _matches(MODIFY_VERBS, text):
        return {"intent": "ask", "reason": "在问行程本身的情况", "target_days": days,
                "scope": "targeted" if days else "global", "action": "modify"}
    if (QUESTION_PATTERN.search(text) or FEASIBILITY_PATTERN.search(text)) \
            and not _matches(MODIFY_VERBS, text) \
            and not re.search(r"(换成|改成|改为|加|去掉|删|取消|撤销)", text):
        return {"intent": "ask", "reason": "在问行程细节", "target_days": days,
                "scope": "targeted" if days else "global", "action": "modify"}
    if days and _matches(MODIFY_VERBS, text):
        return {"intent": "revise_trip", "reason": "点名了要改的日期", "target_days": days,
                "scope": "targeted", "action": "modify"}
    if _matches(REVISE_PATTERNS, text):
        return {"intent": "revise_trip", "reason": "出现调整行程的说法", "target_days": days,
                "scope": "targeted" if days else "global", "action": "modify"}
    return {"intent": "chat", "reason": "既不像改行程也不像提问", "target_days": [],
            "scope": "global", "action": "modify"}


def _day_minutes(activities: list[dict[str, Any]]) -> tuple[int, int]:
    """返回（活动总分钟、交通总分钟）。"""
    total = transit = 0
    for item in activities:
        duration = str(item.get("duration") or "")
        match = re.search(r"([\d.]+)\s*(?:小时|h)", duration)
        if match:
            total += int(float(match.group(1)) * 60)
        else:
            match = re.search(r"(\d+)\s*分钟", duration)
            if match:
                total += int(match.group(1))
        transport = str(item.get("transport") or "")
        match = re.search(r"(\d+)\s*分钟", transport)
        if match:
            transit += int(match.group(1))
    return total, transit


def _feasibility_answer(day: dict[str, Any], hours: float) -> str:
    """按当天活动量给一句确定性判断。"""
    activities = day.get("activities") or []
    number = int(day.get("day") or 0)
    if not activities:
        return f"第{number}天目前没有安排，不会赶。"
    total, transit = _day_minutes(activities)
    starts = sorted(str(item.get("start") or "09:00") for item in activities)
    ratio = total / (hours * 60) if hours else 0
    if ratio <= 0.45:
        level = "比较宽松"
    elif ratio <= 0.72:
        level = "节奏正常"
    else:
        level = "偏紧"
    transit_text = f"，路上约 {transit} 分钟" if transit else ""
    lines = [f"第{number}天现在 {len(activities)} 个活动，{starts[0]} 开始，活动约 "
             f"{total / 60:.1f} 小时{transit_text}；按当天 {hours:g} 小时可用时间算，{level}。"]
    if level == "偏紧":
        lines.append("想松一点可以去掉一个点，或者把其中一个挪到别天。")
    elif len(activities) >= 5:
        lines.append("数量偏多，虽然时间排得下，实际会有点赶，建议减到 4 个以内。")
    return "".join(lines)


def describe_trip(result: dict[str, Any] | None, target_days: list[int] | None = None,
                  question: str | None = None) -> str:
    """用已有行程拼一段确定性回答，不调用模型。"""
    if not result:
        return "还没有可以回答的行程。先告诉我从哪出发、玩几天、喜欢什么吧。"
    days = result.get("days") or []
    if not days:
        question = result.get("question") or result.get("summary") or "还缺一些关键信息，能再补充一下吗？"
        return str(question)
    wanted = set(target_days or [])
    if question and FEASIBILITY_PATTERN.search(question):
        hours_map = {int(item.get("day") or 0): float(item.get("hours") or 10)
                     for item in (result.get("constraints") or {}).get("day_windows") or []
                     if item.get("day")}
        picked = [day for day in days if not wanted or int(day.get("day") or 0) in wanted]
        return "\n".join(_feasibility_answer(day, hours_map.get(int(day.get("day") or 0), 10))
                          for day in picked)
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


INTENT_SYSTEM_PROMPT = """你是旅行规划助手的意图分类器。用户已经有一份行程，判断这句话属于哪一类：
- new_trip：要换目的地/重新规划一份新行程
- revise_trip：要改现有行程（换地点、加减活动、调预算、调节奏、指定某天）
- ask：问现有行程里的信息（时间、地点、预算、交通），不需要改
- chat：寒暄、感谢、与行程无关的话
- unclear：信息不足，无法判断

只输出 JSON：{"intent": "...", "action": "modify|cancel|undo", "confidence": 0.0-1.0,
"target_days": [1,2], "reason": "一句话"}
- action=cancel：用户要取消/不去了；action=undo：用户要退回上一版；其余 modify。
- 需要改行程时，scope 由 target_days 决定：点了具体日期就是 targeted，否则 global。
target_days 只放用户明确点到的日期，没有就给空数组。"""


async def classify_with_model(model: Any, content: str, has_trip: bool,
                              prior_status: str | None = None) -> dict[str, Any]:
    """模型兜底分类；失败时退回规则结果。"""
    context = "用户当前有一份行程。" if has_trip else "用户还没有行程。"
    if prior_status == "needs_clarification":
        context += "上一轮系统刚问过用户缺少的信息。"
    try:
        response = await model.complete("意图分类", INTENT_SYSTEM_PROMPT,
                                        context + "\n用户说：" + content, max_tokens=150)
        from product.model_gateway import parse_json

        data = parse_json(response["content"])
        intent = str(data.get("intent") or "").strip()
        if intent not in INTENTS + ("unclear",):
            raise ValueError("未知意图")
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        days = [int(day) for day in data.get("target_days") or [] if str(day).isdigit()]
        action = str(data.get("action") or "modify").strip()
        if action not in ("modify", "cancel", "undo"):
            action = "modify"
        return {"intent": intent, "confidence": round(confidence, 2), "target_days": days,
                "scope": "targeted" if days else "global", "action": action,
                "reason": "模型判断", "source": "model"}
    except Exception:  # noqa: BLE001 - 分类失败不能阻断会话
        fallback = classify_intent(content, has_trip)
        fallback["source"] = "rules_fallback"
        return fallback


_FAST_CHAT = re.compile(r"^(你好|您好|在吗|谢谢|多谢|好的|好|收到|辛苦了|再见)[!！。.，,\s]*$")


async def route_intent(model: Any, content: str, has_trip: bool,
                       prior_status: str | None = None) -> dict[str, Any]:
    """模型优先分类；取消/撤销与纯寒暄走规则，规则作为兜底与校验。"""
    text = (content or "").strip()
    ruled = classify_intent(text, has_trip)
    ruled["source"] = "rules"
    if not text or _FAST_CHAT.match(text):
        return ruled
    if ruled.get("action") in ("cancel", "undo"):
        return ruled
    modeled = await classify_with_model(model, text, has_trip, prior_status)
    if modeled.get("source") == "rules_fallback":
        ruled["source"] = "rules_fallback"
        return ruled
    if float(modeled.get("confidence") or 0) < 0.6:
        ruled["source"] = "rules_low_confidence"
        return ruled
    if modeled.get("intent") == "unclear" and ruled["intent"] == "chat":
        return ruled
    return modeled
