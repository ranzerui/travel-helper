"""用 browser-use 去"找证据"：给一个景点，抓它的开放时间/票价/闭馆日。

定位非常重要：**它只产出"候选 + 原文引用"，绝不把 verified 改成 true。**
人工看一眼 quote，确认后才用 verify_cli import 变成已核实。

为什么走必应 RSS 而不是官网首页：
- RSS 是静态 XML，browser-use 读得到文字；HTML 搜索页是 JS 渲染，经常只剩噪音；
- 官网首页往往要点击多层导航，8B 模型容易迷路。

用法（在 browser-use 的虚拟环境里跑）：
    python workers\\verify_browser.py --city 杭州 --name 雷峰塔
    ... --city 杭州 --limit 3 --debug-dir debug/

结果写进 travel_planner/data/<城市>.json 的 suggested 字段。
"""
import argparse
import asyncio
import json
import os
import re
import sys
import urllib.parse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from travel_planner import data, llm  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"


def bing_rss_url(city: str, name: str) -> str:
    query = urllib.parse.quote(f"{city} {name} 开放时间 门票 闭馆")
    return f"https://cn.bing.com/search?q={query}&format=rss"


def build_task(city: str, name: str, read_only: bool = True) -> str:
    rules = (
        "1. 只读取当前页面已经显示的文字；禁止点击任何链接、禁止跳转、禁止去别处搜索；\n"
        if read_only else
        "1. 可以在页面上点击，但不要凭记忆拼网址；\n"
    )
    return (
        f"这是必应 RSS 搜索结果页（纯文本 XML）。请找出「{name}」（{city}）的"
        f"开放时间、门票价格、每周固定闭馆日。\n"
        "严格要求：\n"
        f"{rules}"
        "2. 只依据页面文字，不要用你的记忆补充，不要拼写或猜测网址；\n"
        "3. 找不到的字段写 null；找不到就直接 found=false，不要继续尝试；\n"
        "4. quote 必须是从页面原样摘出的一小句（20 字内），用于人工核对；\n"
        "5. 最后只输出一个 JSON，不要解释：\n"
        '{"found": true/false, "open_time": "HH:MM 或 null", "close_time": "HH:MM 或 null",'
        ' "ticket": 数字或 null, "closed_days": ["周一"] 或 [], "quote": "原句", "source_url": "当前页面地址"}'
    )


def parse_json(text: str) -> dict | None:
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def valid_time(value) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return f"{hour:02d}:{minute:02d}"
    return None


def normalize(payload: dict) -> dict:
    """只保留能验证形状的字段，宁缺毋滥。"""
    ticket = payload.get("ticket")
    try:
        ticket = float(ticket) if ticket is not None else None
    except (TypeError, ValueError):
        ticket = None
    if ticket is not None and not (0 <= ticket <= 2000):
        ticket = None
    closed = payload.get("closed_days") or []
    closed = [str(item) for item in closed if str(item) in
              {"周一", "周二", "周三", "周四", "周五", "周六", "周日"}]
    return {
        "found": bool(payload.get("found")),
        "open_time": valid_time(payload.get("open_time")),
        "close_time": valid_time(payload.get("close_time")),
        "ticket": ticket,
        "closed_days": closed,
        "quote": str(payload.get("quote") or "")[:80],
        "source_url": str(payload.get("source_url") or "")[:300],
    }


async def run_agent(url: str, city: str, name: str, model: str, max_steps: int,
                    debug_dir: Path | None, read_only: bool = True) -> str:
    from browser_use import Agent, Browser, ChatOllama

    agent = Agent(
        task=build_task(city, name, read_only) + f"\n入口页面：{url}",
        llm=ChatOllama(model=model),
        browser=Browser(executable_path=EDGE, headless=True),
        flash_mode=True,
        use_vision=False,
        use_thinking=False,
    )
    history = await agent.run(max_steps=max_steps)
    answer = history.final_result() or ""
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)
        (debug_dir / f"verify_{name}.txt").write_text(answer or "", encoding="utf-8")
    return answer


def write_suggestion(city: str, name: str, suggestion: dict) -> Path:
    """把候选写进数据文件。绝不改 verified。"""
    path = data._locate(city)
    payload = json.loads(path.read_text(encoding="utf-8"))
    for spot in payload.get("spots", []):
        if spot.get("name") != name:
            continue
        spot["suggested"] = suggestion
        spot["suggested_at"] = date.today().isoformat()
        break
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", required=True)
    parser.add_argument("--name", default="", help="只处理这个景点")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--model", default=os.environ.get("TRAVEL_MODEL", "qwen3:8b"))
    parser.add_argument("--max-steps", type=int, default=3)
    parser.add_argument("--debug-dir", default="")
    parser.add_argument("--allow-click", action="store_true",
                        help="允许点击链接（默认只读当前页，防止模型拼错网址）")
    args = parser.parse_args()

    if not llm.available():
        print(f"连不上 Ollama（{llm.DEFAULT_HOST}），先启动模型", file=sys.stderr)
        return 2

    spots = data.load_city(args.city)
    pending = [spot for spot in spots if not spot.verified]
    if args.name:
        pending = [spot for spot in pending if args.name in spot.name or spot.name in args.name]
    if not pending:
        print("没有待核实的景点（或者名字没对上）", file=sys.stderr)
        return 1

    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    ok = 0
    for spot in pending[: args.limit]:
        url = bing_rss_url(args.city, spot.name)
        print(f"\n=== {spot.name} ===\n入口：{url}", file=sys.stderr)
        try:
            answer = asyncio.run(run_agent(url, args.city, spot.name, args.model,
                                           args.max_steps, debug_dir,
                                           read_only=not args.allow_click))
        except Exception as exc:  # noqa: BLE001 - 抓取失败要原样报告
            print(f"失败：{exc}", file=sys.stderr)
            continue
        payload = parse_json(answer)
        if not payload:
            print(f"模型没给出可解析的 JSON：{(answer or '')[:120]}", file=sys.stderr)
            continue
        suggestion = normalize(payload)
        path = write_suggestion(args.city, spot.name, suggestion)
        ok += 1
        print(f"候选：开放 {suggestion['open_time']}–{suggestion['close_time']}｜"
              f"门票 {suggestion['ticket']}｜闭馆 {suggestion['closed_days']}")
        print(f"引用：{suggestion['quote'] or '（无）'}")
        print(f"来源：{suggestion['source_url'] or '（无）'}")
        print(f"已写入 {path}（verified 仍是 false，等你确认）")

    print(f"\n完成：{ok}/{min(len(pending), args.limit)} 个景点拿到候选")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
