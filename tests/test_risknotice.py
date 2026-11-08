"""报告里的"官网公告风险提示"：有公告就附上，没公告就不出现这一段。"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import data, planner, report

FIXTURE = Path(__file__).with_name("fixtures") / "成都.json"
TMP = Path(__file__).with_name("_tmp_evidence")


class RiskLinesTest(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        for item in TMP.glob("**/*"):
            if item.is_file():
                item.unlink()

    def test_no_announcement_means_no_section(self):
        self.assertEqual(report._risk_lines("成都", evidence_dir=TMP), [])

    def test_announcement_becomes_risk_section(self):
        folder = TMP / "北京"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "announcements.json").write_text(json.dumps({
            "city": "北京",
            "url": "https://www.dpm.org.cn/Visit.html",
            "captured_at": "2026-11-06",
            "notices": [
                {"text": "武英殿将于2026年10月19日起停止开放", "date": "2026-10-19"},
                {"text": "延禧宫暂停开放，进行保护性勘察", "date": ""},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        lines = report._risk_lines("北京", evidence_dir=TMP)
        text = "\n".join(lines)
        self.assertIn("风险提示", text)
        self.assertIn("武英殿", text)
        self.assertIn("2026-10-19", text)
        self.assertIn("dpm.org.cn", text)

    def test_markdown_includes_risk_when_evidence_exists(self):
        folder = TMP / "成都"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "announcements.json").write_text(json.dumps({
            "city": "成都", "url": "https://example.test/notice", "captured_at": "2026-11-06",
            "notices": [{"text": "杜甫草堂因维修暂停开放", "date": "2026-11-05"}],
        }, ensure_ascii=False), encoding="utf-8")

        spots = data.load_file(FIXTURE)
        itinerary, result = planner.plan("成都", spots, days=1, budget=1500)
        text = report.to_markdown(itinerary, result, evidence_dir=TMP)
        self.assertIn("风险提示", text)
        self.assertIn("杜甫草堂因维修暂停开放", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
