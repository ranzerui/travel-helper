"""离线自测：不需要网络、不需要模型。"""
import os
import shutil
import sys
import unittest
from pathlib import Path
from datetime import date, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CITY_FIXTURE = Path(__file__).with_name("fixtures") / "成都.json"



from travel_planner import cli, data, planner, report, simulator
from travel_planner.models import DayPlan, Itinerary, Leg

MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)
TMP_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")


def temp_dir(name):
    """用工程内的临时目录，避免系统临时目录的权限问题。"""
    path = os.path.join(TMP_ROOT, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path, exist_ok=True)
    return path


def make_itinerary(specs, budget=1500.0):
    """specs: [(date, [spot_id...]), ...]"""
    days = []
    for index, (current, ids) in enumerate(specs, start=1):
        days.append(DayPlan(day_index=index, date=current, legs=[Leg(spot_id=spot_id) for spot_id in ids]))
    return Itinerary(city="成都", days=days, budget=budget)


class SimulatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spots = {spot.id: spot for spot in data.load_file(CITY_FIXTURE)}

    def test_closed_day_detected(self):
        itinerary = make_itinerary([(MONDAY, ["sichuan_museum"])])
        result = simulator.simulate(itinerary, self.spots)
        self.assertFalse(result.ok)
        self.assertTrue(any("闭馆" in issue.message for issue in result.errors))

    def test_time_window_detected(self):
        itinerary = make_itinerary([(TUESDAY, ["sichuan_museum"])])
        itinerary.days[0].start = time(16, 0)
        result = simulator.simulate(itinerary, self.spots)
        self.assertFalse(result.ok)
        self.assertTrue(any("闭馆时间不够" in issue.message for issue in result.errors))

    def test_over_budget_detected(self):
        itinerary = make_itinerary([(TUESDAY, ["panda_base", "dufu_caotang"])], budget=100)
        result = simulator.simulate(itinerary, self.spots)
        self.assertFalse(result.ok)
        self.assertTrue(any("超过预算" in issue.message for issue in result.errors))

    def test_duplicate_spot_detected(self):
        itinerary = make_itinerary([(TUESDAY, ["dufu_caotang", "dufu_caotang"])])
        result = simulator.simulate(itinerary, self.spots)
        self.assertFalse(result.ok)
        self.assertTrue(any("重复安排" in issue.message for issue in result.errors))

    def test_travel_time_is_estimated(self):
        first, second = self.spots["panda_base"], self.spots["dufu_caotang"]
        self.assertGreater(simulator.travel_minutes(first, second), 5)

    def test_good_itinerary_passes(self):
        itinerary = make_itinerary([(TUESDAY, ["dufu_caotang", "kuanzhai"])], budget=1500)
        result = simulator.simulate(itinerary, self.spots)
        self.assertTrue(result.ok, result.summary_lines())
        self.assertLessEqual(result.total_cost, 1500)


class PlannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spots = data.load_file(CITY_FIXTURE)

    def test_default_plan_passes_validation(self):
        itinerary, result = planner.plan("成都", self.spots, days=3, budget=1500, start_date=TUESDAY)
        self.assertTrue(result.ok, result.summary_lines())
        for day in itinerary.days:
            self.assertGreaterEqual(len(day.legs), 1)

    def test_tight_budget_drops_paid_spots(self):
        itinerary, result = planner.plan("成都", self.spots, days=2, budget=500, start_date=TUESDAY)
        self.assertTrue(result.ok, result.summary_lines())
        self.assertLessEqual(result.total_cost, 500)

    def test_preference_shapes_selection(self):
        itinerary, _ = planner.plan("成都", self.spots, days=1, budget=1500, prefer="自然", start_date=TUESDAY)
        categories = [self.spots and next(s.category for s in self.spots if s.id == leg.spot_id) for leg in itinerary.days[0].legs]
        self.assertIn("自然", categories)

    def test_monday_skips_closed_museum(self):
        itinerary, result = planner.plan("成都", self.spots, days=1, budget=1500, prefer="人文", start_date=MONDAY)
        names = [next(s.name for s in self.spots if s.id == leg.spot_id) for leg in itinerary.days[0].legs]
        self.assertNotIn("四川博物院", names)
        self.assertTrue(result.ok, result.summary_lines())


class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spots = data.load_file(CITY_FIXTURE)
        cls.itinerary, cls.result = planner.plan("成都", spots, days=2, budget=1200, start_date=TUESDAY)

    def test_markdown_contains_key_sections(self):
        text = report.to_markdown(self.itinerary, self.result)
        self.assertIn("# 成都", text)
        self.assertIn("## 预算核算", text)
        self.assertIn("## 信息来源", text)
        self.assertIn("校验结果", text)

    def test_ics_contains_events(self):
        text = report.to_ics(self.itinerary, self.result)
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertIn("BEGIN:VEVENT", text)
        self.assertIn("DTSTART:", text)
        self.assertIn("END:VCALENDAR", text)

    def test_write_outputs(self):
        folder = temp_dir("write_outputs")
        md_path, ics_path = report.write_outputs(self.itinerary, self.result, folder)
        self.assertTrue(md_path.exists())
        self.assertTrue(ics_path.exists())
        self.assertGreater(md_path.stat().st_size, 200)


class CliTest(unittest.TestCase):
    def test_cli_runs_end_to_end(self):
        folder = temp_dir("cli_ok")
        code = cli.main(["--offline", "--days", "2", "--budget", "1200", "--out", folder])
        self.assertEqual(code, 0)
        self.assertTrue(os.listdir(folder))

    def test_cli_unknown_city(self):
        folder = temp_dir("cli_bad_city")
        code = cli.main(["--offline", "--city", "不存在的城市", "--out", folder])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
