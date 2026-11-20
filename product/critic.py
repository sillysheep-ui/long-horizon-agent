"""独立评审：只看成品日程与约束，给语义问题，不参与生成。"""

import json
import os
from typing import Any

from product.model_gateway import call_info, parse_json

CRITIC_ENABLED = os.getenv("CRITIC_ENABLED", "1") not in ("0", "false", "no")
ISSUE_KINDS = ("too_tight", "repetitive", "off_preference", "budget", "other")
_KIND_ALIASES = {"to_repetitive": "repetitive", "too_packed": "too_tight",
                 "off-preferences": "off_preference", "over_budget": "budget"}

CRITIC_SYSTEM_PROMPT = """你是独立的行程评审员，不负责生成，只挑问题。
输入是一份已经排好的行程和用户约束。请从这四类里找问题：
- too_tight：某天活动排得太满或间隔太短，实际走不完
- to_repetitive：同一天或相邻天出现重复类型/重复地点
- off_preference：明显偏离用户偏好（如带老人却全天爬山）
- budget：活动花费估算明显超出预算
只报能指出具体活动或日期的问题，不要泛泛而谈，不要因为“不够详细”而报错。
输出 JSON：{"score": 0-10, "verdict": "accept|revise", "issues": [{"day": 1, "kind": "...", "detail": "具体到哪个活动"}]}
没有问题时 issues 为空、verdict=accept。"""


def _normalize_issue(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("kind") or "other").strip()
    kind = _KIND_ALIASES.get(kind, kind)
    if kind not in ISSUE_KINDS:
        kind = "other"
    detail = str(raw.get("detail") or "").strip()
    if not detail:
        return None
    day = raw.get("day")
    try:
        day = int(day) if day is not None else None
    except (TypeError, ValueError):
        day = None
    return {"day": day, "kind": kind, "detail": detail[:80]}


def review(model: Any, days: Any, constraints: dict[str, Any] | None = None,
           calls: list | None = None) -> dict[str, Any]:
    """同步包装，内部用 asyncio.run 会在事件循环里失败；调用方用 review_async。"""
    raise RuntimeError("use review_async")


async def review_async(model: Any, days: Any, constraints: dict[str, Any] | None = None,
                       calls: list | None = None) -> dict[str, Any]:
    """返回 {score, verdict, issues, source}；失败时 fail-open。"""
    empty = {"score": None, "verdict": "skipped", "issues": [], "source": "skipped"}
    if not CRITIC_ENABLED or not days:
        return empty
    payload = {"days": days, "constraints": constraints or {}}
    try:
        response = await model.complete("行程评审", CRITIC_SYSTEM_PROMPT,
                                        json.dumps(payload, ensure_ascii=False), max_tokens=600)
        if calls is not None:
            calls.append(call_info(response))
        data = parse_json(response["content"])
        issues = [item for item in (_normalize_issue(raw) for raw in data.get("issues") or []) if item][:5]
        try:
            score = round(float(data.get("score")), 1)
        except (TypeError, ValueError):
            score = None
        verdict = "revise" if issues else "accept"
        return {"score": score, "verdict": verdict, "issues": issues, "source": "model"}
    except Exception:  # noqa: BLE001 - 评审失败不能阻断主流程
        return empty
