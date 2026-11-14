import unittest

from product.names import title_matches, clean_place_name, dedupe_names, looks_like_place
from product.orchestrator import _clean_activities


class CleanPlaceNameTests(unittest.TestCase):
    def test_strips_site_suffix(self):
        self.assertEqual(clean_place_name("杭州西湖边咖啡店｜Trip.com 杭州"), "杭州西湖边咖啡店")
        self.assertEqual(clean_place_name("西湖 - 马蜂窝"), "西湖")

    def test_strips_column_words(self):
        self.assertEqual(
            clean_place_name("杭州龙井村游玩攻略简介,杭州龙井村门票/地址/图片/开放时间/照片/门票价格【携程攻略】 - 预订"),
            "杭州龙井村",
        )
        self.assertEqual(clean_place_name("九溪烟树门票价格"), "九溪烟树")

    def test_keeps_plain_name(self):
        self.assertEqual(clean_place_name("青芝坞"), "青芝坞")
        self.assertEqual(clean_place_name("杭州西湖风景名胜区"), "杭州西湖风景名胜区")

    def test_empty_falls_back_to_title(self):
        self.assertEqual(clean_place_name(""), "")
        self.assertTrue(clean_place_name("｜｜"))


class LooksLikePlaceTests(unittest.TestCase):
    def test_short_name_is_place(self):
        self.assertTrue(looks_like_place("青芝坞", "西湖", "杭州"))

    def test_long_article_title_is_not(self):
        self.assertFalse(looks_like_place("讲好西湖故事系列：新西湖十景之九溪烟树", "西湖", "杭州"))

    def test_keyword_hit_is_place(self):
        self.assertTrue(looks_like_place("杭州龙井村", "龙井", "杭州"))


class DedupeTests(unittest.TestCase):
    def test_removes_same_names(self):
        items = [{"name": "西湖", "url": "a"}, {"name": "西湖", "url": "b"}, {"name": "灵隐寺"}]
        self.assertEqual([i["name"] for i in dedupe_names(items)], ["西湖", "灵隐寺"])


class CleanActivitiesTests(unittest.TestCase):
    def test_fills_defaults_and_limits(self):
        activities = _clean_activities([
            {"title": "西湖", "evidence_id": "d1-p1"},
            {"title": "灵隐寺", "evidence_id": "d1-p2", "start": "14:00", "cost": "45 元"},
            {"title": "龙井村", "evidence_id": "d1-p3"},
            {"title": "被截断", "evidence_id": "d1-p4"},
        ])
        self.assertEqual(len(activities), 3)
        self.assertEqual(activities[0]["start"], "09:30")
        self.assertEqual(activities[1]["start"], "14:00")
        self.assertEqual(activities[2]["start"], "16:30")
        self.assertEqual(activities[1]["cost"], "45 元")
        self.assertEqual(activities[0]["transport"], "交通未核实")

    def test_prefers_display_title(self):
        activities = _clean_activities([{"title": "很长的证据标题", "display_title": "湖滨咖啡馆"}])
        self.assertEqual(activities[0]["title"], "湖滨咖啡馆")

    def test_invalid_start_falls_back(self):
        activities = _clean_activities([{"title": "西湖", "start": "上午"}])
        self.assertEqual(activities[0]["start"], "09:30")


class TitleMatchTests(unittest.TestCase):
    def test_exact_and_contained(self):
        self.assertTrue(title_matches("龙井村", ["杭州龙井村", "龙井村"]))
        self.assertTrue(title_matches("西湖苏堤段", ["西湖", "杭州西湖风景名胜区"]))

    def test_weak_overlap_is_rejected(self):
        self.assertFalse(title_matches("西湖景区咖啡馆", ["西湖边文艺咖啡馆探店指南"]))

    def test_unrelated_title_rejected(self):
        self.assertFalse(title_matches("外滩夜景", ["杭州西湖风景名胜区"]))


if __name__ == "__main__":
    unittest.main()


class CityLoadTests(unittest.TestCase):
    def test_one_city_per_day(self):
        from product.orchestrator import cities_fit_days
        self.assertTrue(cities_fit_days(["杭州"], 1))
        self.assertTrue(cities_fit_days(["杭州", "苏州"], 2))
        self.assertFalse(cities_fit_days(["北京", "上海", "成都", "广州", "西安"], 3))
        self.assertFalse(cities_fit_days(["杭州", "苏州"], 1))


class IntakeGuardTests(unittest.TestCase):
    def test_declares_duration(self):
        from product.orchestrator import _declares_duration
        self.assertTrue(_declares_duration("去杭州玩两天"))
        self.assertTrue(_declares_duration("周末去苏州"))
        self.assertTrue(_declares_duration("一日游"))
        self.assertFalse(_declares_duration("想去北京玩，喜欢博物馆"))

    def test_budget_conflict(self):
        from product.orchestrator import budget_conflict
        self.assertTrue(budget_conflict("人均预算五百还要住五星酒店", 2))
        self.assertFalse(budget_conflict("预算 8000 想住五星", 2))
        self.assertFalse(budget_conflict("预算 500 住青旅", 2))


class FinalizeDaysTests(unittest.TestCase):
    def test_dedupes_and_sorts(self):
        from product.orchestrator import _finalize_days
        days = [{"day": 1, "city": "杭州", "activities": [
            {"title": "B", "start": "14:00"}, {"title": "A", "start": "09:00"}, {"title": "A", "start": "10:00"}]}]
        out = _finalize_days(days, [], [{"day": 1, "city": "杭州"}])
        self.assertEqual([a["title"] for a in out[0]["activities"]], ["A", "B"])

    def test_fills_from_evidence(self):
        from product.orchestrator import _finalize_days
        good = [{"id": "d1-p1", "status": "ok", "items": [{"name": "九溪烟树"}]}]
        out = _finalize_days([{"day": 1, "city": "杭州", "activities": []}], good, [{"day": 1, "city": "杭州"}])
        self.assertEqual(out[0]["activities"][0]["title"], "九溪烟树")


def test_emoji_label_is_not_a_place_name():
    from product.names import clean_place_name
    assert clean_place_name("📍定位") == ""
