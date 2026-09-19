import unittest

from utils.geo import haversine_km, normalize_location, split_locations


class GeoFallbackTest(unittest.TestCase):
    def test_normalizes_coordinate_embedded_in_text(self):
        self.assertEqual(normalize_location("坐标 116.397, 39.918"), "116.397000,39.918000")

    def test_rejects_invalid_coordinate(self):
        with self.assertRaises(ValueError):
            normalize_location("not-a-coordinate")

    def test_haversine_distance(self):
        distance = haversine_km("116.397,39.918", "121.474,31.230")
        self.assertGreater(distance, 1000)
        self.assertLess(distance, 1200)

    def test_splits_multiple_origins(self):
        self.assertEqual(
            split_locations("116.1,39.1|116.2,39.2"),
            ["116.100000,39.100000", "116.200000,39.200000"],
        )


if __name__ == "__main__":
    unittest.main()
