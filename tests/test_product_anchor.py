"""锚定识别与餐饮/住宿槽位的基础校验。"""

from evaluation.product_checks import check_result
from product.anchor import detect_anchor
from product.postprocess import clean_lodging, clean_meals


def test_detect_food_anchor():
    assert detect_anchor("成都两天，以吃为主，想吃火锅和小吃")["anchor"] == "food"


def test_detect_stay_anchor():
    assert detect_anchor("三亚住个度假酒店，躺平三天")["anchor"] == "stay"


def test_detect_default_sight():
    assert detect_anchor("杭州两日游，想看西湖和灵隐寺")["anchor"] == "sight"


def test_negation_keeps_sight():
    assert detect_anchor("不为了吃，主要看风景")["anchor"] == "sight"


def test_clean_meals_grounds_title():
    evidence = {"d1-p2": {"items": [{"name": "楼外楼"}]}}
    meals = clean_meals([{"type": "lunch", "title": "随便一家", "evidence_id": "d1-p2"}], evidence)
    assert meals and meals[0]["title"] == "楼外楼" and meals[0]["start"] == "12:30"


def test_clean_meals_drops_unknown_evidence():
    assert clean_meals([{"title": "楼外楼", "evidence_id": "d9-p9"}], {}) == []


def test_clean_lodging():
    assert clean_lodging(" 西湖边 ") == "西湖边"
    assert clean_lodging("未核实") == ""


def test_meal_grounded_check():
    result = {
        "status": "complete",
        "constraints": {},
        "evidence": [{"id": "d1-p1", "status": "ok", "items": [{"name": "西湖"}]},
                     {"id": "d1-p2", "status": "ok", "items": [{"name": "楼外楼"}]}],
        "days": [{
            "day": 1, "city": "杭州",
            "activities": [{"title": "西湖", "start": "09:30", "duration": "2 小时",
                            "transport": "交通未核实", "cost": "未核实", "evidence_id": "d1-p1"}],
            "meals": [{"type": "lunch", "title": "楼外楼", "start": "12:30", "evidence_id": "d1-p2"}],
        }],
    }
    assert check_result({"query": "杭州两日游", "days": 1, "cities": ["杭州"]}, result)["meal_grounded"] is True

    result["days"][0]["meals"][0]["title"] = "假的餐馆"
    assert check_result({"query": "杭州两日游", "days": 1, "cities": ["杭州"]}, result)["meal_grounded"] is False
