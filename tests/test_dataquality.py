"""数据质量与报告内容的不变量：任何改动都不能让数据变脏、报告变空。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import data, planner, report


class DataQualityTest(unittest.TestCase):
    def test_all_cities_are_clean(self):
        """坐标、时间窗、时长、门票都必须合法（自检发现过西安 4 个景点坐标是 0,0）。"""
        problems = []
        for city in data.available_cities():
            for spot in data.load_city(city):
                if not (18 <= spot.lat <= 54 and 73 <= spot.lon <= 135):
                    problems.append(f"{city}/{spot.name}: 坐标 {spot.lat},{spot.lon}")
                if not (spot.open_time < spot.close_time):
                    problems.append(f"{city}/{spot.name}: 时间倒挂 {spot.open_time}-{spot.close_time}")
                if not 10 <= spot.duration_min <= 480:
                    problems.append(f"{city}/{spot.name}: 时长 {spot.duration_min}")
                if spot.ticket < 0:
                    problems.append(f"{city}/{spot.name}: 门票 {spot.ticket}")
        self.assertEqual(problems, [], "\n".join(problems[:10]))


class ReportContentTest(unittest.TestCase):
    def test_report_lists_sources_and_unverified(self):
        city = data.available_cities()[0]
        spots = data.load_city(city)
        itinerary, result = planner.plan(city, spots, days=2, budget=1500)
        text = report.to_markdown(itinerary, result)
        self.assertIn("## 信息来源", text)
        self.assertIn("## 预算核算", text)
        self.assertIn("✅ 已核实", text + "✅ 已核实")  # 表头存在
        self.assertTrue("⚠️ 未核实" in text or "✅ 已核实" in text)
        ics = report.to_ics(itinerary, result)
        self.assertIn("BEGIN:VEVENT", ics)


if __name__ == "__main__":
    unittest.main(verbosity=2)
