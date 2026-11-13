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
