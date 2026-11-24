"""全局选点与排序：先有候选池，再按天分配和排序。

与 ``planner.py`` 的逐天查询不同，这里假设已经有了一批带坐标、营业时间、
建议时长的候选点（来自检索），然后一次性决定“哪些点进哪一天、每天怎么排”。
这样跨天的承接是一致的，不会出现每天各自最近邻导致的来回折返。

只做确定性计算，不调用模型；模型只负责生成候选查询和最终文案。
"""

from typing import Any

from product.geo import haversine_km, order_nearest, parse_coord, transit_minutes

DEFAULT_START_MINUTES = 9 * 60
DEFAULT_DURATION_MINUTES = 120
LUNCH_MINUTES = 12 * 60
DINNER_MINUTES = 18 * 60
EAT_MINUTES = 60
PACE_PER_DAY = {"relaxed": 2, "normal": 3, "packed": 4}
DAY_END_MINUTES = 21 * 60


def _minutes(value: Any, default: int = 0) -> int:
    text = str(value or "").strip()
    if not text or ":" not in text:
        return default
    hour, _, minute = text.partition(":")
    try:
        return int(hour) * 60 + int(minute)
    except ValueError:
        return default


def _hhmm(minutes: int) -> str:
    minutes = max(0, min(23 * 60 + 59, int(minutes)))
    return "%02d:%02d" % (minutes // 60, minutes % 60)


def coord_of(item: dict[str, Any]) -> tuple[float, float] | None:
    """兼容三种坐标写法：字符串 "经度,纬度"、[经度, 纬度]、lat/lon 字段。"""
    raw = item.get("coordinates")
    if isinstance(raw, str) and "," in raw:
        return parse_coord(item)
    if isinstance(raw, list | tuple) and len(raw) >= 2:
        try:
            lon, lat = float(raw[0]), float(raw[1])
        except (TypeError, ValueError):
            return None
        if -180 <= lon <= 180 and -90 <= lat <= 90:
            return lat, lon
        return None
    return parse_coord(item)


def open_window(item: dict[str, Any]) -> tuple[int, int] | None:
    """返回 (开门分钟, 关门分钟)；全天开放或缺失时返回 None（不限制）。"""
    start, end = item.get("opentime"), item.get("endtime")
    if not start and not end:
        return None
    start_min = _minutes(start, DEFAULT_START_MINUTES)
    end_min = _minutes(end, 24 * 60)
    if end_min <= start_min:
        return None
    if start_min <= 0 and end_min >= 24 * 60:
        return None
    return start_min, end_min


def duration_minutes(item: dict[str, Any]) -> int:
    low = item.get("recommendmintime")
    high = item.get("recommendmaxtime")
    try:
        low = float(low) if low is not None else None
        high = float(high) if high is not None else None
    except (TypeError, ValueError):
        low = high = None
    if low and high:
        return int(round((low + high) / 2 * 60))
    if high:
        return int(round(high * 60))
    return DEFAULT_DURATION_MINUTES


def _is_food(item: dict[str, Any]) -> bool:
    return str(item.get("source_table") or "") == "restaurants" or bool(item.get("cuisine"))


def _is_sight(item: dict[str, Any]) -> bool:
    return str(item.get("source_table") or "") == "attractions"


def _rank_key(item: dict[str, Any], named: set[str], anchor: str) -> tuple[int, float]:
    name = str(item.get("name") or "")
    named_hit = 1 if any(word and word in name for word in named) else 0
    anchor_hit = 0
    if anchor == "food" and _is_food(item):
        anchor_hit = 1
    elif anchor in ("stay", "sight") and _is_sight(item):
        anchor_hit = 1
    return (named_hit * 2 + anchor_hit, float(item.get("score") or 0.0))


def _farthest_seeds(candidates: list[dict[str, Any]], count: int) -> list[tuple[float, float]]:
    located = [(item, coord_of(item)) for item in candidates]
    located = [(item, coord) for item, coord in located if coord]
    if not located:
        return []
    seeds = [located[0][1]]
    while len(seeds) < count:
        best, best_distance = None, -1.0
        for _, coord in located:
            distance = min(haversine_km(coord, seed) for seed in seeds)
            if distance > best_distance:
                best, best_distance = coord, distance
        if best is None:
            break
        seeds.append(best)
    return seeds


def assign_days(candidates: list[dict[str, Any]], day_count: int,
                per_day: int) -> list[list[dict[str, Any]]]:
    """把候选点按地理邻近分成 day_count 组，每组不超过 per_day 个。"""
    if day_count <= 0:
        return []
    per_day = max(1, per_day)
    limit = day_count * per_day
    chosen = candidates[:limit]
    if not chosen:
        return [[] for _ in range(day_count)]
    seeds = _farthest_seeds(chosen, day_count)
    groups: list[list[dict[str, Any]]] = [[] for _ in range(day_count)]
    for item in chosen:
        coord = coord_of(item)
        if coord and seeds:
            index = min(range(len(seeds)), key=lambda i: haversine_km(coord, seeds[i]))
        else:
            index = min(range(day_count), key=lambda i: len(groups[i]))
        groups[index].append(item)
    # 把超额组里离本组最远的点挪到人少的组，避免一天挤爆、一天空着。
    for _ in range(day_count * per_day):
        over = [index for index, group in enumerate(groups) if len(group) > per_day]
        under = [index for index, group in enumerate(groups) if len(group) < per_day]
        if not over or not under:
            break
        source = max(over, key=lambda index: len(groups[index]))
        target = min(under, key=lambda index: len(groups[index]))
        item = groups[source].pop()
        groups[target].append(item)
    return groups


def order_group(group: list[dict[str, Any]], start: tuple[float, float] | None) -> list[dict[str, Any]]:
    """组内按最近邻排序；有营业时间的点，按“赶不上就等开门”模拟一遍。"""
    if len(group) <= 1:
        return list(group)
    ordered = order_nearest([
        {**item, "start": _hhmm(DEFAULT_START_MINUTES)} for item in group
    ])
    if start:
        first = min(ordered, key=lambda item: haversine_km(start, coord_of(item) or start))
        ordered.remove(first)
        ordered.insert(0, first)
    return ordered


def _pick_meal(candidates: list[dict[str, Any]], anchor_point: tuple[float, float] | None,
               used: set[str]) -> dict[str, Any] | None:
    meals = [item for item in candidates if _is_food(item) and str(item.get("name")) not in used]
    if not meals:
        return None
    if anchor_point:
        meals.sort(key=lambda item: haversine_km(anchor_point, coord_of(item) or anchor_point))
    picked = meals[0]
    used.add(str(picked.get("name")))
    return picked


def build_days(candidates: list[dict[str, Any]], day_count: int, *,
               pace: str = "normal", anchor: str = "", named_places: list[str] | None = None,
               start: tuple[float, float] | None = None) -> list[dict[str, Any]]:
    """返回可以直接写文案的每日骨架：activities 已选点、排序、算好时间。"""
    named = [str(word) for word in (named_places or []) if str(word).strip()]
    named_set = set(named)
    per_day = PACE_PER_DAY.get(str(pace or "normal"), PACE_PER_DAY["normal"])
    sights = [item for item in candidates if not _is_food(item)]
    sights.sort(key=lambda item: _rank_key(item, named_set, anchor), reverse=True)
    named_items = [item for item in sights if any(word in str(item.get("name") or "") for word in named)]
    rest = [item for item in sights if item not in named_items]
    groups = assign_days(named_items + rest, day_count, per_day)
    used: set[str] = set()
    days: list[dict[str, Any]] = []
    for index, group in enumerate(groups, 1):
        day_items = order_group(group, start)
        activities: list[dict[str, Any]] = []
        cursor = DEFAULT_START_MINUTES
        previous = start
        lunch_done = False

        def add_meal(target: int, activities: list[dict[str, Any]] = activities) -> None:
            nonlocal cursor, lunch_done
            meal = _pick_meal(candidates, coord_of(activities[-1]) if activities else start, used)
            if meal is None:
                return
            item2 = dict(meal)
            item2["kind"] = "meal"
            item2["start"] = _hhmm(max(cursor, target))
            item2["duration_minutes"] = EAT_MINUTES
            activities.append(item2)
            cursor = max(cursor, target) + EAT_MINUTES

        for item in day_items:
            coord = coord_of(item)
            transit = transit_minutes(previous, coord) if previous and coord else 0
            if not lunch_done and cursor + transit >= LUNCH_MINUTES - 30:
                add_meal(LUNCH_MINUTES)
                lunch_done = True
                transit = transit_minutes(previous, coord) if previous and coord else 0
            cursor += transit
            window = open_window(item)
            if window:
                open_min, close_min = window
                cursor = max(cursor, open_min)
                if cursor + 30 > close_min or cursor >= DAY_END_MINUTES:
                    continue
            duration = duration_minutes(item)
            item2 = dict(item)
            item2["start"] = _hhmm(cursor)
            item2["duration_minutes"] = duration
            activities.append(item2)
            used.add(str(item.get("name")))
            cursor += duration
            previous = coord or previous
        if not lunch_done:
            add_meal(LUNCH_MINUTES)
        if len(activities) >= 3:
            add_meal(max(cursor, DINNER_MINUTES))
        activities.sort(key=lambda item: _minutes(item.get("start"), 23 * 60))
        days.append({"day": index, "activities": activities,
                     "hours": round(sum(item.get("duration_minutes", 0) for item in activities) / 60, 1)})
    return days


def total_distance(days: list[list[dict[str, Any]]]) -> float:
    """按“天内相邻 + 跨天末点到次日起点”累计直线距离，供评测对比。"""
    total = 0.0
    previous: tuple[float, float] | None = None
    for group in days:
        for item in group:
            coord = coord_of(item)
            total += haversine_km(previous, coord) if coord and previous else 0.0
            previous = coord or previous
    return round(total, 2)


def candidates_from_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把检索证据里的地点摊平成候选池，按名字去重，只保留有坐标的。"""
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in evidence:
        if str(record.get("status")) != "ok":
            continue
        for item in record.get("items") or []:
            name = str(item.get("name") or "").strip()
            if not name or name in seen or not coord_of(item):
                continue
            seen.add(name)
            candidate = dict(item)
            candidate["city"] = str(item.get("city") or record.get("city") or "")
            candidate["evidence_id"] = str(record.get("id") or "")
            candidates.append(candidate)
    return candidates


def _duration_text(minutes: int) -> str:
    hours = minutes / 60
    if abs(hours - round(hours)) < 0.25:
        return "%d 小时" % round(hours)
    return "%.1f 小时" % hours


def skeleton_to_days(skeleton: list[dict[str, Any]],
                     evidence: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """把选点骨架转成回答协议里的 days，模型只需补文案时也能直接当兜底。"""
    by_id = {str(record.get("id")): record for record in (evidence or []) if record.get("id")}
    days: list[dict[str, Any]] = []
    for day in skeleton:
        activities = []
        for item in day.get("activities") or []:
            record = by_id.get(str(item.get("evidence_id") or "")) or {}
            names = [str(row.get("name") or "") for row in record.get("items") or [] if row.get("name")]
            title = str(item.get("name") or "").strip() or (names[0] if names else "")
            if not title:
                continue
            minutes = int(item.get("duration_minutes") or DEFAULT_DURATION_MINUTES)
            activities.append({
                "title": title,
                "start": str(item.get("start") or "09:00"),
                "duration": _duration_text(minutes),
                "description": ("推荐游览 %s。" % _duration_text(minutes)) if str(item.get("kind")) != "meal"
                               else "用餐时间，可按口味替换。",
                "transport": "市内步行/打车，按导航为准",
                "cost": (str(item.get("price")) + " 元") if item.get("price") not in (None, "") else "费用未核实",
                "evidence_id": item.get("evidence_id"),
                **({"kind": "meal"} if str(item.get("kind")) == "meal" else {}),
            })
        if activities:
            days.append({"day": int(day.get("day") or len(days) + 1), "activities": activities})
    return days


def _city_key(city: Any) -> str:
    return str(city or "").strip().removesuffix("市").strip()


def same_city(first: Any, second: Any) -> bool:
    left, right = _city_key(first), _city_key(second)
    if not left or not right:
        return False
    return left == right or left in right or right in left


def plan_city_days(plan_days: list[dict[str, Any]],
                   destinations: list[str] | None = None) -> list[tuple[str, list[int]]]:
    """把 plan 的每日城市压成连续块：[(城市, [天号...]), ...]。"""
    fallback = str((destinations or [""])[0])
    blocks: list[tuple[str, list[int]]] = []
    for index, day in enumerate(plan_days or [], 1):
        city = str(day.get("city") or fallback)
        if blocks and same_city(blocks[-1][0], city):
            blocks[-1][1].append(index)
        else:
            blocks.append((city, [index]))
    return blocks


def _centroid(items: list[dict[str, Any]]) -> tuple[float, float] | None:
    coords = [coord_of(item) for item in items if coord_of(item)]
    if not coords:
        return None
    return (sum(coord[0] for coord in coords) / len(coords),
            sum(coord[1] for coord in coords) / len(coords))


def build_skeleton(constraints: dict[str, Any], plan_days: list[dict[str, Any]],
                   evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """按城市把候选池变成每日骨架；候选不足时返回 ok=False 交给上层回退。"""
    candidates = candidates_from_evidence(evidence)
    destinations = [str(city) for city in constraints.get("destinations") or []]
    blocks = plan_city_days(plan_days, destinations)
    named = [str(word) for word in constraints.get("named_places") or []]
    anchor = str(constraints.get("anchor") or "")
    pace = str(constraints.get("pace") or "normal")
    windows = {int(window.get("day") or 0): float(window.get("hours") or 0)
               for window in constraints.get("day_windows") or []
               if isinstance(window, dict) and window.get("day")}
    days: list[dict[str, Any]] = []
    per_city: dict[str, int] = {}
    reason = ""
    for city, day_numbers in blocks:
        pool = [item for item in candidates if same_city(item.get("city"), city)]
        per_city[city] = len(pool)
        if len(pool) < 2 * len(day_numbers):
            reason = reason or "%s 候选不足（%d 个，至少需要 %d 个）" % (
                city, len(pool), 2 * len(day_numbers))
            continue
        city_pace = pace
        if any(windows.get(number, 24) <= 4.5 for number in day_numbers):
            city_pace = "relaxed"
        built = build_days(pool, len(day_numbers), pace=city_pace, anchor=anchor,
                           named_places=named, start=_centroid(pool))
        for offset, day in enumerate(built):
            day["day"] = day_numbers[offset]
            day["city"] = city
            days.append(day)
    days.sort(key=lambda day: int(day.get("day") or 0))
    return {"days": days, "candidates": per_city, "ok": not reason, "reason": reason,
            "route_km": total_distance([day.get("activities") or [] for day in days])}
