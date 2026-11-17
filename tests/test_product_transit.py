"""跨天衔接：估算前一天结束点到后一天起点，并把当天活动顺延。"""

from product.transit import add_transfers, estimate_transfer


def _day(day, city, activities):
    return {"day": day, "city": city, "activities": activities}


def test_same_city_close_returns_none():
    prev = _day(1, "杭州", [{"title": "西湖", "start": "16:00", "coordinates": "120.150,30.270"}])
    nxt = _day(2, "杭州", [{"title": "湖畔咖啡", "start": "09:00", "coordinates": "120.152,30.272"}])
    assert estimate_transfer(prev, nxt) is None


def test_same_city_far_estimates_drive():
    prev = _day(1, "杭州", [{"title": "西湖", "start": "16:00", "coordinates": "120.150,30.270"}])
    nxt = _day(2, "杭州", [{"title": "灵隐寺", "start": "09:00", "coordinates": "120.100,30.240"}])
    transfer = estimate_transfer(prev, nxt)
    assert transfer["mode"] == "drive"
    assert transfer["minutes"] >= 15


def test_cross_city_uses_train_estimate():
    prev = _day(1, "杭州", [{"title": "西湖", "start": "16:00", "coordinates": "120.150,30.270"}])
    nxt = _day(2, "苏州", [{"title": "拙政园", "start": "09:30", "coordinates": "120.620,31.300"}])
    transfer = estimate_transfer(prev, nxt)
    assert transfer["mode"] == "train"
    assert transfer["minutes"] >= 60
    assert transfer["from"] == "杭州" and transfer["to"] == "苏州"


def test_cross_city_without_coordinates_uses_default():
    prev = _day(1, "杭州", [{"title": "西湖", "start": "16:00"}])
    nxt = _day(2, "苏州", [{"title": "拙政园", "start": "09:30"}])
    transfer = estimate_transfer(prev, nxt)
    assert transfer["mode"] == "unknown"
    assert transfer["minutes"] == 180
    assert transfer["km"] is None


def test_add_transfers_shifts_arrival():
    days = [
        _day(1, "杭州", [{"title": "西湖", "start": "16:00", "coordinates": "120.150,30.270"}]),
        _day(2, "苏州", [{"title": "拙政园", "start": "09:30", "coordinates": "120.620,31.300"},
                         {"title": "平江路", "start": "14:00", "coordinates": "120.630,31.320"}]),
    ]
    result = add_transfers(days)
    transfer = result[1]["transfer"]
    assert transfer["arrival"] == result[1]["activities"][0]["start"]
    assert result[1]["activities"][0]["start"] >= transfer["arrival"]
    assert "transfer" not in result[0]


def test_add_transfers_skips_existing():
    days = [
        _day(1, "杭州", [{"title": "西湖", "start": "16:00"}]),
        _day(2, "苏州", [{"title": "拙政园", "start": "09:30"}],
             ) | {"transfer": {"from": "杭州", "to": "苏州", "minutes": 120, "start": "07:00"}},
    ]
    result = add_transfers(days)
    assert result[1]["transfer"]["minutes"] == 120
