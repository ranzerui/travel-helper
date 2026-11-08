"""交通时长提供者的测试：缓存、回退、来源标记（不发真实请求）。"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import amap, routes
from travel_planner.models import Spot

TMP = Path(__file__).with_name("_tmp_routes")

A = Spot(id="a", name="甲", city="测试", category="人文", lat=30.6598, lon=104.0298,
         open_time="09:00", close_time="18:00", duration_min=60)
B = Spot(id="b", name="乙", city="测试", category="人文", lat=30.6459, lon=104.0458,
         open_time="09:00", close_time="18:00", duration_min=60)
FAR = Spot(id="c", name="丙", city="测试", category="自然", lat=31.2, lon=104.9,
           open_time="09:00", close_time="18:00", duration_min=60)


class EstimateTest(unittest.TestCase):
    def test_estimate_provider_matches_simulator(self):
        provider = routes.EstimateProvider()
        minutes, source = provider.minutes(A, B)
        self.assertEqual(source, "估算")
        self.assertGreater(minutes, 0)


class AmapProviderTest(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        for item in TMP.glob("*"):
            item.unlink()

    def _provider(self):
        return routes.AmapProvider(cache_path=TMP / "routes.json")

    def test_uses_api_then_cache(self):
        calls = {"count": 0}

        def fake_get(path, params, timeout=20):
            calls["count"] += 1
            return {"status": "1", "route": {"paths": [{"cost": {"duration": "900"}}]}}

        provider = self._provider()
        with mock.patch.object(amap, "_get", fake_get):
            first = provider.minutes(A, B)
            second = provider.minutes(A, B)
        self.assertEqual(calls["count"], 1, "第二次应命中缓存")
        self.assertEqual(first[0], 15)          # 900 秒 = 15 分钟
        self.assertIn("高德", first[1])
        self.assertIn("缓存", second[1])

    def test_far_route_uses_driving(self):
        seen = {}

        def fake_get(path, params, timeout=20):
            seen["path"] = path
            return {"status": "1", "route": {"paths": [{"cost": {"duration": "3600"}}]}}

        provider = self._provider()
        with mock.patch.object(amap, "_get", fake_get):
            minutes, source = provider.minutes(A, FAR)
        self.assertIn("driving", seen["path"])
        self.assertEqual(minutes, 60)
        self.assertIn("驾车", source)

    def test_falls_back_on_api_error(self):
        def boom(*args, **kwargs):
            raise amap.AmapError("限流")

        provider = self._provider()
        with mock.patch.object(amap, "_get", boom):
            minutes, source = provider.minutes(A, B)
        self.assertIn("回退", source)
        self.assertGreater(minutes, 0)
        self.assertEqual(provider.stats["fallback"], 1)

    def test_falls_back_when_response_missing_duration(self):
        def fake_get(path, params, timeout=20):
            return {"status": "1", "route": {"paths": [{"cost": {}}]}}

        provider = self._provider()
        with mock.patch.object(amap, "_get", fake_get):
            minutes, source = provider.minutes(A, B)
        self.assertIn("回退", source)

    def test_cache_file_persisted(self):
        def fake_get(path, params, timeout=20):
            return {"status": "1", "route": {"paths": [{"cost": {"duration": "600"}}]}}

        provider = self._provider()
        with mock.patch.object(amap, "_get", fake_get):
            provider.minutes(A, B)
        payload = json.loads((TMP / "routes.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["routes"])
        # 新实例应读到同一份缓存
        fresh = self._provider()
        with mock.patch.object(amap, "_get", side_effect=AssertionError("不该再请求")):
            minutes, source = fresh.minutes(A, B)
        self.assertIn("缓存", source)


class BuildProviderTest(unittest.TestCase):
    def test_estimate_when_offline(self):
        self.assertIsInstance(routes.build_provider("auto", allow_network=False),
                              routes.EstimateProvider)

    def test_estimate_when_no_key(self):
        with mock.patch("travel_planner.settings.amap_key", return_value=None):
            self.assertIsInstance(routes.build_provider("amap", allow_network=True),
                                  routes.EstimateProvider)

    def test_amap_when_key_present(self):
        with mock.patch("travel_planner.settings.amap_key", return_value="test-key"):
            self.assertIsInstance(routes.build_provider("auto", allow_network=True),
                                  routes.AmapProvider)


if __name__ == "__main__":
    unittest.main(verbosity=2)
