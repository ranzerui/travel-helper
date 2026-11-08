"""数据源层测试：本地、缓存、browser-use 子进程（用假的 subprocess 结果）。"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FIXTURE = Path(__file__).with_name("fixtures") / "成都.json"



from travel_planner import data, sources

TMP_ROOT = Path(__file__).with_name("_tmp")
FIXTURE_DIR = Path(__file__).with_name("fixtures")
_REAL_DATA_DIR = data.DATA_DIR


def setUpModule():
    data.DATA_DIR = FIXTURE_DIR


def tearDownModule():
    data.DATA_DIR = _REAL_DATA_DIR


def temp_dir(name: str) -> Path:
    path = TMP_ROOT / name
    if path.exists():
        for item in sorted(path.rglob("*"), reverse=True):
            try:
                item.unlink() if item.is_file() else item.rmdir()
            except OSError:
                pass
    path.mkdir(parents=True, exist_ok=True)
    return path


class LocalSourceTest(unittest.TestCase):
    def test_local_source_returns_spots(self):
        spots = sources.LocalSource().fetch("成都")
        self.assertGreaterEqual(len(spots), 5)
        self.assertTrue(all(spot.name for spot in spots))
        self.assertTrue(any(spot.closed_days for spot in spots))


class CachedSourceTest(unittest.TestCase):
    def test_cache_hit_skips_inner_source(self):
        cache_dir = temp_dir("cache_hit")
        calls = {"count": 0}

        class CountingSource:
            name = "计数源"

            def fetch(self, city, query=""):
                calls["count"] += 1
                return data.load_city(city)

        cached = sources.CachedSource(CountingSource(), cache_dir=cache_dir)
        first = cached.fetch("成都")
        second = cached.fetch("成都")
        self.assertEqual(calls["count"], 1, "第二次应该命中缓存")
        self.assertEqual(len(first), len(second))


class BrowserSourceTest(unittest.TestCase):
    def test_missing_interpreter_raises(self):
        source = sources.BrowserSource(python_exe=str(TMP_ROOT / "没有这个python.exe"))
        with self.assertRaises(RuntimeError):
            source.fetch("成都")

    def test_parses_worker_output(self):
        out_dir = temp_dir("browser_out")
        fake_python = TMP_ROOT / "fake_python.exe"
        fake_python.write_bytes(b"stub")
        payload = {
            "city": "成都",
            "source_name": "Wikipedia（browser-use 抓取）",
            "captured_at": "2026-11-05",
            "spots": [
                {"id": "web_1", "name": "Wuhou Shrine", "desc": "三国文化祠庙",
                 "url": "https://en.wikipedia.org/wiki/Wuhou_Shrine"},
            ],
        }

        def fake_run(command, capture_output, text, encoding, errors, timeout):
            out_path = Path(command[command.index("--out") + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            class Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return Result()

        source = sources.BrowserSource(python_exe=str(fake_python))
        with mock.patch.object(sources.subprocess, "run", fake_run), \
             mock.patch.object(sources, "DEFAULT_CACHE", out_dir):
            spots = source.fetch("成都")

        self.assertEqual(len(spots), 1)
        self.assertEqual(spots[0].name, "Wuhou Shrine")
        self.assertEqual(spots[0].open_time, "09:00")      # 抓取没给的字段用默认值
        self.assertEqual(spots[0].captured_at, "2026-11-05")
        self.assertIn("browser-use", spots[0].source_name)

    def test_worker_failure_propagates(self):
        fake_python = TMP_ROOT / "fake_python.exe"
        fake_python.write_bytes(b"stub")

        def failing_run(command, capture_output, text, encoding, errors, timeout):
            class Result:
                returncode = 1
                stdout = ""
                stderr = "Chromium crashed"

            return Result()

        source = sources.BrowserSource(python_exe=str(fake_python))
        with mock.patch.object(sources.subprocess, "run", failing_run):
            with self.assertRaises(RuntimeError) as ctx:
                source.fetch("成都")
        self.assertIn("抓取失败", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
