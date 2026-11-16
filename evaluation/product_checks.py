"""产品行程的确定性检查：不依赖裁判模型，先卡结构与可追溯性。

这一层对应 TravelPlanner（arXiv:2402.01622）里的 constraint pass rate：
能程序化验证的约束就不交给模型判，既省钱，也能直接当 RL 的 dense reward。
"""

import re
from typing import Any

from product.pace import PACE_LIMITS, PACE_PLAN, detect_pace

NOISE_TOKENS = ("攻略", "门票", "地址", "图片", "开放时间", "营业时间", "【", "】", "｜", "点评", "简介")
TIME_ORDER_TOLERANCE = 0  # 只比较字符串顺序，HH:MM 定长可直接比
DIGITS = re.compile(r"\d+")


def _days(result: dict[str, Any]) -> list[dict[str, Any]]:
    days = result.get("days")
    return [d for d in days if isinstance(d, dict)] if isinstance(days, list) else []


def _activities(day: dict[str, Any]) -> list[dict[str, Any]]:
    items = day.get("activities")
    return [a for a in items if isinstance(a, dict)] if isinstance(items, list) else []


def _evidence_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e.get("id"): e for e in result.get("evidence") or [] if isinstance(e, dict)}


def _norm(text: Any) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or ""))


def _item_names(evidence: dict[str, Any]) -> list[str]:
    items = evidence.get("items")
    if not isinstance(items, list):
        return []
    return [str(item.get("name") or "") for item in items if isinstance(item, dict)]


def _bigrams(text: str) -> set:
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _title_matches(title: str, names: list[str]) -> bool:
    """标题与证据地点名：包含关系，或 2-gram 重合度 ≥60%（容忍“熊猫基地”这类简称）。"""
    normalized = _norm(title)
    if not normalized:
        return False
    grams = _bigrams(normalized)
    for name in names:
        candidate = _norm(name)
        if not candidate:
            continue
        if normalized in candidate or candidate in normalized:
            return True
        if grams and len(grams & _bigrams(candidate)) / len(grams) >= 0.6:
            return True
    return False


def _evidence_text(evidence: dict[str, Any]) -> str:
    parts: list[str] = []
    items = evidence.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            parts += [str(item.get("name") or ""), str(item.get("snippet") or "")]
    return "".join(parts)


def hard_pass_rate(checks: dict[str, bool]) -> float:
    """确定性检查的通过比例，供打分与 RL 奖励使用。"""
    if not checks:
        return 0.0
    return sum(1 for value in checks.values() if value) / len(checks)


_HOURS = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*小时")
_MINS = re.compile(r"([0-9]+)\s*分钟")


def _start_minutes(value: Any) -> int:
    hour, _, minute = str(value or "").partition(":")
    try:
        return int(hour) * 60 + int(minute)
    except ValueError:
        return -1


def _duration_minutes(value: Any) -> int:
    text = str(value or "")
    match = _HOURS.search(text)
    if match:
        return int(float(match.group(1)) * 60)
    match = _MINS.search(text)
    return int(match.group(1)) if match else 0


def _schedule_ok(days: list[dict[str, Any]]) -> bool:
    """每天的开始时间递增，且前一项结束不晚于后一项开始。"""
    for day in days:
        activities = _activities(day)
        starts = [_start_minutes(a.get("start")) for a in activities]
        if any(start < 0 for start in starts) or starts != sorted(starts) or len(starts) != len(set(starts)):
            return False
        for current, following in zip(activities, activities[1:], strict=False):
            finish = _start_minutes(current.get("start")) + _duration_minutes(current.get("duration"))
            if finish > _start_minutes(following.get("start")):
                return False
    return True


PERIODS = (("morning", 6 * 60, 12 * 60), ("afternoon", 12 * 60, 17 * 60 + 30),
           ("evening", 17 * 60 + 30, 24 * 60))


def _free_ratio(activities: list[dict[str, Any]]) -> float:
    """一天里“没被活动占用的时间”占比；活动越少、停留越短，留白越大。"""
    if len(activities) < 2:
        return 1.0
    ordered = sorted(activities, key=lambda a: _start_minutes(a.get("start")))
    start = _start_minutes(ordered[0].get("start"))
    end = max(_start_minutes(a.get("start")) + (_duration_minutes(a.get("duration")) or 90)
              for a in ordered)
    span = max(end - start, 1)
    busy = sum(_duration_minutes(a.get("duration")) or 90 for a in ordered)
    return round(max(0.0, 1 - busy / span), 2)


def _periods(activities: list[dict[str, Any]]) -> set:
    """按活动占用区间判断覆盖时段：跨 17:30 的活动算下午+晚上。"""
    buckets = set()
    for activity in activities:
        start = _start_minutes(activity.get("start"))
        duration = _duration_minutes(activity.get("duration")) or 90
        end = start + duration
        for name, begin, finish in PERIODS:
            if start < finish and end > begin:
                buckets.add(name)
    return buckets


def _total_hours(activities: list[dict[str, Any]]) -> float:
    return round(sum(_duration_minutes(activity.get("duration")) for activity in activities) / 60, 2)


def check_result(case: dict[str, Any], result: dict[str, Any]) -> dict[str, bool]:
    """返回逐项检查结果；键名会直接进报告。"""
    if case.get("expect") == "needs_clarification":
        return {"clarification": result.get("status") == "needs_clarification"}
    days = _days(result)
    pace = detect_pace(case.get("query"))
    min_count, max_count, min_hours, max_hours = PACE_LIMITS[pace]
    plan = PACE_PLAN[pace]
    activities = [a for d in days for a in _activities(d)]
    titles = [str(a.get("title") or "") for a in activities]
    evidence = _evidence_map(result)
    expected_days = case.get("days")
    expected_cities = [str(c).strip().rstrip("市") for c in case.get("cities") or [] if str(c).strip()]

    city_coverage = True
    if expected_cities:
        city_coverage = bool(days) and all(
            str(d.get("city") or "").strip().rstrip("市") in expected_cities for d in days)

    title_matches_evidence = True
    for activity in activities:
        names = _item_names(evidence.get(activity.get("evidence_id")) or {})
        if not names:
            continue
        if not _title_matches(str(activity.get("title") or ""), names):
            title_matches_evidence = False
            break

    # 行程里出现的数字（票价、时长金额）必须能在证据原文里找到，否则视为编造。
    no_fabricated_cost = True
    for activity in activities:
        numbers = DIGITS.findall(str(activity.get("cost") or ""))
        if not numbers:
            continue
        source = _evidence_text(evidence.get(activity.get("evidence_id")) or {}) + str(activity.get("description") or "")
        if any(number not in source for number in numbers):
            no_fabricated_cost = False
            break

    checks = {
        "complete": result.get("status") == "complete",
        "day_count": bool(days) and (expected_days is None or len(days) == expected_days),
        "density": bool(days) and all(min_count <= len(_activities(d)) <= max_count for d in days),
        "periods": bool(days) and all(len(_periods(_activities(d))) >= 2 for d in days),
        "day_load": bool(days) and all(min_hours <= _total_hours(_activities(d)) <= max_hours for d in days),
        "activity_length": bool(days) and all(
            plan["activity"][0] - 1.0 <= _duration_minutes(a.get("duration")) / 60 <= plan["activity"][1] + 1.0
            for a in activities),
        "free_time": bool(days) and all(
            _free_ratio(_activities(d)) >= plan["free"][0] and _free_ratio(_activities(d)) <= plan["free"][1]
            for d in days),
        "unique_titles": len(titles) == len(set(titles)),
        "no_overlap": _schedule_ok(days),
        "title_clean": bool(titles) and all(
            len(title) <= 24 and not any(token in title for token in NOISE_TOKENS) for title in titles),
        "time_order": bool(days) and all(
            [str(a.get("start")) for a in _activities(d)] == sorted(str(a.get("start")) for a in _activities(d))
            for d in days),
        "half_day": True,
        "fields_filled": bool(activities) and all(
            a.get("start") and a.get("duration") and a.get("transport") and a.get("cost") for a in activities),
        "evidence_ok": bool(activities) and all(
            (evidence.get(a.get("evidence_id")) or {}).get("status") == "ok" for a in activities),
        "city_coverage": city_coverage,
        "title_matches_evidence": title_matches_evidence,
        "no_fabricated_cost": no_fabricated_cost,
    }
    if any(token in str(case.get("query") or "") for token in ("半天", "大半天")):
        checks["half_day"] = bool(days) and all(
            1 <= len(_activities(d)) <= 2 and 4.5 <= _total_hours(_activities(d)) <= 7.5 for d in days)
    wanted = [str(place).strip() for place in case.get("named_places") or [] if str(place).strip()]
    if wanted:
        checks["named_places_covered"] = _named_places_covered(wanted, titles)
    return checks


def _squash(text: Any) -> str:
    return re.sub(r"[\s\W_]+", "", str(text or ""))


def _named_places_covered(wanted: list[str], titles: list[str]) -> bool:
    flattened = [_squash(title) for title in titles if _squash(title)]
    for place in wanted:
        target = _squash(place)
        if not target or not any(target in title or title in target for title in flattened):
            return False
    return True


def combined_score(checks: dict[str, bool], rubric: Any, fabrication: bool = False) -> Any:
    """0–1 综合分：硬约束 60% + rubric 40%，编造再扣 0.2。"""
    if rubric is None:
        return None
    score = 0.6 * hard_pass_rate(checks) + 0.4 * float(rubric) - (0.2 if fabrication else 0.0)
    return round(max(0.0, min(1.0, score)), 3)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐条记录汇总成通过率、平均分与延迟。"""
    check_names = sorted({name for r in records for name in r["checks"]})
    passed = {name: sum(1 for r in records if r["checks"].get(name)) for name in check_names}
    applicable = {name: sum(1 for r in records if name in r["checks"]) for name in check_names}
    total = len(records)
    rubrics = [(r.get("judge") or {}).get("rubric") for r in records]
    rubrics = [float(value) for value in rubrics if value is not None]
    scores = [r["score"] for r in records if r.get("score") is not None]
    deliverable = sum(1 for r in records
                      if r.get("score") is not None
                      and all(r["checks"].values())
                      and (r.get("judge") or {}).get("rubric", 0) >= 0.7
                      and not (r.get("judge") or {}).get("fabrication"))
    return {
        "cases": total,
        "check_pass_rate": {name: round(passed[name] / applicable[name], 3) for name in check_names},
        "all_checks_passed": sum(1 for r in records if all(r["checks"].values())),
        "hard_mean": round(sum(hard_pass_rate(r["checks"]) for r in records) / total, 3) if total else None,
        "rubric_mean": round(sum(rubrics) / len(rubrics), 3) if rubrics else None,
        "score_mean": round(sum(scores) / len(scores), 3) if scores else None,
        "deliverable": deliverable,
        "latency_mean": round(sum(r["elapsed"] for r in records) / total, 2) if total else None,
    }
