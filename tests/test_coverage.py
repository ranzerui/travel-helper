"""城市覆盖面：任意城市名都能识别，数据按需生成。"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import data, intent


def citydb_module():
    from travel_planner import citydb

    return citydb


TMP = Path(__file__).with_name("_tmp_coverage")


class CityExtractionTest(unittest.TestCase):
    def test_extracts_arbitrary_cities(self):
        cases = {
            "去广州玩三天": "广州",
            "南京两天，预算800": "南京",
            "成都两天，预算1200，喜欢人文": "成都",
            "去西安出差三天": "西安",
            "重庆两日游，想吃火锅": "重庆",
            "到厦门旅游四天": "厦门",
            # 用户实际遇到的坑：昆明3天 被识别成成都
            "昆明3天": "昆明",
            "昆明 3天": "昆明",
            "3天昆明": "昆明",
            "昆明玩3天": "昆明",
            "昆明逛三天": "昆明",
        }
        for text, expected in cases.items():
            self.assertEqual(intent.extract_city(text), expected, text)

    @mock.patch('travel_planner.citydb.lookup_online', side_effect=lambda name: name == '敦煌')
    def test_muddy_word_orders(self, lookup):
        """语序乱也要认得出（用户反馈的真实输入）。"""
        cases = {
            "三天在昆明玩": "昆明",
            "我下周想去趟敦煌": "敦煌",
            "昆明，三天，预算1500": "昆明",
            "去大理旅游三天喜欢自然": "大理",
            "青岛啤酒节三天": "青岛",
            "预算3000，五天，哈尔滨": "哈尔滨",
            "成都两天，预算1200": "成都",
        }
        for text, expected in cases.items():
            self.assertEqual(intent.extract_city(text), expected, text)

    def test_vague_requests_return_none(self):
        for text in ("景色好就行，三天", "在哪儿都行，两天", "随便找个地方，两天"):
            self.assertIsNone(intent.extract_city(text), text)

    def test_time_expression_is_not_a_city(self):
        """只有天数没有城市时不能把"三天"当城市。"""
        self.assertIsNone(intent.extract_city("三天，预算1200，喜欢自然"))

    def test_adjective_phrase_is_not_a_city(self):
        """用户遇到的坑：'景色好' 被当成城市，还自动生成了一份数据。"""
        self.assertIsNone(intent.extract_city("想去景色好的地方，三天"))
        self.assertIsNone(intent.extract_city("景色好"))
        self.assertIsNone(intent.extract_city("风景美、安静，两天"))

    def test_city_list_validation(self):
        from travel_planner import citydb

        self.assertTrue(citydb.is_city("昆明"))
        self.assertTrue(citydb.is_city("大理"), "大理应能匹配大理白族自治州")
        self.assertFalse(citydb.is_city("景色好"))
        self.assertFalse(citydb.is_city("三天"))

    def test_county_level_city_needs_online_lookup(self):
        """县级市（赤壁/敦煌）不在名录里：不联网时抽不出，联网兜底能确认。"""
        self.assertIsNone(intent.extract_city("赤壁两天", online=False))
        with mock.patch("travel_planner.amap._get", return_value={
            "status": "1",
            "districts": [{"name": "赤壁市", "level": "city"}],
        }):
            with mock.patch.object(citydb_module(), "CHECK_CACHE", TMP / "city_check.json"), \
                 mock.patch.object(citydb_module(), "_CACHE", None):
                self.assertTrue(citydb_module().lookup_online("赤壁"))

    def test_garbage_city_is_refused_before_fetch(self):
        """ensure_city 不能给"景色好"这种词生成数据文件（也不能真的去抓）。"""
        with mock.patch.object(data, "DATA_DIR", TMP), \
             mock.patch("travel_planner.citydb.available", return_value=True), \
             mock.patch("travel_planner.citydb.is_city_anywhere", return_value=False):
            with self.assertRaises(FileNotFoundError) as ctx:
                data.ensure_city("景色好", auto=True)
        self.assertIn("不是城市名", str(ctx.exception))

    def test_garbage_city_refused_even_if_file_exists(self):
        """真实踩过的坑：错误城市被抓过一次后留下 data/风景好.json，
        校验必须在读文件之前，否则它会一直可用。"""
        TMP.mkdir(parents=True, exist_ok=True)
        (TMP / "风景好.json").write_text(json.dumps({
            "city": "风景好", "kind": "scraped",
            "spots": [{"id": "x", "name": "某景点", "city": "风景好", "category": "人文",
                       "lat": 0.0, "lon": 0.0, "open_time": "09:00", "close_time": "18:00",
                       "duration_min": 90, "ticket": 0.0}],
        }, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(data, "DATA_DIR", TMP), \
             mock.patch("travel_planner.citydb.available", return_value=True), \
             mock.patch("travel_planner.citydb.is_city_anywhere", return_value=False):
            with self.assertRaises(FileNotFoundError):
                data.ensure_city("风景好", auto=False)

    def test_vague_input_does_not_become_city(self):
        """用户实际输入：'桃3天，预算2000，去风景好的地方' 不能变成城市"风景好"。"""
        self.assertIsNone(intent.extract_city("桃3天，预算2000，去风景好的地方"))

    def test_model_city_must_appear_in_text(self):
        """模型照抄示例城市（成都）时，以用户原话为准。"""
        from travel_planner import llm

        fake = '{"city":"成都","days":3,"budget":1200,"prefer":"自然","pace":"适中","must_see":[]}'
        original = llm.chat
        llm.chat = lambda *args, **kwargs: fake
        try:
            req = intent.parse_requirements("昆明3天，预算1200，喜欢自然")
            self.assertEqual(req.city, "昆明", "模型说成都但原文是昆明，应该采信原文")
            plain = intent.parse_requirements("三天，预算1200，喜欢自然")
            self.assertEqual(plain.city, "成都", "原文没提城市才用默认值")
        finally:
            llm.chat = original

    def test_no_city_returns_none(self):
        self.assertIsNone(intent.extract_city("预算1500，喜欢人文，两天"))

    def test_requirements_use_extracted_city(self):
        req = intent.parse_requirements("去广州玩三天，预算2000，喜欢美食", use_llm=False)
        self.assertEqual(req.city, "广州")
        self.assertEqual(req.days, 3)
        self.assertEqual(req.prefer, "美食")

    def test_delta_can_switch_to_any_city(self):
        base = intent.Requirements(city="成都", days=2, budget=1200)
        new, changes = intent.apply_delta(base, "改去昆明玩三天")
        self.assertEqual(new.city, "昆明")
        self.assertEqual(new.days, 3)
        self.assertTrue(any("城市" in item for item in changes))


class EnsureCityTest(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        for item in TMP.glob("*.json"):
            item.unlink()

    def test_missing_city_without_auto_raises(self):
        with mock.patch.object(data, "DATA_DIR", TMP):
            with self.assertRaises(FileNotFoundError):
                data.ensure_city("广州", auto=False)

    def test_auto_generates_city_data(self):
        calls = []

        def fake_build(city, limit=12, **kwargs):
            calls.append(city)
            payload = {"city": city, "kind": "scraped", "spots": [{
                "id": "amap_x", "name": "测试景点", "city": city, "category": "人文",
                "lat": 0.0, "lon": 0.0, "open_time": "09:00", "close_time": "18:00",
                "duration_min": 90, "ticket": 0.0, "closed_days": [], "desc": "",
                "source_name": "高德 POI（景点）", "source_url": "", "captured_at": "2026-11-05",
                "verified": False,
            }]}
            (TMP / f"{city}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            return {"city": city, "count": 1, "names": ["测试景点"],
                    "path": str(TMP / f"{city}.json"), "mode": "amap"}

        stages = []
        with mock.patch.object(data, "DATA_DIR", TMP), \
             mock.patch("travel_planner.amap.build_city", fake_build):
            spots = data.ensure_city("广州", auto=True, progress=stages.append)

        self.assertEqual(calls, ["广州"])
        self.assertEqual(len(spots), 1)
        self.assertEqual(spots[0].name, "测试景点")
        self.assertTrue(any("高德" in text for text in stages))

    def test_auto_failure_becomes_file_not_found(self):
        from travel_planner import amap

        def boom(city, limit=12, **kwargs):
            raise amap.AmapError("没有配置 AMAP_KEY 环境变量")

        with mock.patch.object(data, "DATA_DIR", TMP), \
             mock.patch("travel_planner.amap.build_city", boom):
            with self.assertRaises(FileNotFoundError) as ctx:
                data.ensure_city("广州", auto=True)
        self.assertIn("AMAP_KEY", str(ctx.exception))


class WebRefineTest(unittest.TestCase):
    """多轮追问里换城市：不能说"广州3天"还沿用上一版的城市。"""

    def test_refine_switches_city(self):
        from travel_planner import web

        stages = []
        payload = web.run_plan({
            "q": "广州3天", "mode": "offline", "refine": "1",
            "base_city": "成都", "base_days": 2, "base_budget": 1200,
            "base_prefer": "人文", "base_pace": "适中",
            "base_start": "09:00", "base_end": "20:00", "base_must": "",
        }, stages.append)
        self.assertIn("广州", payload["requirements"]["text"])
        self.assertEqual(len(payload["days"]), 3)
        self.assertTrue(any("城市" in text for text in stages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
