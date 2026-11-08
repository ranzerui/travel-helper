"""用 browser-use 做「证据采集」：打开景点页面 → 截图 + 抽正文 → 提取开放时间。

这是 browser-use 在本项目里最稳的位置，因为**它不依赖模型决策**：

    browser = Browser(...)          # 只启动浏览器
    page = await browser.new_page() # 新建标签页
    await page.goto(url)            # 直达 URL（不经搜索、不点链接）
    text = await page.evaluate(js)  # 取正文
    png  = await page.screenshot()  # 截图存证

相比 `Agent(task=...)` 让模型自己找路：模型会拼错网址、会乱点、会不输 JSON。
这里模型完全不参与，所以可重复、可解释。

产物：
    outputs/evidence/<城市>/<景点>.png   截图（人工核对用）
    outputs/evidence/<城市>/<景点>.json  正文摘要 + 抽到的字段 + 页面 URL
并把候选写进 data 的 suggested 字段（verified 永远是 false）。

用法（用 browser-use 的虚拟环境）：
    python workers\\browser_evidence.py --city 杭州 --limit 2
    ... --city 杭州 --name 雷峰塔 --url https://baike.baidu.com/item/雷峰塔
"""
import argparse
import asyncio
import base64
import json
import sys
import urllib.parse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from travel_planner import data, suggest  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
OUT_DIR = ROOT / "outputs" / "evidence"


SUFFIXES = ("风景区", "景区", "公园", "博物馆", "纪念馆", "旧址", "历史街区", "步行街")


def name_variants(name: str) -> list[str]:
    """百科词条通常不带"景区/公园"后缀，先试原名再去后缀。"""
    variants = [name]
    for suffix in SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            variants.append(name[: -len(suffix)])
    return variants


def candidate_urls(city: str, name: str) -> list[str]:
    """不靠模型猜网址：用固定的百科路径模板 + 名称变体。"""
    urls = []
    for variant in name_variants(name):
        quoted = urllib.parse.quote(variant)
        urls.append("https://baike.baidu.com/item/" + quoted)
        urls.append("https://zh.wikipedia.org/wiki/" + quoted)
    return urls


async def grab(url: str, timeout_ms: int = 20000) -> dict:
    """纯确定性抓取：打开 → 取标题/正文 → 截图。"""
    from browser_use import Browser

    browser = Browser(executable_path=EDGE, headless=True)
    await browser.start()
    try:
        page = await browser.new_page()
        await page.set_viewport_size(1280, 900)
        await asyncio.wait_for(page.goto(url), timeout=timeout_ms / 1000 + 10)
        # 等 JS 渲染完成再取正文和截图（首屏常是脚本填的，拍早了就是白屏）
        await asyncio.sleep(3.0)
        title = await page.get_title()
        text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 6000)"
        )
        final_url = await page.get_url()
        # 连拍两张，取信息量大的那张（第一张经常是白屏）
        shots = []
        for _ in range(2):
            shots.append(await page.screenshot())
            await asyncio.sleep(1.2)
        png = max(shots, key=len)
        return {"ok": True, "title": title, "text": text, "url": final_url, "png": png,
                "png_size": len(png)}
    finally:
        await browser.stop()


def run_one(city: str, name: str, url: str = "") -> dict:
    urls = [url] if url else candidate_urls(city, name)
    last = {"ok": False, "error": "没有可用地址"}
    for candidate in urls:
        try:
            result = asyncio.run(grab(candidate))
        except Exception as exc:  # noqa: BLE001 - 抓取失败要如实返回
            last = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "url": candidate}
            print(f"  失败 {candidate} -> {last['error'][:90]}", file=sys.stderr)
            continue
        text = (result.get("text") or "").strip()
        final_url = result.get("url") or ""
        title = result.get("title") or ""
        if not text:
            last = {"ok": False, "error": "页面正文为空（可能需要登录或反爬）", "url": candidate}
            print(f"  空正文 {candidate}", file=sys.stderr)
            continue
        # 404/错误页也会返回正文，必须识别出来，否则会把错误页当成证据
        if "error.html" in final_url or "404" in title or "页面不存在" in text[:200]:
            last = {"ok": False, "error": f"错误页：{title}", "url": final_url}
            print(f"  错误页 {final_url}", file=sys.stderr)
            continue
        result["requested_url"] = candidate
        return result
    return last


def save(city: str, name: str, result: dict) -> tuple[Path | None, Path | None]:
    folder = OUT_DIR / city
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch for ch in name if ch.isalnum())[:40] or "spot"
    png_path = None
    if result.get("png"):
        png_path = folder / f"{safe}.png"
        png_path.write_bytes(base64.b64decode(result["png"]))
    meta = {
        "city": city,
        "name": name,
        "title": result.get("title"),
        "url": result.get("url"),
        "requested_url": result.get("requested_url"),
        "captured_at": date.today().isoformat(),
        "text_excerpt": (result.get("text") or "")[:1500],
    }
    meta_path = folder / f"{safe}.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return png_path, meta_path


def main() -> int:
    parser = argparse.ArgumentParser(description="browser-use 证据采集（确定性，不用模型）")
    parser.add_argument("--city", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--url", default="")
    parser.add_argument("--limit", type=int, default=2)
    args = parser.parse_args()

    spots = data.load_city(args.city)
    pending = [spot for spot in spots if not spot.verified]
    if args.name:
        pending = [spot for spot in pending if args.name in spot.name or spot.name in args.name]
    if not pending:
        print("没有待处理的景点", file=sys.stderr)
        return 1

    done, hit = 0, 0
    for spot in pending[: args.limit]:
        print(f"\n=== {spot.name} ===", file=sys.stderr)
        result = run_one(args.city, spot.name, args.url)
        if not result.get("ok"):
            print(f"  跳过：{result.get('error')}", file=sys.stderr)
            continue
        done += 1
        png_path, meta_path = save(args.city, spot.name, result)
        extraction = suggest.suggest_from_text(result["text"])
        suggestion = {
            "open_time": extraction["open_time"],
            "close_time": extraction["close_time"],
            "ticket": extraction["ticket"],
            "closed_days": extraction["closed_days"],
            "quote": extraction["quote"],
            "source_url": result.get("url") or "",
            "evidence_png": str(png_path) if png_path else "",
            "method": "browser-use 截图取证",
        }
        if extraction["found"]:
            hit += 1
            suggest.write_suggestion(args.city, spot.name, suggestion)
        print(f"  页面：{result.get('title')}  {result.get('url')}")
        print(f"  截图：{png_path}")
        print(f"  文本：{meta_path}")
        if extraction["found"]:
            print(f"  抽到：开放 {extraction['open_time']}–{extraction['close_time']}｜"
                  f"门票 {extraction['ticket']}｜闭馆 {extraction['closed_days']}")
            print(f"  引用：{extraction['quote']}")
        else:
            print("  正文里没有识别到时间/票价（已存截图供人工看）")

    print(f"\n完成：抓取 {done} 个页面，其中 {hit} 个抽到字段")
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
