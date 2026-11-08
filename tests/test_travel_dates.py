from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from travel_planner import cli, data, intent, routes, travel_dates, web


NOW = datetime(2026, 11, 6, 14, 30, tzinfo=timezone(timedelta(hours=8)))


class TravelDatesTest(unittest.TestCase):
    def test_default_uses_local_day_including_timezone(self):
        local_midnight = datetime(2026, 11, 7, 0, 1, tzinfo=NOW.tzinfo)
        selected, source = travel_dates.resolve_start_date(now=local_midnight)
        self.assertEqual(selected, "2026-11-07")
        self.assertIn("电脑", source)

    def test_natural_dates(self):
        examples = {
            "北京两天": "2026-11-06",
            "明天去北京两天": "2026-11-07",
            "后天去北京": "2026-11-08",
            "三天后去北京两天": "2026-11-09",
            "2026年11月15日去北京": "2026-11-15",
            "2026-11-15 北京两天": "2026-11-15",
            "11月15日去北京": "2026-11-15",
            "11/15去北京": "2026-11-15",
            "1月2日去北京": "2027-01-02",
            "明年11月15日去北京": "2027-11-15",
            "下周一去北京": "2026-11-09",
            "本周日去北京": "2026-11-08",
            "周末去北京": "2026-11-07",
            "下周末去北京": "2026-11-14",
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(travel_dates.resolve_start_date(text, now=NOW)[0], expected)

    def test_api_date_overrides_text_even_when_text_is_past(self):
        req = intent.parse_requirements("2025年1月1日北京两天", use_llm=False,
                                        now=NOW, explicit_start_date="2026-11-20")
        self.assertEqual(req.start_date, "2026-11-20")
        self.assertEqual(req.days, 2)

    def test_invalid_or_past_dates_produce_clear_error(self):
        for text in ("2026年2月30日", "2025年11月15日", "2026年13月1日", "下个月去北京"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                travel_dates.resolve_start_date(text, now=NOW)

    def test_dates_never_become_trip_duration(self):
        for text in ("11月15日北京两天", "2026年11月15日北京两天", "2026-11-15 北京两天", "三天后北京两天"):
            with self.subTest(text=text):
                req = intent.parse_requirements(text, use_llm=False, now=NOW)
                self.assertEqual(req.days, 2)
                self.assertEqual(req.city, "北京")
        req = intent.parse_requirements("11月15日去北京", use_llm=False, now=NOW)
        self.assertEqual(req.days, 3)

    def test_refinement_keeps_or_changes_date(self):
        req = intent.parse_requirements("11月15日北京两天", use_llm=False, now=NOW)
        adjusted, _ = intent.apply_delta(req, "预算改成1800", now=NOW)
        self.assertEqual(adjusted.start_date, "2026-11-15")
        changed, changes = intent.apply_delta(adjusted, "改到11月20日", now=NOW)
        self.assertEqual(changed.start_date, "2026-11-20")
        self.assertEqual(changed.days, 2)
        self.assertTrue(any("出游日期" in change for change in changes))

    def test_cli_rejects_invalid_date_before_data_fetch(self):
        with patch.object(travel_dates, "local_now", return_value=NOW), \
                patch.object(cli.data, "ensure_city") as fetch:
            self.assertEqual(cli.main(["--offline", "--start-date", "2026-02-30"]), 2)
        fetch.assert_not_called()

    def test_web_dates_reach_itinerary_and_followup(self):
        spots = data.load_file(Path(__file__).with_name("fixtures") / "成都.json")
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(travel_dates, "local_now", return_value=NOW), \
                patch.object(web.data, "ensure_city", return_value=spots), \
                patch.object(web.routes, "build_provider", return_value=routes.EstimateProvider()), \
                patch.object(web, "OUTPUT_DIR", Path(temporary)):
            payload = web.run_plan({"q": "成都两天预算1500", "mode": "offline"}, lambda text: None)
            self.assertEqual(payload["days"][0]["date"], "2026-11-06")
            params = {"q": "改到11月15日", "mode": "offline", "refine": "1",
                      **{"base_" + key: str(value) for key, value in payload["base"].items()}}
            revised = web.run_plan(params, lambda text: None)
            self.assertEqual(revised["days"][0]["date"], "2026-11-15")
            params = {"q": "预算改成1800", "mode": "offline", "refine": "1",
                      **{"base_" + key: str(value) for key, value in revised["base"].items()}}
            final = web.run_plan(params, lambda text: None)
            self.assertEqual(final["requirements"]["start_date"], "2026-11-15")
            self.assertEqual(final["days"][0]["date"], "2026-11-15")


if __name__ == "__main__":
    unittest.main()
