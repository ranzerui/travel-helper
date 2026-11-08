"""高德 POI 数据源的离线测试（不发真实请求）。"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import amap

TMP = Path(__file__).with_name("_tmp_amap")

RAW_POIS = [
    {"name": "杭州西湖风景名胜区-断桥残雪", "location": "120.148,30.259",
     "typecode": "110202", "address": "北山街", "adname": "西湖区"},
    {"name": "吴山景区", "location": "120.168,30.240", "typecode": "110200",
     "address": "吴山广场", "adname": "上城区"},
    {"name": "武侯祠(地铁站)", "location": "104.051,30.645", "typecode": "150500",
     "address": "", "adname": "武侯区"},
    {"name": "某景点停车场", "location": "120.1,30.2", "typecode": "150900",
     "address": "", "adname": "西湖区"},
    {"name": "浙江博物馆", "location": "120.15,30.25", "typecode": "140100",
     "address": "孤山路", "adname": "西湖区"},
    {"name": "吴山景区", "location": "120.168,30.240", "typecode": "110200",
     "address": "吴山广场", "adname": "上城区"},
]


class HelperTest(unittest.TestCase):
    def test_filters_transit_and_parking(self):
        self.assertFalse(amap._is_usable(RAW_POIS[2]))   # 地铁站
        self.assertFalse(amap._is_usable(RAW_POIS[3]))   # 停车场
        self.assertTrue(amap._is_usable(RAW_POIS[0]))

    def test_short_name_strips_scenic_prefix(self):
        self.assertEqual(amap._short_name("杭州西湖风景名胜区-断桥残雪"), "断桥残雪")
        self.assertEqual(amap._short_name("吴山景区"), "吴山景区")

    def test_location_parsed_to_lat_lon(self):
        lat, lon = amap._location(RAW_POIS[0])
        self.assertAlmostEqual(lat, 30.259, places=3)
        self.assertAlmostEqual(lon, 120.148, places=3)

    def test_location_invalid_returns_zero(self):
        self.assertEqual(amap._location({"location": ""}), (0.0, 0.0))


class DiscoverTest(unittest.TestCase):
    def test_merge_dedupes_and_shortens(self):
        def fake_search(city, keywords="", types="", page_size=20, page=1,
                        timeout=20, sortrule="weight"):
            return RAW_POIS

        with mock.patch.object(amap, "search_pois", fake_search):
            pois = amap.discover("杭州")
        names = [poi["name"] for poi in pois]
        self.assertIn("断桥残雪", names)
        self.assertIn("吴山景区", names)
        self.assertIn("浙江博物馆", names)
        self.assertEqual(names.count("吴山景区"), 1, "重名要去掉")
        self.assertNotIn("某景点停车场", names)
        self.assertNotIn("武侯祠(地铁站)", names)

    def test_to_spot_records_marks_unverified(self):
        pois = [
            {"name": "断桥残雪", "lat": 30.259, "lon": 120.148, "address": "北山街",
             "category": "景点", "typecode": "110202"},
            {"name": "西湖博物馆", "lat": 30.25, "lon": 120.15, "address": "孤山路",
             "category": "博物馆", "typecode": "140100"},
        ]
        records = amap.to_spot_records(pois, "杭州")
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertFalse(record["verified"])
            self.assertEqual(record["open_time"], "09:00")
            self.assertTrue(record["id"].startswith("amap_"))
        self.assertEqual(records[0]["category"], "人文")
        self.assertEqual(records[1]["category"], "人文")


class CacheTest(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        for item in TMP.glob("*.json"):
            item.unlink()

    def test_second_call_hits_cache(self):
        calls = {"count": 0}

        def fake_discover(city, **kwargs):
            calls["count"] += 1
            return [{"name": "断桥残雪", "lat": 30.2, "lon": 120.1, "address": "",
                     "category": "景点", "typecode": "110202"}]

        with mock.patch.object(amap, "discover", fake_discover), \
             mock.patch.object(amap, "CACHE_DIR", TMP):
            first = amap.cached_discover("杭州")
            second = amap.cached_discover("杭州")
        self.assertEqual(calls["count"], 1, "第二次应命中缓存")
        self.assertEqual(first, second)
        self.assertTrue((TMP / "amap_杭州.json").exists())

    def test_force_refresh_bypasses_cache(self):
        calls = {"count": 0}

        def fake_discover(city, **kwargs):
            calls["count"] += 1
            return [{"name": f"景点{calls['count']}", "lat": 0, "lon": 0, "address": "",
                     "category": "景点", "typecode": "110000"}]

        with mock.patch.object(amap, "discover", fake_discover), \
             mock.patch.object(amap, "CACHE_DIR", TMP):
            amap.cached_discover("杭州")
            refreshed = amap.cached_discover("杭州", force=True)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(refreshed[0]["name"], "景点2")


class KeyTest(unittest.TestCase):
    def test_missing_key_raises(self):
        # .env 里可能有真 key，这里显式屏蔽
        with mock.patch("travel_planner.settings.amap_key", return_value=None):
            with self.assertRaises(amap.AmapError):
                amap.api_key()

    def test_env_file_is_loaded(self):
        from travel_planner import settings

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AMAP_KEY", None)
            with mock.patch.object(settings, "ENV_FILE", TMP / ".env"):
                (TMP / ".env").write_text("AMAP_KEY=from-env-file\n", encoding="utf-8")
                self.assertEqual(settings.amap_key(), "from-env-file")


if __name__ == "__main__":
    unittest.main(verbosity=2)
