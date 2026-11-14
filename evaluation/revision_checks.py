"""多轮修改评测的确定性检查。

输入是同一行程修改前、修改后的两次结果，检查“只改了该改的那天、其余保持稳定”。
不调模型，纯函数，方便单测与线上复用。
"""

from typing import Any, Dict, List, Optional


def _days(result: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    days: Dict[int, Dict[str, Any]] = {}
    for day in result.get("days") or []:
        try:
            days[int(day.get("day"))] = day
        except (TypeError, ValueError):
            continue
    return days


def _titles(day: Optional[Dict[str, Any]]) -> List[str]:
    return [str(activity.get("title") or "") for activity in (day or {}).get("activities") or []]


def _all_titles(result: Dict[str, Any]) -> List[str]:
    return [title for day in _days(result).values() for title in _titles(day)]


def _time_order_ok(result: Dict[str, Any]) -> bool:
    for day in _days(result).values():
        starts = [str(activity.get("start") or "99:99") for activity in day.get("activities") or []]
        if starts != sorted(starts):
            return False
    return True


def _evidence_ok(result: Dict[str, Any]) -> bool:
    status = {str(e.get("id")): e.get("status") for e in result.get("evidence") or []}
    for day in _days(result).values():
        for activity in day.get("activities") or []:
            if status.get(str(activity.get("evidence_id"))) != "ok":
                return False
    return True


def check_revision(case: Dict[str, Any], before: Dict[str, Any],
                   after: Dict[str, Any]) -> Dict[str, Optional[bool]]:
    """返回 {检查名: 通过与否}，None 表示不适用。"""
    checks: Dict[str, Optional[bool]] = {}
    before_days = _days(before)
    after_days = _days(after)
    targets = [int(day) for day in case.get("target_days") or []]
    expect = case.get("expect", "targeted")

    if expect == "clarify":
        return {
            "clarified": after.get("status") == "needs_clarification",
            "applied": after.get("revision_applied") is None,
            "days_empty": not after.get("days"),
        }

    checks["complete"] = after.get("status") == "complete"
    checks["days_present"] = bool(after_days) and all(day in after_days for day in range(1, len(after_days) + 1))
    checks["day_count"] = len(before_days) == len(after_days) and bool(after_days)

    if expect == "targeted":
        checks["applied"] = after.get("revision_applied") is True
        keep_days = [day for day in before_days if day not in targets]
        checks["kept_days"] = all(
            _titles(before_days[day]) == _titles(after_days.get(day)) for day in keep_days
        )
        checks["target_changed"] = all(
            bool(_titles(after_days.get(day))) and _titles(after_days.get(day)) != _titles(before_days.get(day))
            for day in targets
        )
    else:
        # 没有点名日期（或日期越界）：允许整单重排，不应误标“已定向修改”。
        checks["applied"] = after.get("revision_applied") is None

    titles = _all_titles(after)
    checks["unique_titles"] = len(titles) == len(set(titles))
    checks["evidence_ok"] = _evidence_ok(after)
    checks["time_order"] = _time_order_ok(after)
    return checks


def summarize_revision(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    names: List[str] = []
    for record in records:
        for name in record.get("checks") or {}:
            if name not in names:
                names.append(name)
    pass_rate: Dict[str, float] = {}
    for name in names:
        values = [record["checks"][name] for record in records if name in (record.get("checks") or {})]
        values = [value for value in values if value is not None]
        pass_rate[name] = round(sum(1 for value in values if value) / len(values), 3) if values else 0.0
    applicable = [record for record in records if record.get("checks")]
    all_passed = sum(1 for record in applicable if all(v for v in record["checks"].values() if v is not None))
    changed = [record for record in records if record.get("changed_days")]
    return {
        "cases": len(records),
        "errors": sum(1 for record in records if record.get("error")),
        "all_passed": all_passed,
        "all_passed_rate": round(all_passed / len(applicable), 3) if applicable else 0.0,
        "pass_rate": pass_rate,
        "targeted_with_change": f"{len(changed)}/{len(applicable)}",
        "first_elapsed_mean": round(sum(r.get("first_elapsed", 0) for r in records) / max(1, len(records)), 2),
        "revision_elapsed_mean": round(sum(r.get("elapsed", 0) for r in records) / max(1, len(records)), 2),
    }
