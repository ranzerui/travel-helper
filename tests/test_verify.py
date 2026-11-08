"""数据核实闭环的测试：导出 → 回导 → 健康度。"""
import csv
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import verify

TMP = Path(__file__).with_name("_tmp_verify")


def make_city(name="测试城", spots=None):
    TMP.mkdir(parents=True, exist_ok=True)
    payload = {
        "city": name,
        "kind": "scraped",
        "spots": spots or [
            {"id": "a1", "name": "景点甲", "open_time": "09:00", "close_time": "18:00",
             "ticket": 0.0, "closed_days": [], "verified": False,
             "source_name": "高德 POI（景点）", "captured_at": "2026-11-05", "desc": "地址甲"},
            {"id": "b2", "name": "景点乙", "open_time": "09:00", "close_time": "18:00",
             "ticket": 0.0, "closed_days": [], "verified": True,
             "source_name": "人工核实", "captured_at": "2026-11-01", "desc": "地址乙"},
        ],
    }
    (TMP / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return TMP / f"{name}.json"


class ExportTest(unittest.TestCase):
    def setUp(self):
        for item in TMP.glob("*"):
            item.unlink()

    def test_exports_only_pending_by_default(self):
        make_city()
        target = TMP / "pending.csv"
        with mock.patch.object(verify, "DATA_DIR", TMP):
            count = verify.export_pending("测试城", target)
        self.assertEqual(count, 1)
        rows = list(csv.DictReader(target.open(encoding="utf-8-sig")))
        self.assertEqual(rows[0]["name"], "景点甲")
        self.assertEqual(rows[0]["verified"], "否")
        self.assertIn("open_time", target.read_text(encoding="utf-8-sig"))

    def test_export_all_includes_verified(self):
        make_city()
        target = TMP / "all.csv"
        with mock.patch.object(verify, "DATA_DIR", TMP):
            count = verify.export_pending("测试城", target, only_pending=False)
        self.assertEqual(count, 2)


class ImportTest(unittest.TestCase):
    def setUp(self):
        for item in TMP.glob("*"):
            item.unlink()

    def _write_csv(self, rows, headers=None):
        target = TMP / "filled.csv"
        fields = headers or ["id", "name", "open_time", "close_time", "ticket", "closed_days"]
        with open(target, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return target

    def test_import_updates_and_marks_verified(self):
        path = make_city()
        csv_path = self._write_csv([
            {"id": "a1", "name": "景点甲", "open_time": "08:30", "close_time": "17:30",
             "ticket": "50", "closed_days": "周一、周三"},
        ])
        with mock.patch.object(verify, "DATA_DIR", TMP):
            result = verify.import_updates("测试城", csv_path)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["skipped"], 0)
        spot = json.loads(path.read_text(encoding="utf-8"))["spots"][0]
        self.assertTrue(spot["verified"])
        self.assertEqual(spot["open_time"], "08:30")
        self.assertEqual(spot["ticket"], 50.0)
        self.assertEqual(spot["closed_days"], [0, 2])
        self.assertIn("人工核实", spot["source_name"])

    def test_invalid_rows_are_skipped_with_reason(self):
        make_city()
        csv_path = self._write_csv([
            {"id": "a1", "name": "景点甲", "open_time": "8点半", "close_time": "18:00",
             "ticket": "0", "closed_days": ""},
            {"id": "b2", "name": "景点乙", "open_time": "09:00", "close_time": "18:00",
             "ticket": "免费", "closed_days": ""},
            {"id": "zz", "name": "不存在的景点", "open_time": "09:00", "close_time": "18:00",
             "ticket": "0", "closed_days": ""},
        ])
        with mock.patch.object(verify, "DATA_DIR", TMP):
            result = verify.import_updates("测试城", csv_path)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], 3)
        joined = " ".join(result["errors"])
        self.assertIn("HH:MM", joined)
        self.assertIn("门票不是数字", joined)
        self.assertIn("找不到对应的景点", joined)

    def test_unchanged_rows_are_not_marked_verified(self):
        """原样返回的 CSV 不能把景点标成已核实。"""
        path = make_city()
        csv_path = self._write_csv([
            {"id": "a1", "name": "景点甲", "open_time": "09:00", "close_time": "18:00",
             "ticket": "0", "closed_days": ""},
        ])
        with mock.patch.object(verify, "DATA_DIR", TMP):
            result = verify.import_updates("测试城", csv_path)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["unchanged"], 1)
        spot = json.loads(path.read_text(encoding="utf-8"))["spots"][0]
        self.assertFalse(spot["verified"])

    def test_explicit_yes_marks_verified_even_if_unchanged(self):
        path = make_city()
        csv_path = TMP / "marked.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=[
                "id", "name", "open_time", "close_time", "ticket", "closed_days", "verified"])
            writer.writeheader()
            writer.writerow({"id": "a1", "name": "景点甲", "open_time": "09:00", "close_time": "18:00",
                             "ticket": "0", "closed_days": "", "verified": "是"})
        with mock.patch.object(verify, "DATA_DIR", TMP):
            result = verify.import_updates("测试城", csv_path)
        self.assertEqual(result["updated"], 1)
        self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["spots"][0]["verified"])

    def test_missing_file_raises(self):
        make_city()
        with mock.patch.object(verify, "DATA_DIR", TMP):
            with self.assertRaises(FileNotFoundError):
                verify.import_updates("测试城", TMP / "nope.csv")


class HealthTest(unittest.TestCase):
    def setUp(self):
        for item in TMP.glob("*"):
            item.unlink()

    def test_health_reports_ratio_and_oldest(self):
        make_city()
        with mock.patch.object(verify, "DATA_DIR", TMP):
            rows = verify.health()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["total"], 2)
        self.assertEqual(row["verified"], 1)
        self.assertAlmostEqual(row["ratio"], 0.5)
        self.assertEqual(row["oldest"], "2026-11-01")


if __name__ == "__main__":
    unittest.main(verbosity=2)
