import base64
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from travel_planner import browser_review, discovery, planner
from travel_planner.models import Spot


class DiscoveryTest(unittest.TestCase):
    def test_candidate_selection_ignores_aggregators_and_unrelated_results(self):
        candidates = discovery.rank_candidates([
            {"title": "景山公园攻略", "url": "https://www.ctrip.com/park"},
            {"title": "北京政府", "url": "https://www.beijing.gov.cn/"},
            {"title": "北京市景山公园", "url": "https://www.bjjspark.cn/"},
        ], ["景山公园"])
        self.assertEqual([item["url"] for item in candidates], ["https://www.bjjspark.cn/"])

    def test_decodes_real_bing_redirect_target(self):
        target = "https://www.bjjspark.cn/"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        self.assertEqual(discovery.result_url("https://www.bing.com/ck/a?u=a1" + encoded), target)

    def test_search_title_alone_does_not_authenticate_source(self):
        self.assertFalse(discovery.source_identity("景山公园官网", "景山公园旅游攻略", ["景山公园"])["matched"])
        identity = discovery.source_identity("北京市景山公园", "版权所有：北京市景山公园管理处", ["景山公园"])
        self.assertTrue(identity["matched"])
        self.assertIn("未经人工认证", identity["reason"])

    def test_private_or_credential_urls_are_rejected(self):
        for url in ("http://127.0.0.1/", "http://10.0.0.1/", "file:///tmp/page",
                    "https://user:pass@www.bjjspark.cn/", "http://server.local/", "http://localhost/"):
            with self.subTest(url=url):
                self.assertFalse(discovery.public_url(url))

    def test_dns_must_resolve_to_public_addresses(self):
        addresses = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch.object(discovery.socket, "getaddrinfo", return_value=addresses):
            self.assertFalse(discovery.public_url("https://example.com", resolve=True))

    def test_unconfigured_spot_enters_discovery_and_search_image_is_served(self):
        spots = [Spot("park", "景山公园", "北京", "自然", 39.93, 116.40, "09:00", "18:00", 90)]
        itinerary, _ = planner.plan("北京", spots, 1, 1000, start_date=date(2026, 11, 7))

        def run_worker(command, **kwargs):
            manifest = json.loads(Path(command[command.index("--manifest") + 1]).read_text(encoding="utf-8"))
            self.assertTrue(manifest["targets"][0]["discover"])
            self.assertEqual(manifest["trip_dates"], ["2026-11-07"])
            result = {"checked": [{"spot_id": "park", "name": "景山公园", "status": "failed", "pages": [],
                                   "error": "尚未确认来源", "discovery": {"search_page": {"screenshot": "search.png"}}}]}
            Path(command[command.index("--out") + 1]).write_text(json.dumps(result), encoding="utf-8")
            return SimpleNamespace(stdout="", stderr="", returncode=0)

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(browser_review.subprocess, "run", side_effect=run_worker), \
                patch.dict("os.environ", {"BROWSER_USE_PY": __import__("sys").executable}):
            review = browser_review.collect_evidence("北京", spots, itinerary, lambda text: None, temporary)
        self.assertEqual(review["status"], "failed")
        self.assertEqual(review["unchecked"][0]["name"], "景山公园")
        self.assertTrue(review["checked"][0]["discovery"]["search_page"]["screenshot_url"].startswith("/evidence/"))


if __name__ == "__main__":
    unittest.main()
