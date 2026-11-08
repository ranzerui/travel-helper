from datetime import date, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from travel_planner import browser_review, data, intent, planner, report, routes, simulator, web
from travel_planner.models import Spot


SOURCE = {"aliases": ["故宫博物院", "故宫"]}
SATURDAY = date(2026, 11, 7)


def example_spots():
    return [
        Spot("palace", "故宫博物院", "北京", "人文", 39.92, 116.39, "09:00", "17:00", 90),
        Spot("park", "景山公园", "北京", "自然", 39.93, 116.40, "09:00", "18:00", 90),
    ]


def review_for(itinerary, spots, text):
    return {"status": "completed", "initial": browser_review._snapshot(itinerary, spots),
            "changes": [], "unchecked": [], "message": "已采集官网证据",
            "checked": [{"spot_id": "palace", "name": "故宫博物院", "status": "captured",
                         "pages": [{"url": "https://www.dpm.org.cn/test-notice",
                                    "captured_at": "2026-11-06T10:00:00+08:00",
                                    "findings": browser_review.extract_findings(text, SOURCE)}]}]}


class ExtractionTest(unittest.TestCase):
    def finding(self, text):
        return browser_review.extract_findings(text, SOURCE)[0]

    def test_full_closure_has_effective_dates(self):
        finding = self.finding("故宫博物院于2026年11月7日至2026年11月9日全馆临时闭馆。")
        self.assertTrue(finding["actionable"])
        self.assertEqual(finding["effective_end"], "2026-11-09")

    def test_abbreviated_range(self):
        finding = self.finding("故宫博物院2026年11月7日至9日全馆闭馆。")
        self.assertTrue(finding["actionable"])
        self.assertEqual(finding["effective_end"], "2026-11-09")

    def test_partial_closure_does_not_remove_entire_palace(self):
        finding = self.finding("故宫博物院武英殿自2026年11月7日起停止开放。")
        self.assertEqual(finding["scope"], "partial")
        self.assertFalse(finding["actionable"])

    def test_publication_date_is_not_effective_date(self):
        self.assertFalse(self.finding("2026年11月6日发布：故宫博物院临时闭馆公告")["actionable"])

    def test_unknown_date_is_advisory(self):
        self.assertFalse(self.finding("故宫博物院全馆暂停开放，时间另行通知。")["actionable"])

    def test_recovery_or_exception_is_not_closure(self):
        self.assertFalse(self.finding("故宫博物院2026年11月7日恢复正常开放，不闭馆。")["actionable"])
        self.assertFalse(self.finding("故宫博物院周一闭馆，法定节假日除外。")["actionable"])

    def test_unknown_entity_is_not_full_closure(self):
        self.assertFalse(self.finding("2026年11月7日临时闭馆。")["actionable"])

    def test_article_title_can_resolve_museum_pronoun(self):
        finding = browser_review.extract_findings("我院将于2026年11月7日全馆临时闭馆。", SOURCE,
                                                 "故宫博物院临时闭馆公告")[0]
        self.assertTrue(finding["actionable"])

    def test_unlabelled_publication_date_is_not_schedule(self):
        self.assertFalse(self.finding("2026年11月7日 关于故宫博物院临时闭馆的公告")["actionable"])

    def test_closure_subject_does_not_match_other_museum_in_same_sentence(self):
        finding = self.finding("故宫博物院附近的其他博物馆2026年11月7日临时闭馆。")
        self.assertFalse(finding["actionable"])

    def test_reservation_preserves_exact_quote(self):
        quote = "参观需提前7天实名预约。"
        self.assertEqual(self.finding(quote)["quote"], quote)
        self.assertEqual(self.finding(quote)["kind"], "reservation")


class ReplanningTest(unittest.TestCase):
    def setUp(self):
        self.spots = example_spots()
        self.requirements = intent.Requirements(city="北京", days=2, budget=1500)
        self.itinerary, self.result = planner.plan("北京", self.spots, 2, 1500, start_date=SATURDAY)

    def test_closure_moves_spot_to_available_day_and_passes_simulation(self):
        review = review_for(self.itinerary, self.spots, "故宫博物院2026年11月7日全馆临时闭馆。")
        itinerary, result = browser_review.apply_review(
            self.requirements, self.spots, self.itinerary, self.result, review)
        self.assertTrue(result.ok)
        palace_dates = [day.date for day in itinerary.days if any(leg.spot_id == "palace" for leg in day.legs)]
        self.assertEqual(palace_dates, [date(2026, 11, 8)])
        self.assertEqual(review["changes"][0]["before"], ["2026-11-07"])
        self.assertEqual(review["changes"][0]["after"], ["2026-11-08"])
        self.assertEqual(self.spots[0].closed_dates, ())
        self.assertFalse(self.spots[0].verified)

    def test_expired_notice_does_not_replan(self):
        review = review_for(self.itinerary, self.spots, "故宫博物院2026年10月7日至2026年10月9日全馆闭馆。")
        itinerary, result = browser_review.apply_review(
            self.requirements, self.spots, self.itinerary, self.result, review)
        self.assertIs(itinerary, self.itinerary)
        self.assertEqual(review["changes"], [])

    def test_partial_closure_does_not_replan(self):
        review = review_for(self.itinerary, self.spots, "故宫武英殿2026年11月7日起停止开放。")
        itinerary, _ = browser_review.apply_review(self.requirements, self.spots, self.itinerary, self.result, review)
        self.assertIs(itinerary, self.itinerary)

    def test_reopening_supersedes_previous_closure(self):
        review = review_for(self.itinerary, self.spots,
                            "故宫博物院2026年11月6日起全馆闭馆。\n故宫博物院2026年11月7日起恢复开放。")
        itinerary, _ = browser_review.apply_review(self.requirements, self.spots, self.itinerary, self.result, review)
        self.assertIs(itinerary, self.itinerary)
        self.assertEqual(review["changes"], [])

    def test_unavailable_must_see_is_explicit_error(self):
        self.requirements.must_see = ["故宫"]
        review = review_for(self.itinerary, self.spots, "故宫博物院2026年11月7日至2026年11月9日全馆闭馆。")
        _, result = browser_review.apply_review(self.requirements, self.spots, self.itinerary, self.result, review)
        self.assertFalse(result.ok)
        self.assertIn("必去景点", result.errors[-1].message)

    def test_simulator_and_planner_both_respect_date_closures(self):
        from dataclasses import replace

        blocked = replace(self.spots[0], closed_dates=("2026-11-07",))
        self.assertFalse(data.open_on(blocked, SATURDAY))
        result = simulator.simulate(self.itinerary, {"palace": blocked, "park": self.spots[1]})
        self.assertIn("官网公告临时闭馆", result.errors[0].message)

    def test_report_contains_review_and_source(self):
        review = review_for(self.itinerary, self.spots, "故宫博物院2026年11月7日全馆闭馆。")
        itinerary, result = browser_review.apply_review(self.requirements, self.spots, self.itinerary, self.result, review)
        markdown = report.to_markdown(itinerary, result)
        self.assertIn("官网核验与调整", markdown)
        self.assertIn("https://www.dpm.org.cn/test-notice", markdown)
        self.assertIn("2026-11-08", markdown)


class IntegrationTest(unittest.TestCase):
    def test_offline_does_not_start_worker(self):
        spots = example_spots()
        itinerary, _ = planner.plan("北京", spots, 1, 1000, start_date=SATURDAY)
        with patch.object(browser_review.subprocess, "run") as process:
            review = browser_review.collect_evidence("北京", spots, itinerary, lambda text: None, ".", enabled=False)
        process.assert_not_called()
        self.assertEqual(review["status"], "skipped")

    def test_official_domain_restriction(self):
        self.assertTrue(browser_review.allowed_url("https://www.dpm.org.cn/Visit.html", ["www.dpm.org.cn"]))
        for url in ["https://www.dpm.org.cn.evil.test/", "javascript:alert(1)", "https://user@www.dpm.org.cn/"]:
            self.assertFalse(browser_review.allowed_url(url, ["www.dpm.org.cn"]))

    def test_web_result_includes_review_and_replans(self):
        spots = example_spots()

        def collect(city, values, itinerary, emit, output_dir, **kwargs):
            return review_for(itinerary, values, "故宫博物院2026年11月7日全馆闭馆。")

        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(web.data, "ensure_city", return_value=spots), \
                    patch.object(web.travel_dates, "local_now", return_value=datetime(2026, 11, 6, 10)), \
                    patch.object(web.routes, "build_provider", return_value=routes.EstimateProvider()), \
                    patch.object(web.browser_review, "collect_evidence", side_effect=collect), \
                    patch.object(web, "OUTPUT_DIR", Path(temporary)):
                payload = web.run_plan({"q": "北京两天预算1500", "mode": "auto", "start_date": "2026-11-07"}, lambda text: None)
        self.assertTrue(payload["checks"]["ok"])
        self.assertEqual(payload["browser_review"]["changes"][0]["after"], ["2026-11-08"])
        self.assertNotIn("故宫博物院", [leg["name"] for leg in payload["days"][0]["legs"]])

    def test_missing_runtime_does_not_claim_success(self):
        spots = example_spots()
        itinerary, _ = planner.plan("北京", spots, 1, 1000, start_date=SATURDAY)
        with patch.dict("os.environ", {"BROWSER_USE_PY": "missing-python.exe"}):
            review = browser_review.collect_evidence("北京", spots, itinerary, lambda text: None, ".")
        self.assertEqual(review["status"], "unavailable")
        self.assertEqual(review["checked"], [])

    def test_only_one_browser_worker_runs_at_a_time(self):
        with browser_review.exclusive_browser():
            with self.assertRaises(OSError):
                with browser_review.exclusive_browser():
                    self.fail("并发启动了第二个浏览器")

    def test_evidence_endpoint_blocks_path_escape(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "evidence" / "run").mkdir(parents=True)
            (base / "evidence" / "run" / "entry.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (base / "secret.png").write_bytes(b"private")
            with patch.object(web, "OUTPUT_DIR", base):
                server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                try:
                    with opener.open(f"http://127.0.0.1:{server.server_port}/evidence/run/entry.png") as response:
                        self.assertEqual(response.headers["Content-Type"], "image/png")
                    with self.assertRaises(urllib.error.HTTPError):
                        opener.open(f"http://127.0.0.1:{server.server_port}/evidence/%2e%2e/secret.png")
                finally:
                    server.shutdown()
                    server.server_close()


if __name__ == "__main__":
    unittest.main()
