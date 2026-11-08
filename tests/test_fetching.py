"""抓取结果解析与合并的测试：不需要浏览器。"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CITY_FIXTURE = Path(__file__).with_name("fixtures") / "成都.json"



from travel_planner import data, fetching


class ParseAnswerTest(unittest.TestCase):
    def test_parses_spots_object(self):
        records = fetching.parse_answer('{"spots":[{"name":"杜甫草堂","desc":"诗圣故居"}]}')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "杜甫草堂")

    def test_parses_bare_list(self):
        records = fetching.parse_answer('[{"name":"武侯祠"},{"name":"青羊宫"}]')
        self.assertEqual(len(records), 2)

    def test_parses_fenced_json_with_prose(self):
        answer = '我已经看完了页面，结果如下：\n```json\n{"spots":[{"name":"宽窄巷子"}]}\n```\n以上。'
        records = fetching.parse_answer(answer)
        self.assertEqual(len(records), 1)

    def test_garbage_returns_empty(self):
        self.assertEqual(fetching.parse_answer("抱歉，我无法完成这个任务。"), [])
        self.assertEqual(fetching.parse_answer(""), [])

    def test_filters_records_without_name(self):
        records = fetching.parse_answer('{"spots":[{"desc":"没有名字"},{"name":"  "},{"name":"金沙遗址"}]}')
        self.assertEqual([item["name"] for item in records], ["金沙遗址"])


class ToRecordsTest(unittest.TestCase):
    def test_article_titles_are_filtered_out(self):
        """搜索页给的是文章标题，不能当成景点。"""
        answer = ('{"spots":[{"name":"成都必打卡的景点"},{"name":"逛遍成都23个景区后..."},'
                  '{"name":"成都游玩15个必去的好玩景点"},{"name":"杜甫草堂"}]}')
        records, notes = fetching.records_from_answer(
            answer, "成都", "必应（抓取）", "https://cn.bing.com/x", "2026-11-05", local_spots=[])
        self.assertEqual([item["name"] for item in records], ["杜甫草堂"])
        self.assertTrue(any("文章标题" in note for note in notes))

    def test_truncated_url_is_not_trusted(self):
        answer = '{"spots":[{"name":"武侯祠","url":"https://zhuanlan.zhihu.com..."}]}'
        records = fetching.parse_answer(answer)
        converted = fetching.to_spot_records(records, "成都", "必应（抓取）", "https://cn.bing.com/x", "2026-11-05")
        self.assertFalse(converted[0]["link_verified"])
        self.assertEqual(converted[0]["source_url"], "https://cn.bing.com/x")

    def test_accepts_alternate_key_names(self):
        """模型常用 title/description/link 这套键名，不能只认 name/desc/url。"""
        answer = '{"results":[{"title":"杜甫草堂","description":"诗圣故居","link":"https://www.cddfct.com/"}]}'
        records = fetching.parse_answer(answer)
        self.assertEqual(len(records), 1)
        converted = fetching.to_spot_records(records, "成都", "必应（抓取）", "https://cn.bing.com/x", "2026-11-05")
        self.assertEqual(converted[0]["name"], "杜甫草堂")
        self.assertEqual(converted[0]["desc"], "诗圣故居")
        self.assertEqual(converted[0]["source_url"], "https://www.cddfct.com/")
        self.assertTrue(converted[0]["link_verified"])

    def test_fabricated_url_falls_back_to_source_page(self):
        answer = '{"spots":[{"name":"成都博物馆","url":"https://example.com/5"}]}'
        records = fetching.parse_answer(answer)
        converted = fetching.to_spot_records(records, "成都", "必应（抓取）", "https://cn.bing.com/x", "2026-11-05")
        self.assertFalse(converted[0]["link_verified"])
        self.assertEqual(converted[0]["source_url"], "https://cn.bing.com/x")

    def test_missing_fields_are_none(self):
        raw = fetching.parse_answer('{"spots":[{"name":"人民公园","desc":"茶馆"}]}')
        records = fetching.to_spot_records(raw, "成都", "维基（抓取）", "https://example.org", "2026-11-05")
        self.assertIsNone(records[0]["open_time"])
        self.assertIsNone(records[0]["ticket"])
        self.assertEqual(records[0]["duration_min"], 90)
        self.assertEqual(records[0]["captured_at"], "2026-11-05")

    def test_normalizes_fields(self):
        raw = [{"name": "望江楼公园", "open_time": "08:00-18:00", "ticket": "20", "url": "https://a.example/x"}]
        records = fetching.to_spot_records(raw, "成都", "s", "u", "2026-11-05")
        record = records[0]
        self.assertIsNone(record["open_time"], "区间写法不是合法时间，应当置空交给合并层")
        self.assertEqual(record["ticket"], 20.0)
        self.assertEqual(record["source_url"], "https://a.example/x")
        self.assertTrue(record["id"].startswith("web_"))


class MergeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.local = data.load_file(CITY_FIXTURE)

    def test_matched_name_uses_verified_fields(self):
        raw = [{"name": "杜甫草堂（青羊区）", "desc": "抓取到的简介"}]
        records, notes = fetching.records_from_answer(
            '{"spots":' + str(raw).replace("'", '"') + '}',
            "成都", "维基（抓取）", "https://zh.wikipedia.org/wiki/成都", "2026-11-05",
            local_spots=self.local)
        self.assertEqual(records[0]["ticket"], 50.0)
        self.assertEqual(records[0]["open_time"], "09:00")
        self.assertIn("本地核实", records[0]["source_name"])
        self.assertTrue(any("杜甫草堂" in note for note in notes))

    def test_unmatched_gets_defaults_with_warning(self):
        answer = '{"spots":[{"name":"不存在的景点XYZ"}]}'
        records, notes = fetching.records_from_answer(
            answer, "成都", "维基（抓取）", "https://example.org", "2026-11-05", local_spots=self.local)
        self.assertEqual(records[0]["open_time"], "09:00")
        self.assertEqual(records[0]["close_time"], "18:00")
        self.assertEqual(records[0]["ticket"], 0.0)
        self.assertIn("未核实", records[0]["source_name"])
        self.assertTrue(any("未核实" in note for note in notes))

    def test_normalize_name_handles_brackets_and_spaces(self):
        self.assertEqual(fetching.normalize_name("杜甫草堂（青羊区）"), "杜甫草堂")
        self.assertEqual(fetching.normalize_name(" 宽窄巷子 "), "宽窄巷子")
        self.assertEqual(fetching.normalize_name("杜甫草堂"), fetching.normalize_name("杜甫草堂(西门)"))


class RecordsFromAnswerTest(unittest.TestCase):
    def test_empty_answer_reports_reason(self):
        records, notes = fetching.records_from_answer(
            "无法访问页面", "成都", "s", "u", "2026-11-05", local_spots=[])
        self.assertEqual(records, [])
        self.assertTrue(notes and "没有返回" in notes[0])

    def test_without_local_data_marks_defaults(self):
        records, notes = fetching.records_from_answer(
            '{"spots":[{"name":"青羊宫"}]}', "成都", "s", "u", "2026-11-05", local_spots=None)
        self.assertEqual(len(records), 1)
        self.assertIn("默认值", notes[0])

    def test_limit_is_respected(self):
        answer = '{"spots":[' + ",".join(f'{{"name":"景点{i}"}}' for i in range(12)) + ']}'
        records, _ = fetching.records_from_answer(
            answer, "成都", "s", "u", "2026-11-05", local_spots=[], limit=5)
        self.assertEqual(len(records), 5)


class TaskPromptTest(unittest.TestCase):
    def test_task_contains_url_and_city(self):
        task = fetching.build_task("https://example.org/x", "成都")
        self.assertIn("https://example.org/x", task)
        self.assertIn("成都", task)
        self.assertIn("只输出JSON", task)


if __name__ == "__main__":
    unittest.main(verbosity=2)
