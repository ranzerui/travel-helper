"""用 browser-use 抓取景点事实（在 browser-use 自己的虚拟环境里运行）。

产出：一份 JSON（records 已经是 Spot 可构造的完整字段）。
解析、字段补齐、与本地数据合并都交给 travel_planner.fetching（纯函数，可离线测试）。

用法：
    python workers/browser_fetch.py --city 成都 --out cache/fetched_成都.json
    python workers/browser_fetch.py --city 成都 --out out.json --debug-dir debug/
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.parse
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from travel_planner import data, fetching, llm  # noqa: E402

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
# 顺序很重要：国内网络下维基百科经常打不开，先走能访问的页面。
SOURCES = [
    ("必应搜索结果（抓取）", "https://cn.bing.com/search?q=%E6%88%90%E9%83%BD%E6%99%AF%E7%82%B9%E5%BC%80%E6%94%BE%E6%97%B6%E9%97%B4"),
    ("百度百科（抓取）", "https://baike.baidu.com/item/%E6%88%90%E9%83%BD"),
    ("中文维基百科（抓取）", "https://zh.wikipedia.org/wiki/%E6%88%90%E9%83%BD%E5%B8%82"),
]


def preflight(model: str) -> str | None:
    """抓取前先确认模型可用：省得浏览器都起来了才发现连不上 Ollama。"""
    if not llm.available():
        return (f"连不上 Ollama（{llm.DEFAULT_HOST}）。先启动 Ollama 再跑抓取，"
                f"否则 browser-use 会在每一步重试后放弃。")
    try:
        llm.chat("回复 ok", model=model, timeout=90, json_mode=False)
    except llm.LLMError as exc:
        return f"模型 {model} 不可用：{exc}"
    return None


def _dump_debug(debug_dir: Path, name: str, text: str):
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{name}.txt"
    path.write_text(text or "", encoding="utf-8")
    print(f"调试输出：{path}", file=sys.stderr)


async def ask_model(url: str, city: str, model: str, max_steps: int,
                    debug_dir: Path | None, names_only: bool = False,
                    bing_query: str = "") -> str:
    from browser_use import Agent, Browser, ChatOllama

    if bing_query:
        task = fetching.build_bing_task(bing_query, city)
    elif names_only:
        task = fetching.build_names_task(url, city)
    else:
        task = fetching.build_task(url, city)
    browser = Browser(executable_path=EDGE, headless=True)
    agent = Agent(
        task=task,
        llm=ChatOllama(model=model),
        browser=browser,
        flash_mode=True,
        use_vision=False,
        use_thinking=False,
    )
    history = await agent.run(max_steps=max_steps)
    answer = history.final_result() or ""
    if debug_dir is not None:
        _dump_debug(debug_dir, "agent_answer", answer)
    return answer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="成都")
    parser.add_argument("--url", default="")
    parser.add_argument("--model", default=os.environ.get("TRAVEL_MODEL", "qwen3:8b"))
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--out", required=True)
    parser.add_argument("--debug-dir", default="")
    parser.add_argument("--no-local-merge", action="store_true")
    parser.add_argument("--names-only", action="store_true",
                        help="窄任务：只抽景点名称，不要简介和链接（小模型更稳）")
    parser.add_argument("--bing", default="",
                        help="以必应搜索为入口，例如 --bing \"成都景点\"" )
    args = parser.parse_args()

    debug_dir = Path(args.debug_dir) if args.debug_dir else None
    local_spots = []
    if not args.no_local_merge:
        try:
            local_spots = data.load_city(args.city)
        except FileNotFoundError as exc:
            print(f"提示：{exc}，本次不做本地字段合并", file=sys.stderr)

    if args.bing:
        query = urllib.parse.quote(args.bing)
        candidates = [(f"必应搜索·{args.bing}（抓取）", f"https://cn.bing.com/search?q={query}")]
    else:
        candidates = [(args.url, args.url)] if args.url else SOURCES
    captured_at = date.today().isoformat()

    problem = preflight(args.model)
    if problem:
        print(problem, file=sys.stderr)
        return 2

    for source_name, url in candidates:
        print(f"尝试：{source_name} {url}", file=sys.stderr)
        try:
            answer = asyncio.run(ask_model(url, args.city, args.model, args.max_steps,
                                           debug_dir, names_only=args.names_only,
                                           bing_query=args.bing))
        except Exception as exc:  # noqa: BLE001 - 抓取失败要原样报告给调用方
            print(f"这一路失败：{exc}", file=sys.stderr)
            continue

        records, notes = fetching.records_from_answer(
            answer, args.city, source_name, url, captured_at, local_spots=local_spots)
        if not records:
            print(f"解析失败：{'；'.join(notes)}", file=sys.stderr)
            continue

        payload = {
            "city": args.city,
            "source_name": source_name,
            "source_url": url,
            "captured_at": captured_at,
            "notes": notes,
            "spots": records,
        }
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"抓取完成：{len(records)} 个景点 -> {target}")
        for note in notes:
            print(f"  · {note}")
        return 0

    print("所有来源都没成功；主流程会降级到本地数据", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
