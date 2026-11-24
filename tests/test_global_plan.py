"""全局选点与排序模块的单元测试（不调模型）。"""

from product.global_plan import (
    assign_days,
    build_days,
    coord_of,
    open_window,
    total_distance,
)


def make(name: str, lon: float, lat: float, table: str = "attractions", **extra):
    item = {"name": name, "coordinates": [lon, lat], "source_table": table,
            "recommendmintime": 1, "recommendmaxtime": 2}
    item.update(extra)
    return item


def test_coord_of_supports_three_formats():
    assert coord_of({"coordinates": [120.1, 30.2]}) == (30.2, 120.1)
    assert coord_of({"coordinates": "120.1,30.2"}) == (30.2, 120.1)
    assert coord_of({"lat": 30.2, "lon": 120.1}) == (30.2, 120.1)
    assert coord_of({"coordinates": [200, 30]}) is None
    assert coord_of({}) is None


def test_open_window_handles_all_day_and_missing():
    assert open_window({"opentime": "00:00", "endtime": "24:00"}) is None
    assert open_window({}) is None
    assert open_window({"opentime": "09:00", "endtime": "17:30"}) == (9 * 60, 17 * 60 + 30)


def test_assign_days_keeps_geographic_clusters_together():
    west = [make(f"W{i}", 120.0 + i * 0.005, 30.0) for i in range(3)]
    east = [make(f"E{i}", 120.6 + i * 0.005, 30.6) for i in range(3)]
    groups = assign_days(west + east, 2, 3)
    names = [{item["name"][0] for item in group} for group in groups]
    assert names == [{"W"}, {"E"}]


def test_build_days_has_meals_and_no_duplicate_titles():
    candidates = [make(f"A{i}", 120.0 + i * 0.01, 30.0) for i in range(4)]
    candidates += [make(f"B{i}", 120.3 + i * 0.01, 30.3) for i in range(4)]
    candidates += [make(f"食A{i}", 120.0 + i * 0.01, 30.01, "restaurants") for i in range(2)]
    candidates += [make(f"食B{i}", 120.3 + i * 0.01, 30.31, "restaurants") for i in range(2)]
    days = build_days(candidates, 2, pace="packed")
    assert [day["day"] for day in days] == [1, 2]
    titles = [item["name"] for day in days for item in day["activities"]]
    assert len(titles) == len(set(titles))
    for day in days:
        starts = [item["start"] for item in day["activities"]]
        assert starts == sorted(starts)
        assert any(item.get("kind") == "meal" for item in day["activities"])


def test_build_days_respects_opening_hours():
    early = make("早关门", 120.0, 30.0, opentime="09:00", endtime="11:30",
                 recommendmintime=0.5, recommendmaxtime=0.5)
    late = make("下午开", 120.0, 30.0)
    candidates = [early, late]
    days = build_days(candidates, 1, pace="relaxed", named_places=["下午开"])
    first = days[0]["activities"][0]
    assert first["name"] == "下午开"
    closed = [item for item in days[0]["activities"] if item["name"] == "早关门"]
    assert closed and closed[0]["start"] < "11:30"


def test_build_days_skips_place_open_after_day_ends():
    late_night = make("夜场", 120.0, 30.0, opentime="21:00", endtime="23:00")
    candidates = [make("白天", 120.0, 30.0), late_night]
    days = build_days(candidates, 1, pace="relaxed")
    names = [item["name"] for item in days[0]["activities"]]
    assert "白天" in names
    assert "夜场" not in names


def test_global_partition_beats_sequential_split_on_interleaved_order():
    candidates = [make(f"W{i}", 120.0 + i * 0.005, 30.0) for i in range(3)]
    candidates += [make(f"E{i}", 120.5 + i * 0.005, 30.5) for i in range(3)]
    interleaved = [candidates[0], candidates[3], candidates[1], candidates[4], candidates[2], candidates[5]]
    sequential = [interleaved[:3], interleaved[3:]]
    grouped = assign_days(interleaved, 2, 3)
    assert total_distance(grouped) < total_distance(sequential) * 0.5


def test_candidates_from_evidence_dedupes_and_drops_unlocated():
    from product.global_plan import candidates_from_evidence

    evidence = [
        {"id": "e1", "status": "ok", "city": "杭州", "items": [
            {"name": "西湖", "coordinates": [120.15, 30.25], "source_table": "attractions"},
            {"name": "西湖", "coordinates": [120.15, 30.25]},
            {"name": "无坐标", "coordinates": None},
        ]},
        {"id": "e2", "status": "failed", "items": [{"name": "失败的", "coordinates": [120.0, 30.0]}]},
    ]
    candidates = candidates_from_evidence(evidence)
    assert [item["name"] for item in candidates] == ["西湖"]
    assert candidates[0]["evidence_id"] == "e1"
    assert candidates[0]["city"] == "杭州"


def test_skeleton_to_days_outputs_answer_schema():
    from product.global_plan import skeleton_to_days

    skeleton = [{"day": 1, "activities": [
        {"name": "西湖", "start": "09:00", "duration_minutes": 120, "evidence_id": "e1"},
        {"name": "楼外楼", "start": "12:00", "duration_minutes": 60, "evidence_id": "e2", "kind": "meal"},
    ]}]
    evidence = [{"id": "e1", "items": [{"name": "西湖"}]}, {"id": "e2", "items": [{"name": "楼外楼"}]}]
    days = skeleton_to_days(skeleton, evidence)
    assert days[0]["day"] == 1
    assert [item["title"] for item in days[0]["activities"]] == ["西湖", "楼外楼"]
    assert days[0]["activities"][0]["duration"] == "2 小时"
    assert days[0]["activities"][1]["kind"] == "meal"


def _pool(city, count, prefix):
    items = [{"name": f"{prefix}{i}", "coordinates": [120.0 + i * 0.01, 30.0],
              "source_table": "attractions", "city": city} for i in range(count)]
    items.append({"name": f"{prefix}食", "coordinates": [120.02, 30.01],
                  "source_table": "restaurants", "city": city})
    return {"id": f"e-{prefix}", "status": "ok", "city": city, "items": items}


def test_plan_city_days_groups_consecutive_cities():
    from product.global_plan import plan_city_days

    blocks = plan_city_days([{"day": 1, "city": "杭州"}, {"day": 2, "city": "杭州市"},
                             {"day": 3, "city": "苏州"}], ["杭州"])
    assert blocks == [("杭州", [1, 2]), ("苏州", [3])]


def test_build_skeleton_assigns_blocks_by_city():
    from product.global_plan import build_skeleton

    evidence = [_pool("杭州", 6, "H"), _pool("苏州", 3, "S")]
    constraints = {"destinations": ["杭州", "苏州"], "pace": "normal", "named_places": []}
    plan_days = [{"day": 1, "city": "杭州"}, {"day": 2, "city": "杭州"}, {"day": 3, "city": "苏州"}]
    skeleton = build_skeleton(constraints, plan_days, evidence)
    assert skeleton["ok"] is True
    assert [day["day"] for day in skeleton["days"]] == [1, 2, 3]
    assert [day["city"] for day in skeleton["days"]] == ["杭州", "杭州", "苏州"]
    assert skeleton["candidates"] == {"杭州": 7, "苏州": 4}
    assert skeleton["route_km"] > 0


def test_build_skeleton_reports_insufficient_candidates():
    from product.global_plan import build_skeleton

    evidence = [_pool("杭州", 8, "H"), _pool("苏州", 1, "S")]
    constraints = {"destinations": ["杭州", "苏州"], "pace": "normal"}
    plan_days = [{"day": 1, "city": "杭州"}, {"day": 2, "city": "苏州"}, {"day": 3, "city": "苏州"}]
    skeleton = build_skeleton(constraints, plan_days, evidence)
    assert skeleton["ok"] is False
    assert "苏州" in skeleton["reason"]
    assert [day["city"] for day in skeleton["days"]] == ["杭州"]


def test_build_days_ignores_lodging_and_only_uses_attractions():
    candidates = [
        make("西湖", 120.0, 30.0),
        make("某酒店", 120.001, 30.001, "accommodations"),
        make("某餐厅", 120.002, 30.002, "restaurants"),
    ]
    days = build_days(candidates, 1, pace="relaxed")
    titles = [item["name"] for item in days[0]["activities"]]
    assert "西湖" in titles
    assert "某酒店" not in titles
