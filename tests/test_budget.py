from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from travel_planner import browser_review, budget, cli, intent, planner, pricing, report, routes, web
from travel_planner.models import DayPlan, Itinerary, Leg, Spot
from travel_planner.simulator import simulate


NOW = datetime(2026, 11, 6, 14, 0)
DAY = date(2026, 11, 7)
SOURCE = {"aliases": ["故宫博物院", "故宫"]}
TARIFF = "每年4月1日至10月31日为旺季，大门票60元/人；每年11月1日至次年3月31日为淡季，大门票40元/人；珍宝馆门票：10元/人；学生票20元/人。"


def example_spot(**values):
    return replace(Spot("palace", "故宫博物院", "北京", "人文", 39.92, 116.39, "09:00", "17:00", 90), **values)


class BudgetIntentTest(unittest.TestCase):
    def test_explicit_budget_variants_are_preserved(self):
        for text in ("200", "预算200", "预算只有200", "北京两天，200", "北京两天，两百块", "北京预算：￥200", "我只有200"):
            with self.subTest(text=text):
                req = intent.parse_requirements(text, use_llm=False, now=NOW)
                self.assertEqual(req.budget, 200)
                self.assertEqual(req.budget_source, "用户明确指定的总预算")

    def test_model_cannot_override_user_budget(self):
        with patch.object(intent.llm, "chat", return_value='{"city":"北京","days":2,"budget":1500}'):
            req = intent.parse_requirements("北京两天，预算200", now=NOW)
        self.assertEqual(req.budget, 200)

    def test_followup_bare_amount_updates_budget_only(self):
        req = intent.parse_requirements("11月15日北京两天预算1500", use_llm=False, now=NOW)
        changed, changes = intent.apply_delta(req, "200", now=NOW)
        self.assertEqual((changed.budget, changed.days, changed.city, changed.start_date), (200, 2, "北京", "2026-11-15"))
        self.assertTrue(any("预算" in change for change in changes))

    def test_dates_and_duration_do_not_become_budget(self):
        for text in ("11月15日北京两天", "2026年11月15日北京三天", "明天去北京2天"):
            with self.subTest(text=text):
                self.assertIsNone(budget.extract_budget(text))

    def test_money_precision_and_thousands(self):
        self.assertEqual(budget.extract_budget("预算1,500.50元"), 1500.5)
        self.assertEqual(budget.extract_budget("预算一千五百元"), 1500)
        self.assertEqual(budget.extract_budget("预算两百元"), 200)

    def test_zero_and_negative_are_errors(self):
        for text in ("0", "预算0", "预算-200", "-200"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                budget.extract_budget(text)
        self.assertEqual(cli.main(["--offline", "--budget", "0"]), 2)


class TicketPricingTest(unittest.TestCase):
    def test_seasonal_adult_price_excludes_extras_and_discounts(self):
        rules = pricing.extract_ticket_prices(TARIFF, SOURCE, "故宫导览")
        self.assertEqual(len(rules), 2)
        self.assertEqual(pricing.matching_price(rules, DAY)["amount"], 40)
        self.assertEqual(pricing.matching_price(rules, date(2027, 3, 31))["amount"], 40)
        self.assertEqual(pricing.matching_price(rules, date(2027, 4, 1))["amount"], 60)

    def test_free_admission_requires_unconditional_statement(self):
        self.assertEqual(pricing.extract_ticket_prices("故宫博物院未满18周岁免费参观。", SOURCE), [])
        self.assertEqual(pricing.extract_ticket_prices("故宫博物院取消免费参观。", SOURCE), [])
        self.assertEqual(pricing.extract_ticket_prices("免费参观\n仅限儿童", SOURCE, "故宫博物院"), [])
        self.assertEqual(pricing.extract_ticket_prices("故宫博物院实行免费开放。", SOURCE)[0]["amount"], 0)

    def test_ambiguous_or_conditional_tariffs_are_not_adopted(self):
        for text in ("旺季门票60元", "成人票20元起", "团体成人票20元", "门票20至80元", "珍宝馆门票10元"):
            with self.subTest(text=text):
                self.assertEqual(pricing.extract_ticket_prices(text, SOURCE), [])

    def test_conflicting_prices_remain_unknown(self):
        rules = [{"amount": 40}, {"amount": 60}]
        self.assertIsNone(pricing.matching_price(rules, DAY))

    def test_square_free_admission_is_not_gate_ticket(self):
        self.assertEqual(pricing.extract_ticket_prices("天安门广场实行免费开放。", {"aliases": ["天安门"]}, "天安门广场预约"), [])

    def test_unknown_zero_is_not_free_in_reports(self):
        spot = example_spot()
        itinerary = Itinerary("北京", [DayPlan(1, DAY, [Leg(spot.id)])], 200)
        result = simulate(itinerary, {spot.id: spot})
        self.assertIsNone(result.results[0].legs[0]["ticket"])
        self.assertFalse(result.budget_complete)
        self.assertEqual(result.unknown_tickets, [spot.name])
        document = report.to_markdown(itinerary, result)
        self.assertIn("待核实", document)
        self.assertNotIn("按 09:00–18:00、免费处理", document)
        self.assertNotIn("| 免费 |", document)
        self.assertIn("不能确认结余", document)

    def test_actual_day_selects_season_across_trip_boundary(self):
        spot = example_spot(ticket_prices=tuple(pricing.extract_ticket_prices(TARIFF, SOURCE)))
        self.assertEqual(pricing.ticket_details(spot, date(2027, 3, 31))["amount"], 40)
        self.assertEqual(pricing.ticket_details(spot, date(2027, 4, 1))["amount"], 60)

    def test_official_price_updates_replan_under_original_budget(self):
        spot = example_spot()
        req = intent.Requirements(city="北京", days=1, budget=180)
        itinerary, result = planner.plan("北京", [spot], 1, 180, start_date=DAY)
        review = {"initial": browser_review._snapshot(itinerary, [spot]), "checked": [{"spot_id": spot.id,
                  "status": "captured", "pages": [{"url": "https://www.dpm.org.cn/Visit.html", "findings": [],
                  "ticket_prices": pricing.extract_ticket_prices(TARIFF, SOURCE)}]}], "unchecked": [], "changes": []}
        final, checked = browser_review.apply_review(req, [spot], itinerary, result, review)
        self.assertEqual(final.budget, 180)
        self.assertTrue(review["ticket_updates"])
        self.assertNotIn(spot.id, final.spot_ids)
        self.assertFalse(checked.ok)

    def test_web_preserves_200_and_reports_insufficient_two_day_budget(self):
        spot = example_spot()
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(web.travel_dates, "local_now", return_value=NOW), \
                patch.object(web.data, "ensure_city", return_value=[spot]), \
                patch.object(web.routes, "build_provider", return_value=routes.EstimateProvider()), \
                patch.object(web, "OUTPUT_DIR", Path(temporary)):
            payload = web.run_plan({"q": "北京两天，预算只有200", "mode": "offline"}, lambda text: None)
        self.assertEqual(payload["requirements"]["budget"], 200)
        self.assertEqual(payload["base"]["budget"], 200)
        self.assertEqual(payload["budget"]["budget"], 200)
        self.assertEqual(payload["budget"]["status"], "over_budget")
        self.assertIsNone(payload["budget"]["left"])
        self.assertFalse(payload["checks"]["ok"])
        self.assertFalse(payload["checks"]["budget_ok"])
        self.assertFalse(payload["checks"]["feasible"])
        self.assertIsNone(payload["days"][0]["legs"][0]["ticket"])


if __name__ == "__main__":
    unittest.main()
