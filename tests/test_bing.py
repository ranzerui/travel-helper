"""必应数据源测试：用固定的 RSS 样本，不联网。"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CITY_FIXTURE = Path(__file__).with_name("fixtures") / "成都.json"



from travel_planner import bing, data, sources

FIXTURE = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0"><channel>
  <title>必应：成都景点</title>
  <item>
    <title>成都旅游攻略：值得去的景点</title>
    <link>https://example-article.test/chengdu</link>
    <description>推荐去杜甫草堂、武侯祠和宽窄巷子，开放时间多为 09:00-18:00。</description>
  </item>
  <item>
    <title>成都博物馆免费开放</title>
    <link>https://example-article.test/museum</link>
    <description>成都博物馆位于天府广场，周一闭馆，门票免费。</description>
  </item>
  <item>
    <title>北京景点推荐</title>
    <link>https://example-article.test/beijing</link>
    <description>故宫、天坛、颐和园。</description>
  </item>
</channel></rss>
"""


class ParseRssTest(unittest.TestCase):
    def test_parses_items(self):
        items = bing.parse_rss(FIXTURE)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0]["title"], "成都旅游攻略：值得去的景点")
        self.assertEqual(items[0]["link"], "https://example-article.test/chengdu")
        self.assertIn("杜甫草堂", items[0]["description"])

    def test_invalid_xml_returns_empty(self):
        self.assertEqual(bing.parse_rss("<html>不是RSS</html>"), [])

    def test_match_spots_uses_dictionary(self):
        text = "推荐去杜甫草堂、武侯祠和宽窄巷子，成都博物馆免费。"
        names = ["杜甫草堂", "武侯祠", "宽窄巷子", "成都博物馆", "青羊宫"]
        self.assertEqual(bing.match_spots(text, names),
                         ["杜甫草堂", "武侯祠", "宽窄巷子", "成都博物馆"])

    def test_match_spots_ignores_bracket_suffix(self):
        """匹配时去掉括号补充说明，但返回的是原始名称。"""
        self.assertEqual(bing.match_spots("杜甫草堂值得去", ["杜甫草堂（青羊区）"]), ["杜甫草堂（青羊区）"])

    def test_search_rss_collects_hits(self):
        with mock.patch.object(bing, "fetch_html", lambda url, timeout=20: FIXTURE):
            result = bing.search_rss("成都 景点", ["杜甫草堂", "青羊宫"])
        self.assertEqual(result.matched, ["杜甫草堂"])
        self.assertEqual(len(result.titles), 3)
        self.assertIn("format=rss", result.url)


class BingSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.local = data.load_file(CITY_FIXTURE)
        cls._patch_load = mock.patch.object(data, "load_city", lambda city: cls.local)
        cls._patch_load.start()

    @classmethod
    def tearDownClass(cls):
        cls._patch_load.stop()

    def test_marks_mentioned_and_unmentioned(self):
        result = bing.BingResult(query="q", url="https://cn.bing.com/search?q=q&format=rss",
                                 titles=["t"], matched=["杜甫草堂"], text="")
        with mock.patch.object(bing, "search_rss", lambda *a, **k: result):
            spots = sources.BingSource(extra_queries=0).fetch("成都")
        by_name = {spot.name: spot for spot in spots}
        self.assertIn("必应搜索提及", by_name["杜甫草堂"].source_name)
        self.assertTrue(by_name["杜甫草堂"].source_url.startswith("https://cn.bing.com"))
        self.assertIn("未核实", by_name["青羊宫"].source_name)
        self.assertEqual(by_name["青羊宫"].source_url, "")
        # 开放时间仍然来自本地核实值
        self.assertEqual(by_name["杜甫草堂"].open_time, "09:00")
        self.assertEqual(by_name["杜甫草堂"].ticket, 50.0)

    def test_updates_captured_at_only_for_mentioned(self):
        result = bing.BingResult(query="q", url="https://cn.bing.com/x", titles=[], matched=["武侯祠"], text="")
        with mock.patch.object(bing, "search_rss", lambda *a, **k: result):
            spots = sources.BingSource(extra_queries=0).fetch("成都")
        by_name = {spot.name: spot for spot in spots}
        self.assertNotEqual(by_name["武侯祠"].captured_at, by_name["青羊宫"].captured_at)

    def test_bing_source_registered_in_build_source(self):
        self.assertIsInstance(sources.build_source("bing"), sources.BingSource)


if __name__ == "__main__":
    unittest.main(verbosity=2)
