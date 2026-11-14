"""行程排程：补足数量、时间不重叠。"""

from product.orchestrator import _duration_minutes, _fit_schedule, _finalize_days


def test_duration_parsing():
    assert _duration_minutes("2.5 小时") == 150
    assert _duration_minutes("90 分钟") == 90
    assert _duration_minutes("未核实") == 90


def test_overlapping_start_is_pushed_to_previous_end():
    activities = [
        {"title": "A", "start": "09:30", "duration": "3 小时"},
        {"title": "B", "start": "10:00", "duration": "1 小时"},
        {"title": "C", "start": "12:30", "duration": "1 小时"},
    ]
    fitted = _fit_schedule(activities)
    assert [item["start"] for item in fitted] == ["09:30", "12:30", "13:30"]


def test_out_of_order_activities_are_sorted():
    activities = [
        {"title": "B", "start": "15:00", "duration": "1 小时"},
        {"title": "A", "start": "09:00", "duration": "1 小时"},
    ]
    assert [item["title"] for item in _fit_schedule(activities)] == ["A", "B"]


def test_day_is_filled_to_at_least_two_when_evidence_allows():
    evidence = [{
        "id": "d1-p1", "status": "ok",
        "items": [{"name": "西湖"}, {"name": "灵隐寺"}, {"name": "九溪"}],
    }]
    days = _finalize_days(
        [{"day": 1, "city": "杭州", "activities": [{"title": "西湖", "start": "09:30", "evidence_id": "d1-p1"}]}],
        evidence, [{"day": 1, "city": "杭州"}])
    titles = [activity["title"] for activity in days[0]["activities"]]
    assert len(titles) == 2
    assert titles[0] == "西湖"
