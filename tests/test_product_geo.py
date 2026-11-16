import unittest

from product.geo import haversine_km, order_nearest, parse_coord, route_km, transit_minutes
from product.postprocess import _schedule_day


class GeoTests(unittest.TestCase):
    def test_parse_and_distance(self):
        suzhou = parse_coord({"coordinates": "120.6196,31.3175"})
        self.assertIsNotNone(suzhou)
        self.assertIsNone(parse_coord({"coordinates": "bad"}))
        near = parse_coord({"coordinates": "120.6274,31.3231"})
        self.assertGreater(haversine_km(suzhou, near), 0.5)
        self.assertGreater(route_km(suzhou, near), haversine_km(suzhou, near))

    def test_transit_prefers_walking_then_bike_then_car(self):
        short = transit_minutes((31.3175, 120.6196), (31.3180, 120.6200))
        mid = transit_minutes((31.3175, 120.6196), (31.3300, 120.6300))
        far = transit_minutes((31.3175, 120.6196), (31.4000, 120.7000))
        self.assertLess(short, mid)
        self.assertLess(mid, far)

    def test_order_nearest_clusters_points(self):
        acts = [
            {"title": "虎丘", "start": "09:00", "coordinates": "120.5750,31.3390"},
            {"title": "拙政园", "start": "10:00", "coordinates": "120.6256,31.3240"},
            {"title": "狮子林", "start": "13:00", "coordinates": "120.6280,31.3230"},
        ]
        self.assertEqual([a["title"] for a in order_nearest(acts)], ["虎丘", "拙政园", "狮子林"])

    def test_schedule_inserts_transit(self):
        acts = [
            {"title": "虎丘", "start": "09:00", "duration": "2 小时", "coordinates": "120.5750,31.3390"},
            {"title": "拙政园", "start": "10:00", "duration": "2 小时", "coordinates": "120.6256,31.3240"},
        ]
        result = _schedule_day(acts, 6.0)
        self.assertGreater(result[1]["start"], "11:00")
        self.assertIn("分钟", result[1]["transport"])

    def test_schedule_drops_last_when_window_overflows(self):
        acts = [
            {"title": "A", "start": "09:00", "duration": "3 小时", "coordinates": "120.5750,31.3390"},
            {"title": "B", "start": "12:00", "duration": "3 小时", "coordinates": "120.6300,31.3200"},
            {"title": "C", "start": "16:00", "duration": "3 小时", "coordinates": "120.7000,31.3000"},
        ]
        self.assertLessEqual(len(_schedule_day(acts, 6.0)), 2)


if __name__ == "__main__":
    unittest.main()
