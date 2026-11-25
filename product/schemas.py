"""三个阶段模型输出的结构契约。

模型只保证“大意”，这一层保证“形状”：字段缺失、类型不对、列表里混进对象，
都在进入清洗前归一成安全值，并把原始问题记进 trace，方便定位是哪一步跑偏。
语义（地点是否存在、时间是否冲突）仍由 ``product.plan_rules`` 与约束引擎负责。
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

MAX_ITEMS = 24
MAX_ISSUES = 8


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return ""


def _text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return []
    items = [_text(item) for item in value]
    return [item for item in items if item][:MAX_ITEMS]


def _count(value: Any) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        found = re.search(r"\d+", value)
        return int(found.group()) if found else 0
    return 0


def _optional_count(value: Any) -> int | None:
    if value is None:
        return None
    return _count(value) or None


def _day_span(value: Any) -> str:
    return "half" if _text(value).lower() in {"half", "半天"} else "full"


Text = Annotated[str, BeforeValidator(_text)]
TextList = Annotated[list[str], BeforeValidator(_text_list)]
Count = Annotated[int, BeforeValidator(_count)]
OptionalCount = Annotated[int | None, BeforeValidator(_optional_count)]
DaySpan = Annotated[str, BeforeValidator(_day_span)]


class _Payload(BaseModel):
    """未知字段保留，避免把模型多给的上下文悄悄丢掉。"""

    model_config = ConfigDict(extra="allow")


class IntakePayload(_Payload):
    departure_city: Text = ""
    destinations: TextList = Field(default_factory=list)
    days: OptionalCount = None
    day_span: DaySpan = "full"
    budget: Text = ""
    named_places: TextList = Field(default_factory=list)
    preferences: TextList = Field(default_factory=list)
    people: Count = 0
    rooms: Count = 0
    room_type: Text = ""
    transport: Text = ""
    missing_required: TextList = Field(default_factory=list)
    conflicts: TextList = Field(default_factory=list)


class PlanDay(_Payload):
    day: Count = 0
    city: Text = ""
    poi_queries: TextList = Field(default_factory=list)


class PlanPayload(_Payload):
    days: list[PlanDay] = Field(default_factory=list)

    @field_validator("days", mode="before")
    @classmethod
    def _drop_non_dicts(cls, value: Any) -> list[Any]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class Activity(_Payload):
    title: Text = ""
    display_title: Text = ""
    start: Text = ""
    duration: Text = ""
    description: Text = ""
    transport: Text = ""
    cost: Text = ""
    evidence_id: Text = ""


class Meal(_Payload):
    type: Text = ""
    title: Text = ""
    start: Text = ""
    evidence_id: Text = ""


class AnswerDay(_Payload):
    day: Count = 0
    city: Text = ""
    activities: list[Activity] = Field(default_factory=list)
    meals: list[Meal] = Field(default_factory=list)
    lodging_area: Text = ""

    @field_validator("activities", "meals", mode="before")
    @classmethod
    def _drop_non_dicts(cls, value: Any) -> list[Any]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


class AnswerPayload(_Payload):
    summary: Text = ""
    days: list[AnswerDay] = Field(default_factory=list)
    notes: TextList = Field(default_factory=list)

    @field_validator("days", mode="before")
    @classmethod
    def _drop_non_dicts(cls, value: Any) -> list[Any]:
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "布尔值"
    if isinstance(value, int | float):
        return "数字"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, dict):
        return "对象"
    if value is None:
        return "空值"
    return type(value).__name__


_ALLOWED = {
    "text": {"字符串", "数字"},
    "count": {"数字", "字符串"},
    "list": {"数组"},
}
_WANT = {"text": "字符串", "count": "整数", "list": "数组"}


def _diff(spec: dict[str, str], payload: dict[str, Any], prefix: str = "") -> list[str]:
    issues = []
    for key, wanted in spec.items():
        if key not in payload:
            continue
        got = _kind(payload[key])
        if got not in _ALLOWED[wanted]:
            issues.append("%s%s 应为%s，实际是%s" % (prefix, key, _WANT[wanted], got))
    return issues


_INTAKE_SPEC = {
    "departure_city": "text",
    "destinations": "list",
    "days": "count",
    "day_span": "text",
    "budget": "text",
    "named_places": "list",
    "preferences": "list",
    "people": "count",
    "rooms": "count",
    "room_type": "text",
    "transport": "text",
    "missing_required": "list",
    "conflicts": "list",
}
_PLAN_SPEC = {"days": "list"}
_ANSWER_SPEC = {"summary": "text", "days": "list", "notes": "list"}


def _plan_issues(payload: dict[str, Any]) -> list[str]:
    issues = _diff(_PLAN_SPEC, payload)
    for index, day in enumerate(payload.get("days") or []):
        if not isinstance(day, dict):
            issues.append("days[%d] 应为对象，实际是%s" % (index, _kind(day)))
        elif "poi_queries" in day and _kind(day["poi_queries"]) not in _ALLOWED["list"]:
            issues.append("days[%d].poi_queries 应为数组，实际是%s" % (index, _kind(day["poi_queries"])))
    return issues


def _answer_issues(payload: dict[str, Any]) -> list[str]:
    issues = _diff(_ANSWER_SPEC, payload)
    for index, day in enumerate(payload.get("days") or []):
        if not isinstance(day, dict):
            issues.append("days[%d] 应为对象，实际是%s" % (index, _kind(day)))
            continue
        for key in ("activities", "meals"):
            if key in day and _kind(day[key]) not in _ALLOWED["list"]:
                issues.append("days[%d].%s 应为数组，实际是%s" % (index, key, _kind(day[key])))
    return issues


def _parse(model: type[BaseModel], payload: Any, fallback: dict[str, Any],
           issues_of) -> tuple[dict[str, Any], list[str]]:
    if not isinstance(payload, dict):
        return dict(fallback), ["顶层应为对象，实际是%s" % _kind(payload)]
    issues = issues_of(payload)
    try:
        data = model.model_validate(payload).model_dump()
    except ValidationError as exc:
        first = "; ".join("%s: %s" % (".".join(str(part) for part in error["loc"]), error["msg"])
                          for error in exc.errors()[:3])
        return dict(fallback), issues + ["结构校验失败：%s" % first]
    return data, issues[:MAX_ISSUES]


def parse_intake(payload: Any) -> tuple[dict[str, Any], list[str]]:
    return _parse(IntakePayload, payload, {}, lambda data: _diff(_INTAKE_SPEC, data))


def parse_plan(payload: Any) -> tuple[dict[str, Any], list[str]]:
    return _parse(PlanPayload, payload, {"days": []}, _plan_issues)


def parse_answer(payload: Any) -> tuple[dict[str, Any], list[str]]:
    return _parse(AnswerPayload, payload, {"days": []}, _answer_issues)
