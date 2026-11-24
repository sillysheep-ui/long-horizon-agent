"""锚定用例集与 anchor_satisfied 检查。"""

import json
from pathlib import Path

from evaluation.product_checks import check_result

ALL_CASES = json.loads(Path(__file__).resolve().parents[1].joinpath("evaluation/product_cases.json").read_text())
CASES = [case for case in ALL_CASES if "anchor" in (case.get("tags") or [])]


def test_anchor_cases_shape():
    assert len(CASES) == 10
    assert len({case["id"] for case in CASES}) == 10
    counts = {"food": 0, "stay": 0, "sight": 0}
    for case in CASES:
        counts[case["anchor"]] += 1
    assert counts == {"food": 4, "stay": 3, "sight": 3}


def _result(meals=None, lodging=""):
    day = {"day": 1, "city": "成都", "activities": [
        {"title": "宽窄巷子", "start": "09:30", "duration": "2 小时",
         "transport": "交通未核实", "cost": "未核实", "evidence_id": "d1-p1"}]}
    if meals:
        day["meals"] = meals
    if lodging:
        day["lodging_area"] = lodging
    return {"status": "complete", "constraints": {}, "days": [day],
            "evidence": [{"id": "d1-p1", "status": "ok", "items": [{"name": "宽窄巷子"}]}]}


def test_anchor_food_requires_meal():
    case = {"query": "成都吃两天", "days": 1, "cities": ["成都"], "anchor": "food"}
    assert check_result(case, _result())["anchor_satisfied"] is False
    meals = [{"type": "lunch", "title": "蜀大侠火锅", "start": "12:30", "evidence_id": "d1-p1"}]
    assert check_result(case, _result(meals=meals))["anchor_satisfied"] is True


def test_anchor_stay_requires_lodging():
    case = {"query": "三亚躺两天", "days": 1, "cities": ["成都"], "anchor": "stay"}
    assert check_result(case, _result())["anchor_satisfied"] is False
    assert check_result(case, _result(lodging="春熙路商圈"))["anchor_satisfied"] is True
