"""模型日程的硬伤检查：规则定义在 `product/constraints/sets/runtime.json`。"""

from typing import Any

from product.constraints.runtime import check_plan


def check_day_plan(days: Any, constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回 {ok, violations}；violations 是给模型的可读反馈。"""
    return check_plan(days, constraints)
