"""兼容入口：实现已拆分到 prompts / tool_adapters / postprocess / trace / workflow。

保留这个模块是为了不破坏 `from product.orchestrator import AgentWorkflow` 等既有调用。
"""

from product.postprocess import (  # noqa: F401
    _as_list,
    _clean_activities,
    _declares_duration,
    _duration_minutes,
    _finalize_days,
    _fit_schedule,
    _hhmm,
    _minutes_of,
    _norm_title,
    budget_conflict,
    chinese_number,
    cities_fit_days,
    match_activity,
)
from product.prompts import build_answer_system  # noqa: F401
from product.tool_adapters import ResearchTools, _rank_search_items  # noqa: F401
from product.trace import (  # noqa: F401
    _critical_path_ms,
    _fabricated_costs,
    _span,
    _time_order_ok,
    _tool_spans,
)
from product.workflow import AgentWorkflow  # noqa: F401

__all__ = ["AgentWorkflow", "ResearchTools"]
