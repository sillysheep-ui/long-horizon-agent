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
