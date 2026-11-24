"""负控制：故意写错的行程必须被对应检查抓住。

每条负例都配一条正例，防止检查退化成“永远失败”。
"""

from __future__ import annotations

from product.constraints.engine import build_view, evaluate
from product.constraints.spec import load_set

CONSTRAINTS = {
    "days": 2,
    "day_windows": [{"day": 1, "hours": 10}, {"day": 2, "hours": 10}],
}
EVIDENCE = [
    {"id": "d1-p1", "status": "ok", "source": "测试",
     "items": [{"name": "西湖", "opentime": "10:00", "endtime": "18:00", "price": "60"}]},
]


def _activity(title="西湖", start="10:30", duration="2 小时", cost="60 元", evidence_id="d1-p1"):
    return {"title": title, "start": start, "duration": duration, "transport": "步行",
            "cost": cost, "evidence_id": evidence_id}


def _day(number=1, activities=None):
    if activities is None:
        activities = [_activity(), _activity(title="灵隐寺", start="13:00")]
    return {"day": number, "city": "杭州", "activities": activities}


def _findings(days, evidence=None, constraints=None, status="complete"):
    result = {"status": status, "days": days, "evidence": evidence or EVIDENCE,
              "constraints": constraints or CONSTRAINTS}
    view = build_view(result, case={"days": 2})
    return {item.id: item for item in evaluate(view, load_set())}


def test_fabricated_cost_is_caught():
    bad = [_day(1, [_activity(cost="128 元"), _activity(start="13:00")])]
    assert _findings(bad)["no_fabricated_cost"].ok is False
    good = [_day(1, [_activity(cost="60 元"), _activity(start="13:00")])]
    assert _findings(good)["no_fabricated_cost"].ok is True


def test_opening_hours_violation_is_caught():
    bad = [_day(1, [_activity(start="09:00"), _activity(start="13:00")])]
    assert _findings(bad)["opening_hours"].ok is False
    good = [_day(1, [_activity(start="10:30"), _activity(start="13:00")])]
    assert _findings(good)["opening_hours"].ok is True


def test_overlap_is_caught():
    bad = [_day(1, [_activity(start="10:30", duration="3 小时"), _activity(start="11:30")])]
    assert _findings(bad)["no_overlap"].ok is False
    good = [_day(1, [_activity(start="10:30", duration="2 小时"), _activity(start="12:45")])]
    assert _findings(good)["no_overlap"].ok is True


def test_day_count_mismatch_is_caught():
    assert _findings([_day(1)])["day_count"].ok is False
    assert _findings([_day(1), _day(2)])["day_count"].ok is True


def test_duplicate_titles_across_days_are_caught():
    bad = [_day(1), _day(2)]
    assert _findings(bad)["unique_titles"].ok is False
    good = [_day(1), _day(2, [_activity(title="雷峰塔"), _activity(title="苏堤", start="13:00")])]
    assert _findings(good)["unique_titles"].ok is True


def test_evidence_id_must_exist_and_be_ok():
    bad = [_day(1, [_activity(evidence_id="d9-p9"), _activity(start="13:00")])]
    assert _findings(bad)["evidence_ok"].ok is False


def test_title_must_match_evidence_name():
    bad = [_day(1, [_activity(title="世外桃源"), _activity(start="13:00")])]
    assert _findings(bad)["title_matches_evidence"].ok is False
    good = [_day(1, [_activity(title="西湖"), _activity(start="13:00")])]
    assert _findings(good)["title_matches_evidence"].ok is True


def test_day_load_over_window_is_caught():
    heavy = [_activity(duration="4 小时"), _activity(start="14:30", duration="4 小时"),
             _activity(start="18:30", duration="4 小时")]
    assert _findings([_day(1, heavy)])["day_load"].ok is False
