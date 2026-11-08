from contextlib import asynccontextmanager
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from travel_planner import browser_review, planner
from travel_planner.models import Spot
from workers import browser_session, review_browser


class BrowserSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(),
                                       cdp_client=SimpleNamespace(send=SimpleNamespace(
                                           Browser=SimpleNamespace(close=AsyncMock()))))
        self.factory = Mock(return_value=self.browser)
        self.module = SimpleNamespace(Browser=self.factory)
        self.executable_check = patch.object(browser_session.Path, 'is_file', return_value=True)
        self.executable_check.start()
        self.addCleanup(self.executable_check.stop)

    async def test_owned_sessions_use_separate_temporary_profiles_and_background_edge(self):
        processes = [Mock(), Mock()]
        for process in processes:
            process.poll.return_value = None
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"BROWSER_USE_CDP_URL": ""}), \
                patch.dict("sys.modules", {"browser_use": self.module}), \
                patch.object(browser_session.subprocess, "Popen", side_effect=processes) as launch, \
                patch.object(browser_session, "wait_for_debugger", new=AsyncMock(return_value="ws://127.0.0.1:1234/browser")):
            with patch.dict(os.environ, {}, clear=True):
                async with browser_session.open_browser(folder):
                    first_command = launch.call_args.args[0]
                    first_profile = Path(next(value.split("=", 1)[1] for value in first_command if value.startswith("--user-data-dir=")))
                    async with browser_session.open_browser(folder):
                        second_command = launch.call_args.args[0]
                        second_profile = Path(next(value.split("=", 1)[1] for value in second_command if value.startswith("--user-data-dir=")))
                        self.assertNotEqual(first_profile, second_profile)
                        self.assertTrue(first_profile.is_dir())
                        self.assertTrue(second_profile.is_dir())
                        self.assertNotEqual(first_profile.parent, Path(folder))
                        self.assertIn("--headless=new", first_command)
                        self.assertIn("--remote-debugging-port=0", first_command)
                        self.assertNotIn("--no-sandbox", first_command)
            self.assertFalse(first_profile.exists())
            self.assertFalse(second_profile.exists())
        for process in processes:
            process.terminate.assert_called_once()

    async def test_attached_browser_is_never_closed_or_terminated(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"BROWSER_USE_CDP_URL": "http://127.0.0.1:9222"}), \
                patch.dict("sys.modules", {"browser_use": self.module}), \
                patch.object(browser_session.subprocess, "Popen") as launch:
            async with browser_session.open_browser(folder):
                pass
        launch.assert_not_called()
        self.browser.cdp_client.send.Browser.close.assert_not_awaited()
        self.browser.stop.assert_awaited_once()

    async def test_failed_connection_cleans_up_only_owned_process_and_profile(self):
        process = Mock()
        process.poll.return_value = None
        self.browser.start.side_effect = RuntimeError("connection failed")
        with tempfile.TemporaryDirectory() as folder, \
                patch.dict(os.environ, {"BROWSER_USE_CDP_URL": ""}), \
                patch.dict("sys.modules", {"browser_use": self.module}), \
                patch.object(browser_session.subprocess, "Popen", return_value=process) as launch, \
                patch.object(browser_session, "wait_for_debugger", new=AsyncMock(return_value="ws://127.0.0.1:1234/browser")):
            with self.assertRaisesRegex(RuntimeError, "connection failed"):
                async with browser_session.open_browser(folder):
                    self.fail("failed browser was yielded")
            profile = Path(next(value.split("=", 1)[1] for value in launch.call_args.args[0] if value.startswith("--user-data-dir=")))
            self.assertFalse(profile.exists())
        process.terminate.assert_called_once()

    async def test_debugger_reports_early_process_exit(self):
        process = Mock(returncode=123)
        process.poll.return_value = 123
        with self.assertRaisesRegex(RuntimeError, "123"):
            await browser_session.wait_for_debugger(process, Path("unused"))

    async def test_debugger_waits_for_complete_port_file(self):
        process = Mock()
        process.poll.return_value = None
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=Mock())
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(browser_session.Path, "read_text", side_effect=["", "12345\n/browser"]), \
                patch.object(browser_session.urllib.request, "build_opener", return_value=opener), \
                patch.object(browser_session.json, "load", return_value={"webSocketDebuggerUrl": "ws://127.0.0.1:12345/browser"}), \
                patch.object(browser_session.asyncio, "sleep", new=AsyncMock()):
            endpoint = await browser_session.wait_for_debugger(process, Path("profile"))
        self.assertEqual(endpoint, "ws://127.0.0.1:12345/browser")
        opener.open.assert_called_once_with("http://127.0.0.1:12345/json/version", timeout=0.5)

    async def test_page_closed_when_attached_review_is_cancelled(self):
        page = AsyncMock()
        page.set_viewport_size.side_effect = TimeoutError("interrupted")
        browser = SimpleNamespace(new_page=AsyncMock(return_value=page), close_page=AsyncMock())

        @asynccontextmanager
        async def connection(folder):
            yield browser

        with patch.object(review_browser, "open_browser", connection):
            with self.assertRaises(TimeoutError):
                await review_browser.run({"targets": []}, Path("unused/result.json"))
        browser.close_page.assert_awaited_once_with(page)


class WorkerFailureTest(unittest.TestCase):
    def test_worker_connection_error_reaches_review_message(self):
        spot = Spot("palace", "故宫博物院", "北京", "人文", 39.92, 116.39, "09:00", "17:00", 90)
        itinerary, _ = planner.plan("北京", [spot], 1, 1500, start_date=date(2026, 11, 7))

        def failed_worker(command, **kwargs):
            result_file = Path(command[command.index("--out") + 1])
            result_file.write_text(json.dumps({"checked": [], "error": "后台查价失败：独立 Edge 连接失败"}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 1, "", "connection failed")

        with tempfile.TemporaryDirectory() as folder, \
                patch.object(browser_review.subprocess, "run", side_effect=failed_worker):
            review = browser_review.collect_evidence("北京", [spot], itinerary, lambda message: None, folder)
        self.assertEqual(review["status"], "failed")
        self.assertIn("独立 Edge 连接失败", review["message"])
        self.assertTrue(review["unchecked"])

    def test_main_keeps_partial_evidence_and_records_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = Path(folder) / "request.json"
            result = Path(folder) / "result.json"
            manifest.write_text('{"targets": []}', encoding="utf-8")
            result.write_text('{"checked": [{"spot_id": "palace"}]}', encoding="utf-8")
            with patch("sys.argv", ["review_browser", "--manifest", str(manifest), "--out", str(result)]), \
                    patch.object(review_browser, "run", new=AsyncMock(side_effect=TimeoutError())):
                with self.assertRaises(TimeoutError):
                    review_browser.main()
            payload = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(payload["checked"], [{"spot_id": "palace"}])
        self.assertIn("时间上限", payload["error"])


if __name__ == "__main__":
    unittest.main()
