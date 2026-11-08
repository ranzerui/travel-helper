"""设置页的后端：读/写 .env、校验 key 和模型、以及 HTTP 接口。"""
import json
import os
import sys
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import settings

TMP = Path(__file__).with_name("_tmp_settings")


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        for item in TMP.iterdir():
            item.unlink()
        self.env = TMP / ".env"

    def test_write_merges_and_preserves_other_keys(self):
        self.env.write_text("AMAP_KEY=old\nOTHER=keep\n", encoding="utf-8")
        settings.write_env({"TRAVEL_MODEL": "qwen3:14b"}, path=self.env)
        text = self.env.read_text(encoding="utf-8")
        self.assertIn("OTHER=keep", text)
        self.assertIn("TRAVEL_MODEL=qwen3:14b", text)

    def test_write_replaces_existing_key(self):
        self.env.write_text("AMAP_KEY=old\n", encoding="utf-8")
        settings.write_env({"AMAP_KEY": "new"}, path=self.env)
        self.assertNotIn("AMAP_KEY=old", self.env.read_text(encoding="utf-8"))

    def test_read_env_does_not_touch_environ(self):
        self.env.write_text("TRAVEL_MODEL=from-file\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TRAVEL_MODEL", None)
            values = settings.read_env(path=self.env)
            self.assertEqual(values["TRAVEL_MODEL"], "from-file")
            self.assertNotIn("TRAVEL_MODEL", os.environ)


class MaskTest(unittest.TestCase):
    def test_mask_hides_middle(self):
        self.assertEqual(settings.mask(""), "")
        self.assertEqual(settings.mask("12345"), "*****")
        masked = settings.mask("0123456789abcdef0123456789abcdef")
        self.assertTrue(masked.startswith("0123"))
        self.assertTrue(masked.endswith("cdef"))
        self.assertNotIn("456789ab", masked)


class ApplyTest(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        for item in TMP.iterdir():
            item.unlink()
        self.env = TMP / ".env"
        self.env.write_text("AMAP_KEY=k1\nTRAVEL_MODEL=qwen3:8b\n", encoding="utf-8")

    def test_apply_without_verify_writes_model(self):
        with mock.patch.object(settings, "ENV_FILE", self.env):
            result = settings.apply({"model": "qwen3:14b"}, verify=False)
        self.assertTrue(result["ok"])
        self.assertIn("TRAVEL_MODEL=qwen3:14b", self.env.read_text(encoding="utf-8"))

    def test_invalid_model_is_rejected_and_not_written(self):
        with mock.patch.object(settings, "ENV_FILE", self.env), \
             mock.patch.object(settings, "validate_model", return_value=(False, "没有这个模型")):
            result = settings.apply({"model": "不存在的模型"})
        self.assertFalse(result["ok"])
        self.assertIn("没有这个模型", " ".join(result["errors"]))
        self.assertNotIn("不存在的模型", self.env.read_text(encoding="utf-8"))

    def test_invalid_key_is_rejected(self):
        with mock.patch.object(settings, "ENV_FILE", self.env), \
             mock.patch.object(settings, "validate_amap", return_value=(False, "INVALID_USER_KEY")):
            result = settings.apply({"amap_key": "bad-key"})
        self.assertFalse(result["ok"])
        self.assertIn("INVALID_USER_KEY", " ".join(result["errors"]))

    def test_current_reports_masked_key(self):
        with mock.patch.object(settings, "ENV_FILE", self.env), \
             mock.patch.dict(os.environ, {"AMAP_KEY": "0123456789abcdef0123456789abcdef",
                                          "TRAVEL_MODEL": "qwen3:8b"}, clear=False):
            info = settings.current()
        self.assertTrue(info["amap_configured"])
        self.assertNotIn("456789ab", info["amap_key_masked"])
        self.assertEqual(info["model"], "qwen3:8b")


class HttpSettingsTest(unittest.TestCase):
    """真实起一个 server，验 GET/POST /api/settings。"""

    @classmethod
    def setUpClass(cls):
        import threading

        from travel_planner import web

        cls.env = TMP / ".env"
        TMP.mkdir(parents=True, exist_ok=True)
        cls.env.write_text("AMAP_KEY=test-key\nTRAVEL_MODEL=qwen3:8b\n", encoding="utf-8")
        cls.patcher = mock.patch.object(settings, "ENV_FILE", cls.env)
        cls.patcher.start()
        cls.server = web._try_bind(8830)
        if cls.server is None:
            raise unittest.SkipTest("端口 8830 被占用")
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.patcher.stop()
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:8830" + path, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_get_settings(self):
        status, data = self._get("/api/settings")
        self.assertEqual(status, 200)
        self.assertIn("settings", data)
        self.assertIsInstance(data["models"], list)

    def test_post_rejects_bad_model(self):
        body = json.dumps({"model": "不存在的模型"}).encode("utf-8")
        request = urllib.request.Request("http://127.0.0.1:8830/api/settings", data=body,
                                         headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(request, timeout=15)
            self.fail("应该返回 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
            payload = json.loads(exc.read().decode("utf-8"))
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
