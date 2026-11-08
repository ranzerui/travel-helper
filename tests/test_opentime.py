"""高德 business.opentime_week 解析测试：这是我们拿到"真实开放时间"的关键。"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import amap

SEASONAL = ("03-16至04-30 周一至周日 08:00-19:00 最晚进入18:30；"
            "05-01至10-31 周一至周日 08:00-20:00 最晚进入19:30；"
            "11-01至03-15 周一至周日 08:00-17:30 最晚进入17:00")
SIMPLE = "周一至周日 09:00-17:00"


class ParseOpentimeTest(unittest.TestCase):
    def test_picks_season_for_today(self):
        winter = amap.parse_opentime({"opentime_week": SEASONAL}, today=date(2026, 11, 5))
        self.assertEqual((winter["open_time"], winter["close_time"]), ("08:00", "17:30"))
        summer = amap.parse_opentime({"opentime_week": SEASONAL}, today=date(2026, 7, 1))
        self.assertEqual((summer["open_time"], summer["close_time"]), ("08:00", "20:00"))

    def test_simple_range(self):
        result = amap.parse_opentime({"opentime_week": SIMPLE})
        self.assertEqual(result["open_time"], "09:00")
        self.assertEqual(result["close_time"], "17:00")
        self.assertTrue(result["raw"])

    def test_falls_back_to_opentime_today(self):
        result = amap.parse_opentime({"opentime_today": "08:30-18:00"})
        self.assertEqual(result["open_time"], "08:30")

    def test_empty_returns_nothing(self):
        result = amap.parse_opentime({})
        self.assertIsNone(result["open_time"])
        self.assertIsNone(result["close_time"])

    def test_garbage_returns_nothing(self):
        self.assertIsNone(amap.parse_opentime({"opentime_week": "全天开放"})["open_time"])
        self.assertIsNone(amap.parse_opentime({"opentime_week": "25:00-26:00"})["open_time"])


class SpotRecordsTest(unittest.TestCase):
    def test_uses_poi_open_time(self):
        pois = [{
            "name": "雷峰塔", "lat": 30.2, "lon": 120.1, "address": "南山路",
            "category": "景点", "typecode": "110202", "full_name": "雷峰塔景区",
            "open_time": "08:00", "close_time": "17:30", "opentime_raw": SEASONAL,
            "rating": "4.8", "tel": "0571-1",
        }]
        records = amap.to_spot_records(pois, "杭州")
        self.assertEqual(records[0]["open_time"], "08:00")
        self.assertEqual(records[0]["close_time"], "17:30")
        self.assertFalse(records[0]["verified"], "高德的营业时间也不算人工核实")
        self.assertIn("营业时间", records[0]["source_name"])

    def test_defaults_when_missing(self):
        pois = [{"name": "某公园", "lat": 0, "lon": 0, "address": "", "category": "公园",
                 "typecode": "110101", "full_name": "某公园", "open_time": None,
                 "close_time": None, "opentime_raw": "", "rating": "", "tel": ""}]
        records = amap.to_spot_records(pois, "杭州")
        self.assertEqual(records[0]["open_time"], "09:00")
        self.assertNotIn("营业时间", records[0]["source_name"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
