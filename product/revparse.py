"""把修改句解析成结构化“修改单”：模型给候选，规则做校验。

模型只负责理解自然语言（想改哪天、想加什么属性、有没有整体条件变化），
是否合法、能不能落到当前行程上，由这里用确定性规则判断。
"""

from __future__ import annotations

import json
from typing import Any

from product.destinations import known_destination
from product.model_gateway import call_info, parse_json

ACTIONS = ("modify", "add", "remove", "move")
ATTRIBUTES = ("quiet", "kid_friendly", "indoor", "outdoor", "nature",
              "museum", "food", "photo", "slow", "compact")
PACES = ("relaxed", "normal", "packed")
ATTRIBUTE_LABELS = {
    "quiet": "安静", "kid_friendly": "适合孩子", "indoor": "室内", "outdoor": "户外",
    "nature": "自然风光", "museum": "博物馆", "food": "美食", "photo": "拍照",
    "slow": "慢节奏", "compact": "紧凑",
}

REVISION_SYSTEM_PROMPT = """你是行程修改解析器。把用户这一句修改要求解析成 JSON。

只输出一个 JSON 对象，字段如下：
{
  "action": "modify|add|remove|move",
  "target_days": [整数日期，1 开始；没点名就给空数组],
  "attributes": ["从给定列表中选择的属性；没提就给空数组"],
  "changes": {
    "budget": 数字或 null,
    "days": 整数或 null,
    "city": "城市名或 null",
    "pace": "relaxed|normal|packed 或 null"
  },
  "confidence": 0 到 1,
  "reason": "不超过 20 字的理由"
}

可用属性：%(attributes)s。
规则：
1) 只填用户明确提到的内容，不要推测；
2) 用户说“别/不要/不想”时是 remove，不是 add；
3) 目标日期超出当前天数就留空，交给后续追问；
4) 拿不准时 confidence 给 0.5 以下，不要编。
""" % {"attributes": "、".join(ATTRIBUTES)}


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def empty_revision() -> dict[str, Any]:
    return {"action": "modify", "target_days": [], "attributes": [], "changes": {},
            "confidence": 0.0, "reason": ""}


def validate_revision(parsed: Any, days_count: int, constraints: dict[str, Any]) -> dict[str, Any]:
    """丢弃模型编造或越界的内容，只保留能落到当前行程上的部分。"""
    result = empty_revision()
    if not isinstance(parsed, dict):
        return result

    action = str(parsed.get("action") or "").strip()
    result["action"] = action if action in ACTIONS else "modify"

    days: list[int] = []
    for raw in parsed.get("target_days") or []:
        day = _as_int(raw)
        if day is not None and 1 <= day <= max(1, int(days_count)) and day not in days:
            days.append(day)
    result["target_days"] = sorted(days)

    attributes: list[str] = []
    for raw in parsed.get("attributes") or []:
        name = str(raw or "").strip()
        if name in ATTRIBUTES and name not in attributes:
            attributes.append(name)
    result["attributes"] = attributes[:4]

    changes: dict[str, Any] = {}
    raw_changes = parsed.get("changes") if isinstance(parsed.get("changes"), dict) else {}
    budget = _as_int(raw_changes.get("budget"))
    if budget is not None and 100 <= budget <= 200000 and budget != constraints.get("budget"):
        changes["budget"] = budget
    total_days = _as_int(raw_changes.get("days"))
    if total_days is not None and 1 <= total_days <= 7 and total_days != constraints.get("days"):
        changes["days"] = total_days
    city = str(raw_changes.get("city") or "").strip()
    if city and known_destination(city) and city not in (constraints.get("destinations") or []):
        changes["destinations"] = [city]
    pace = str(raw_changes.get("pace") or "").strip()
    if pace in PACES and pace != constraints.get("pace"):
        changes["pace"] = pace
    result["changes"] = changes

    try:
        confidence = float(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    result["confidence"] = max(0.0, min(1.0, confidence))
    result["reason"] = str(parsed.get("reason") or "")[:40]

    if result["confidence"] < 0.6:
        fallback = empty_revision()
        fallback["reason"] = result["reason"]
        return fallback
    return result


async def parse_revision(model: Any, text: str, days_count: int,
                         constraints: dict[str, Any],
                         calls: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """调用模型解析修改句；失败或低置信度时返回空修改单。"""
    payload = {
        "revision": text,
        "days": int(days_count),
        "current": {
            "cities": constraints.get("destinations") or [],
            "budget": constraints.get("budget"),
            "pace": constraints.get("pace"),
            "anchor": constraints.get("anchor"),
        },
    }
    try:
        response = await model.complete("修改解析", REVISION_SYSTEM_PROMPT,
                                        json.dumps(payload, ensure_ascii=False), max_tokens=400)
    except Exception:  # noqa: BLE001 - 解析失败不应影响主流程
        return empty_revision()
    if calls is not None:
        calls.append(call_info(response))
    return validate_revision(parse_json(response["content"]), days_count, constraints)
