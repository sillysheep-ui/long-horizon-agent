"""跨槽位确定性核验：规则定义在 `product/constraints/sets/crosscheck.json`。

所有检查都是纯函数，数据不足时返回 None（unknown），不把“算不出来”当成失败。
"""

from typing import Any

from product.constraints.engine import build_view, evaluate
from product.constraints.spec import SET_DIR, load_set

CROSSCHECK_SET = load_set(SET_DIR / "crosscheck.json")
MAX_MEAL_KM = 1.5
MAX_STAY_TRANSFER_MINUTES = 30
MEAL_MINUTES = 60


def _finding(days: list[dict[str, Any]], constraints: dict[str, Any], constraint_id: str):
    view = build_view({"days": days, "constraints": constraints or {}})
    for finding in evaluate(view, CROSSCHECK_SET):
        if finding.id == constraint_id:
            return finding
    raise KeyError(constraint_id)


def meal_near(day: dict[str, Any]) -> dict[str, Any] | None:
    """当天最远的一餐离最近活动多远；没有坐标就不判。"""
    finding = _finding([day], {}, "meal_near")
    if not finding.meta.get("known"):
        return None
    return {"ok": finding.ok, "max_km": finding.meta["max_km"]}


def meal_time(day: dict[str, Any]) -> dict[str, Any] | None:
    """用餐时间要落在活动之间，不能和活动重叠。"""
    finding = _finding([day], {}, "meal_time")
    if not finding.meta.get("known"):
        return None
    return {"ok": finding.ok, "bad": finding.meta.get("bad") or []}


def budget_fits(days: list[dict[str, Any]], constraints: dict[str, Any]) -> bool | None:
    """把行程里写明的票价/人均加起来，和用户预算比；数字不全就不判。"""
    finding = _finding(days, constraints, "budget_fits")
    if not finding.meta.get("known"):
        return None
    return finding.ok


def cross_checks(days: list[dict[str, Any]], constraints: dict[str, Any]) -> dict[str, bool]:
    """汇总所有可判定的跨槽位检查。"""
    checks: dict[str, bool] = {}
    near = [meal_near(day) for day in days]
    known_near = [item for item in near if item]
    if known_near:
        checks["meal_near"] = all(item["ok"] for item in known_near)
    timing = [meal_time(day) for day in days]
    known_timing = [item for item in timing if item]
    if known_timing:
        checks["meal_time"] = all(item["ok"] for item in known_timing)
    fits = budget_fits(days, constraints)
    if fits is not None:
        checks["budget_fits"] = fits
    return checks
