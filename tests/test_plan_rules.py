"""模型排期规则校验的单元测试（不调模型）。"""

from product.plan_rules import duration_minutes, parse_time, validate_plan

EVIDENCE = [
    {"id": "d1-p1", "items": [
        {"name": "西湖", "coordinates": "120.150,30.250", "opentime": "08:00", "endtime": "18:00"}]},
    {"id": "d1-p2", "items": [
        {"name": "灵隐寺", "coordinates": "120.100,30.240", "opentime": "07:00", "endtime": "17:30"}]},
    {"id": "d1-p3", "items": [
        {"name": "楼外楼", "coordinates": "120.148,30.252", "opentime": "10:30", "endtime": "20:30"}]},
]


def make_day(activities, meals=None):
    return [{"day": 1, "activities": activities, "meals": meals or []}]


def test_parse_time_and_duration():
    assert parse_time("9:05") == 9 * 60 + 5
    assert parse_time("24:00") is None
    assert parse_time("") is None
    assert duration_minutes({"duration": "2.5 小时"}) == 150
    assert duration_minutes({"duration": "90 分钟"}) == 90
    assert duration_minutes({"duration_minutes": 45}) == 45
    assert duration_minutes({}) == 90


def test_valid_day_has_no_issues():
    days = make_day([
        {"title": "西湖", "start": "09:00", "duration": "2 小时"},
        {"title": "灵隐寺", "start": "12:00", "duration": "2 小时"},
    ], [{"title": "楼外楼", "start": "16:30"}])
    assert validate_plan(days, EVIDENCE) == []


def test_unknown_place_and_bad_time():
    days = make_day([{"title": "雷峰塔", "start": "9点半"}])
    codes = {issue["code"] for issue in validate_plan(days, EVIDENCE)}
    assert "unknown_place" in codes
    assert "bad_time" in codes


def test_overlap_detected():
    days = make_day([
        {"title": "西湖", "start": "09:00", "duration": "3 小时"},
        {"title": "灵隐寺", "start": "10:00", "duration": "1 小时"},
    ])
    codes = [issue["code"] for issue in validate_plan(days, EVIDENCE)]
    assert "overlap" in codes


def test_closed_window_detected():
    days = make_day([{"title": "灵隐寺", "start": "17:10", "duration": "1 小时"}])
    codes = [issue["code"] for issue in validate_plan(days, EVIDENCE)]
    assert "closed" in codes


def test_transit_short_detected():
    days = make_day([
        {"title": "西湖", "start": "09:00", "duration": "2 小时"},
        {"title": "灵隐寺", "start": "11:00", "duration": "2 小时"},
    ])
    codes = [issue["code"] for issue in validate_plan(days, EVIDENCE)]
    assert "transit_short" in codes


def test_day_end_and_too_early():
    days = make_day([
        {"title": "西湖", "start": "08:00", "duration": "1 小时"},
        {"title": "灵隐寺", "start": "19:30", "duration": "2 小时"},
    ])
    codes = [issue["code"] for issue in validate_plan(days, EVIDENCE)]
    assert "too_early" in codes
    assert "day_end" in codes


def test_missing_meal_detected():
    days = make_day([{"title": "西湖", "start": "09:00", "duration": "2 小时"}])
    codes = [issue["code"] for issue in validate_plan(days, EVIDENCE)]
    assert "missing_meal" in codes


def test_meal_closed_detected():
    days = make_day([{"title": "西湖", "start": "09:00", "duration": "2 小时"}],
                    [{"title": "楼外楼", "start": "21:00"}])
    codes = [issue["code"] for issue in validate_plan(days, EVIDENCE)]
    assert "closed" in codes
