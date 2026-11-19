"""目的地白名单测试：挡住明显不存在的城市。"""

from product.destinations import known_destination, unknown_destinations


def test_known_destination_accepts_common_forms():
    assert known_destination("杭州")
    assert known_destination("杭州市")
    assert known_destination("千岛湖")
    assert known_destination("南昌")
    assert known_destination("Hefei")
    assert known_destination("Hangzhou")
    assert known_destination("西安")
    assert known_destination("Xian")


def test_known_destination_rejects_fake_places():
    assert not known_destination("火星")
    assert not known_destination("不知道")
    assert not known_destination("")


def test_unknown_destinations_filters_known():
    assert unknown_destinations(["杭州", "火星"]) == ["火星"]
    assert unknown_destinations(["杭州", "苏州"]) == []
