"""约束层的数据模型：Constraint / Finding / ItineraryView。

这一层只描述数据，不做求值；求值在 engine.py，算子实现在 ops.py。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CATEGORIES = ("hard", "commonsense", "experience")
SEVERITIES = ("hard", "soft")

_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(小时|个小时|h|hour|hours|分钟|min|minutes?)", re.IGNORECASE)


@dataclass(frozen=True)
class Finding:
    """一条约束的判定结果，报告与运行时都消费这个结构。"""

    id: str
    ok: bool
    detail: str = ""
    expected: str = ""
    actual: str = ""
    category: str = "hard"
    severity: str = "hard"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ok": self.ok,
            "detail": self.detail,
            "expected": self.expected,
            "actual": self.actual,
            "category": self.category,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class Constraint:
    id: str
    category: str
    severity: str
    type: str
    params: dict[str, Any] = field(default_factory=dict)
    select: str = ""


@dataclass(frozen=True)
class ConstraintSet:
    version: str
    constraints: tuple[Constraint, ...]

    def by_id(self, constraint_id: str) -> Constraint | None:
        for item in self.constraints:
            if item.id == constraint_id:
                return item
        return None


@dataclass
class ItineraryView:
    """一次行程 + 证据 + 需求，算子的统一输入。"""

    days: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    case: dict[str, Any] = field(default_factory=dict)
    status: str = ""

    def activities(self, day: dict[str, Any]) -> list[dict[str, Any]]:
        items = day.get("activities") if isinstance(day, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]

    def evidence_item(self, title: str) -> dict[str, Any] | None:
        target = squash(title)
        if not target:
            return None
        for evidence in self.evidence:
            for item in evidence.get("items") or []:
                name = squash(str(item.get("name") or ""))
                if not name:
                    continue
                if name == target:
                    return item
        for evidence in self.evidence:
            for item in evidence.get("items") or []:
                name = squash(str(item.get("name") or ""))
                if name and (name in target or target in name):
                    return item
        return None


def squash(text: str) -> str:
    """去掉空白与常见标点，便于中英文混排的包含判断。"""
    return re.sub(r"[\s\W_]+", "", text or "", flags=re.UNICODE)


def parse_hhmm(value: str) -> int | None:
    """'09:30' -> 570；非法返回 None。"""
    match = _TIME.match(str(value or "").strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def format_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def duration_minutes(value: Any) -> int | None:
    """'2.5 小时' / '90 分钟' -> 分钟；缺失或非法返回 None。"""
    if isinstance(value, int | float) and value > 0:
        return int(round(float(value) * 60))
    match = _DURATION.search(str(value or ""))
    if not match:
        return None
    amount = float(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith(("小时", "个小时", "h", "hour")):
        return int(round(amount * 60))
    return int(round(amount))


def activity_window(activity: dict[str, Any], default_minutes: int = 60) -> tuple[int, int] | None:
    """活动占用区间（分钟），缺开始时间返回 None。"""
    start = parse_hhmm(activity.get("start"))
    if start is None:
        return None
    minutes = duration_minutes(activity.get("duration"))
    if minutes is None:
        minutes = default_minutes
    return start, start + minutes
