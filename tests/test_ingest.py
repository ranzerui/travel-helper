"""ingest：从抓取结果生成城市数据文件的测试（不联网）。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import bing, data, ingest

TMP = Path(__file__).with_name("_tmp_ingest")

SAMPLE_TEXT = (
    "杭州必去景点：西湖、灵隐寺、西溪湿地，交通方便。\n"
    "杭州旅游推荐：西湖、灵隐寺、河坊街，门票另计。\n"
    "杭州三日游：西湖、宋城、雷峰塔，适合亲子。\n"
    "杭州攻略大全 2026 最新版\n"
)


class CandidateTest(unittest.TestCase):
    def setUp(self):
        # 一条搜索结果当作文本单元：真实场景里每个查询产出一段
        self.texts = SAMPLE_TEXT.strip().splitlines()

    def test_extracts_repeated_names(self):
        """至少两条结果里都出现的才留下来（共现过滤）。"""
        names = ingest.candidate_names(self.texts, min_hits=2)
        self.assertIn("西湖", names)
        self.assertIn("灵隐寺", names)

    def test_single_mention_is_dropped(self):
        names = ingest.candidate_names(self.texts, min_hits=2)
        self.assertNotIn("西溪湿地", names, "只出现在一条结果里的不算")

    def test_filters_article_titles_and_stop_words(self):
        names = ingest.candidate_names(self.texts, min_hits=1)
        joined = "".join(names)
        for bad in ("攻略", "大全", "推荐", "门票", "交通", "旅游"):
            self.assertNotIn(bad, joined)

    def test_single_occurrence_is_dropped(self):
        names = ingest.candidate_names(["只出现一次的名字：孤山"], min_hits=2)
        self.assertNotIn("孤山", names)


class BuildFileTest(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        for item in TMP.glob("*.json"):
            item.unlink()

    def test_build_spots_marks_unverified(self):
        spots = ingest.build_spots("杭州", ["西湖", "灵隐寺"], "必应搜索（杭州）", "https://cn.bing.com/x")
        self.assertEqual(len(spots), 2)
        for spot in spots:
            self.assertFalse(spot["verified"])
            self.assertEqual(spot["open_time"], "09:00")
            self.assertEqual(spot["ticket"], 0.0)
            self.assertTrue(spot["id"].startswith("scraped_"))

    def test_write_then_load_roundtrip(self):
        target = TMP / "hangzhou.json"
        spots = ingest.build_spots("杭州", ["西湖", "灵隐寺"], "必应搜索（杭州）", "https://cn.bing.com/x")
        ingest.write_city_file("杭州", spots, [{"query": "杭州 景点", "url": "u", "titles": []}], path=target)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(payload["kind"], "scraped")
        self.assertFalse(payload["verified"])
        self.assertEqual(len(payload["spots"]), 2)

        with mock.patch.object(data, "DATA_DIR", TMP):
            loaded = data.load_city("杭州")
        self.assertEqual(len(loaded), 2)
        self.assertFalse(loaded[0].verified)
        self.assertEqual(loaded[0].name, "西湖")

    def test_build_for_city_with_fake_bing(self):
        target = TMP / "fake_city.json"
        result = bing.BingResult(query="q", url="https://cn.bing.com/x", titles=[], matched=[], text=SAMPLE_TEXT * 2)
        with mock.patch.object(bing, "search_rss", lambda *a, **k: result):
            report = ingest.build_for_city("杭州", limit=5, path=target)
        self.assertEqual(report["count"], 5)
        self.assertTrue(target.exists())
        self.assertEqual(len(report["queries"]), 3)

    def test_empty_result_raises(self):
        result = bing.BingResult(query="q", url="u", titles=[], matched=[], text="")
        with mock.patch.object(bing, "search_rss", lambda *a, **k: result):
            with self.assertRaises(RuntimeError):
                ingest.build_for_city("杭州", path=TMP / "empty.json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
