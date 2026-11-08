"""用 browser-use 检查官网"开放公告"：抓临时闭馆/停开/限流，写风险提示。

这是 browser-use 在本项目里**不可替代**的地方：
高德 POI 给不出"武英殿 2026-10-19 起停止开放"这种临时公告，只有网页有。

确定性抓取（不让模型决策）：
    打开公告页 → 取正文 → 用规则抽"公告标题 + 日期 + 关键词" → 落 JSON

用法（browser-use 的虚拟环境）：
    python workers\\notice_watch.py `
        --city 北京 --url https://www.dpm.org.cn/Visit.html
"""
import argparse
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT_DIR = ROOT / "outputs" / "evidence"

# 公告里出现这些词，说明行程可能受影响
RISK_WORDS = ("停止开放", "暂停开放", "闭馆", "维修", "施工", "限流", "预约", "调整开放", "临时关闭")
DATE_RE = re.compile(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*[月\-/.]\s*(\d{1,2})")


async def grab_text(url: str, timeout: float = 25) -> tuple[str, str]:
    from browser_use import Browser

    browser = Browser(executable_path=EDGE, headless=True)
    await browser.start()
    try:
        page = await browser.new_page()
        await page.set_viewport_size(1280, 900)
        await asyncio.wait_for(page.goto(url), timeout=timeout)
        await asyncio.sleep(3.0)
        title = await page.get_title()
        text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 20000)"
        )
        return title or "", text or ""
    finally:
        await browser.stop()


def extract_notices(text: str, limit: int = 8) -> list[dict]:
    """从正文里挑出"带风险词"的行，配上日期。"""
    notices = []
    seen = set()
    for raw in re.split(r"[\n\r]+", text):
        line = raw.strip()
        if not (8 <= len(line) <= 160):
            continue
        if not any(word in line for word in RISK_WORDS):
            continue
        if line in seen:
            continue
        seen.add(line)
        match = DATE_RE.search(line)
        day = ""
        if match:
            year, month, day_of_month = (int(item) for item in match.groups())
            try:
                day = date(year, month, day_of_month).isoformat()
            except ValueError:
                day = ""
        notices.append({"text": line, "date": day})
        if len(notices) >= limit:
            break
    return notices


def main() -> int:
    parser = argparse.ArgumentParser(description="browser-use 公告检查（确定性抓取）")
    parser.add_argument("--city", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--name", default="", help="关联的景点名（可选）")
    args = parser.parse_args()

    print(f"打开：{args.url}", file=sys.stderr)
    try:
        title, text = asyncio.run(grab_text(args.url))
    except Exception as exc:  # noqa: BLE001 - 抓不到要如实报告
        print(f"抓取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if not text.strip():
        print("页面正文为空（可能需要登录/反爬）", file=sys.stderr)
        return 1

    notices = extract_notices(text)
    folder = OUT_DIR / args.city
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "announcements.json"
    payload = {
        "city": args.city,
        "spot": args.name,
        "url": args.url,
        "title": title,
        "captured_at": date.today().isoformat(),
        "notices": notices,
    }
    existing = []
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8")).get("sources", [])
        except (OSError, json.JSONDecodeError):
            existing = []
    payload["sources"] = existing + [args.url]
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"页面：{title}")
    print(f"风险提示 {len(notices)} 条：")
    for notice in notices:
        stamp = f"[{notice['date']}] " if notice["date"] else ""
        print("  ·", stamp + notice["text"][:80])
    print(f"\n已写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
