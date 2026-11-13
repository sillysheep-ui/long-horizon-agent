"""产品行程的确定性检查：不依赖裁判模型，先卡结构与可追溯性。"""

from typing import Any, Dict, List

NOISE_TOKENS = ("攻略", "门票", "地址", "图片", "开放时间", "营业时间", "【", "】", "｜", "点评", "简介")
TIME_ORDER_TOLERANCE = 0  # 只比较字符串顺序，HH:MM 定长可直接比


def _days(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    days = result.get("days")
    return [d for d in days if isinstance(d, dict)] if isinstance(days, list) else []


def _activities(day: Dict[str, Any]) -> List[Dict[str, Any]]:
    items = day.get("activities")
    return [a for a in items if isinstance(a, dict)] if isinstance(items, list) else []


def _evidence_map(result: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {e.get("id"): e for e in result.get("evidence") or [] if isinstance(e, dict)}


def check_result(case: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, bool]:
    """返回逐项检查结果；键名会直接进报告。"""
    if case.get("expect") == "needs_clarification":
        return {"clarification": result.get("status") == "needs_clarification"}
    days = _days(result)
    activities = [a for d in days for a in _activities(d)]
    titles = [str(a.get("title") or "") for a in activities]
    evidence = _evidence_map(result)
    expected_days = case.get("days")
    return {
        "complete": result.get("status") == "complete",
        "day_count": bool(days) and (expected_days is None or len(days) == expected_days),
        "density": bool(days) and all(2 <= len(_activities(d)) <= 3 for d in days),
        "unique_titles": len(titles) == len(set(titles)),
        "title_clean": bool(titles) and all(
            len(title) <= 24 and not any(token in title for token in NOISE_TOKENS) for title in titles),
        "time_order": bool(days) and all(
            [str(a.get("start")) for a in _activities(d)] == sorted(str(a.get("start")) for a in _activities(d))
            for d in days),
        "fields_filled": bool(activities) and all(
            a.get("start") and a.get("duration") and a.get("transport") and a.get("cost") for a in activities),
        "evidence_ok": bool(activities) and all(
            (evidence.get(a.get("evidence_id")) or {}).get("status") == "ok" for a in activities),
    }


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把逐条记录汇总成通过率、平均分与延迟。"""
    check_names = sorted({name for r in records for name in r["checks"]})
    passed = {name: sum(1 for r in records if r["checks"].get(name)) for name in check_names}
    applicable = {name: sum(1 for r in records if name in r["checks"]) for name in check_names}
    total = len(records)
    scores = [r["judge"]["overall"] for r in records if r.get("judge", {}).get("overall") is not None]
    judged = [r for r in records if r.get("judge", {}).get("overall") is not None]
    deliverable = sum(1 for r in judged
                      if all(r["checks"].values()) and r["judge"]["overall"] >= 3.5)
    return {
        "cases": total,
        "check_pass_rate": {name: round(passed[name] / applicable[name], 3) for name in check_names},
        "all_checks_passed": sum(1 for r in records if all(r["checks"].values())),
        "judge_mean": round(sum(scores) / len(scores), 2) if scores else None,
        "deliverable": deliverable,
        "latency_mean": round(sum(r["elapsed"] for r in records) / total, 2) if total else None,
    }
