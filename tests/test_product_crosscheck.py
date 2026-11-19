"""跨槽位核验：餐到活动、用餐时间、预算合计。"""

from product.crosscheck import budget_fits, cross_checks, meal_near, meal_time

NEAR = "120.1500,30.2500"
FAR = "121.5000,31.2000"


def _day(meal_coord=NEAR, meal_start="12:30"):
    return {
        "day": 1,
        "activities": [
            {"title": "西湖", "start": "09:30", "duration": "2 小时", "coordinates": NEAR, "cost": "参考 60 元（未核实）"},
            {"title": "灵隐寺", "start": "14:00", "duration": "1.5 小时", "coordinates": NEAR, "cost": "参考 30 元（未核实）"},
        ],
        "meals": [{"type": "lunch", "title": "楼外楼", "start": meal_start,
                   "coordinates": meal_coord, "cost": "参考 100 元（未核实）"}],
    }


def test_meal_near_ok():
    result = meal_near(_day())
    assert result and result["ok"] is True


def test_meal_near_far():
    result = meal_near(_day(meal_coord=FAR))
    assert result and result["ok"] is False


def test_meal_time_overlap():
    result = meal_time(_day(meal_start="10:00"))
    assert result and result["ok"] is False and result["bad"] == ["楼外楼"]


def test_meal_time_ok():
    assert meal_time(_day())["ok"] is True


def test_budget_fits():
    assert budget_fits([_day()], {"budget": "500 元"}) is True
    assert budget_fits([_day()], {"budget": "100 元"}) is False
    assert budget_fits([{"activities": [], "meals": []}], {"budget": "500"}) is None


def test_cross_checks_skips_unknown():
    checks = cross_checks([{"activities": [{"start": "09:30", "duration": "1 小时"}], "meals": []}],
                          {"budget": ""})
    assert checks == {}
