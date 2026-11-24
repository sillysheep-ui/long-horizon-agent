"""intake 新字段（人数/房间/城际交通）的清洗与提示词。"""

from product.postprocess import clean_people, clean_room_type, clean_rooms, clean_transport
from product.prompts import ANSWER_SYSTEM_PROMPT, INTAKE_SYSTEM_PROMPT


def test_clean_people_accepts_units_and_bounds():
    assert clean_people(3) == 3
    assert clean_people("3人") == 3
    assert clean_people("0") == 0
    assert clean_people("99") == 0
    assert clean_people("") == 0


def test_clean_rooms_bounds():
    assert clean_rooms("1间") == 1
    assert clean_rooms(11) == 0
    assert clean_rooms(None) == 0


def test_clean_room_type_maps_keywords():
    assert clean_room_type("一间大床房") == "大床房"
    assert clean_room_type("标准间") == "标间"
    assert clean_room_type("随便住") == ""


def test_clean_transport_maps_keywords():
    assert clean_transport("坐高铁去") == "高铁"
    assert clean_transport("动车往返") == "高铁"
    assert clean_transport("飞过去") == "飞机"
    assert clean_transport("不限") == ""


def test_prompts_declare_new_fields():
    for field in ("people", "rooms", "room_type", "transport"):
        assert field in INTAKE_SYSTEM_PROMPT
    assert "constraints.people" in ANSWER_SYSTEM_PROMPT
