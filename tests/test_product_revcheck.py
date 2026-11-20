"""修改要求达标检查。"""

from product.revcheck import check_requirements


def _day(*titles, duration="2 小时"):
    return {"day": 1, "activities": [
        {"title": title, "display_title": title, "duration": duration} for title in titles]}


def test_quiet_matches_nature_keywords():
    result = check_requirements(["安静"], [_day("云栖竹径", "龙井茶园")])
    assert result == {"met": {"安静": True}, "unmet": []}


def test_quiet_fails_on_noisy_places():
    result = check_requirements(["安静"], [_day("西湖音乐喷泉", "河坊街")])
    assert result["met"] == {"安静": False}
    assert result["unmet"] == ["安静"]


def test_kid_friendly_matches_museum_or_park():
    assert check_requirements(["适合孩子"], [_day("上海科技馆")])["unmet"] == []
    assert check_requirements(["适合孩子"], [_day("外滩", "南京路")])["unmet"] == ["适合孩子"]


def test_slow_requires_longer_stays_and_few_activities():
    assert check_requirements(["慢节奏"], [_day("西湖", "龙井村", duration="3 小时")])["unmet"] == []
    assert check_requirements(["慢节奏"], [_day("西湖", duration="1 小时")])["unmet"] == ["慢节奏"]
    many = [_day("A", "B", "C", "D", duration="3 小时")]
    assert check_requirements(["慢节奏"], many)["unmet"] == ["慢节奏"]


def test_compact_requires_enough_activities():
    assert check_requirements(["紧凑"], [_day("A", "B", "C")])["unmet"] == []
    assert check_requirements(["紧凑"], [_day("A", "B")])["unmet"] == ["紧凑"]


def test_empty_day_marks_unmet():
    assert check_requirements(["安静"], [{"day": 1, "activities": []}])["unmet"] == ["安静"]
