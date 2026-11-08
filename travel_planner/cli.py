"""命令行入口。

用法：
  python -m travel_planner "成都三天，预算1500，喜欢人文，节奏轻松"
  python -m travel_planner --offline --city 成都 --days 3 --budget 1200
"""
import argparse
from datetime import date

from . import agent_planner, browser_review, data, intent, report, routes, sources, travel_dates


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="travel_planner",
        description="带可行性校验的旅游行程生成器：模型负责编排，代码负责验证。",
    )
    parser.add_argument("request", nargs="?", default="",
                        help="一句话需求，例如 \"成都三天，预算1500，喜欢人文\"")
    parser.add_argument("--city", default=None)
    parser.add_argument("--days", type=int, default=None)
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--prefer", default=None, help="人文 / 自然 / 美食 / 亲子")
    parser.add_argument("--pace", default=None, help="轻松 / 适中 / 紧凑")
    parser.add_argument("--start-date", default="", help="第一天日期 YYYY-MM-DD（优先于文字日期；未指定默认电脑当地今天）")
    parser.add_argument("--start", default=None, help="每天出发时间（默认 09:00）")
    parser.add_argument("--end", default=None, help="每天最晚结束（默认 20:00）")
    parser.add_argument("--offline", action="store_true", help="不调用模型，用规则规划器")
    parser.add_argument("--fetch", choices=["local", "bing", "browser", "cache"], default="local",
                        help="数据来源：local 本地 / bing 必应搜索(RSS)+词典匹配 / browser browser-use 抓取 / cache 缓存")
    parser.add_argument("--model", default=None, help="Ollama 模型名（默认 qwen3:8b）")
    parser.add_argument("--max-rounds", type=int, default=3, help="最多让模型重排几轮（默认 3）")
    parser.add_argument("--out", default="outputs", help="输出目录（默认 outputs）")
    parser.add_argument("--route", choices=["auto", "amap", "estimate"], default="auto",
                        help="交通时长：auto 有key就用高德/amap 强制高德/estimate 直线估算")
    parser.add_argument("--strict", action="store_true", help="校验不通过时以非零码退出")
    parser.add_argument("--no-browser-review", action="store_true", help="跳过官网公告核验和截图")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    current = travel_dates.local_now()
    try:
        requirements = intent.parse_requirements(args.request, use_llm=not args.offline, now=current,
                                                 explicit_start_date=args.start_date) if args.request \
            else intent.Requirements()
        if not args.request:
            requirements.start_date, requirements.date_source = travel_dates.resolve_start_date(
                explicit=args.start_date, now=current)
    except ValueError as exc:
        print(exc)
        return 2
    if args.city:
        requirements.city = args.city
    if args.days:
        requirements.days = args.days
    if args.budget is not None:
        requirements.budget = args.budget
        requirements.budget_source = "用户通过 --budget 指定总预算"
    if args.prefer:
        requirements.prefer = args.prefer
    if args.pace:
        requirements.pace = args.pace
    if args.start:
        requirements.start_time = args.start
    if args.end:
        requirements.end_time = args.end
    if not 1 <= requirements.days <= 14 or requirements.budget <= 0:
        print("天数需在 1~14 之间，预算需大于 0")
        return 2

    start_date = date.fromisoformat(requirements.start_date)

    # 取事实：本地没有这个城市就现抓（--offline 时只读本地）
    auto = not args.offline
    progress = lambda text: print(f"  · {text}")
    spots = None
    if args.fetch != "local":
        try:
            data.ensure_city(requirements.city, auto=auto, progress=progress)
            source = sources.build_source(args.fetch)
            spots = source.fetch(requirements.city)
            print(f"===== 数据来源：{source.name}（{len(spots)} 个景点）=====")
            for note in (getattr(source, "notes", None) or [])[:6]:
                print(f"  · {note}")
        except (RuntimeError, ValueError, OSError, KeyError) as exc:
            print(f"⚠️ 抓取没成功，降级到本地数据：{exc}")
    if spots is None:
        try:
            spots = data.ensure_city(requirements.city, auto=auto, progress=progress)
        except FileNotFoundError as exc:
            print(exc)
            return 2
        print(f"===== 数据来源：本地数据（{len(spots)} 个景点）=====")

    print(f"===== 需求：{requirements.describe()}（理解来源：{requirements.source}）=====")
    print(f"出游日期依据：{requirements.date_source}；电脑时间 {current.isoformat(timespec='seconds')}")
    if not args.offline:
        print("（模型规划中，首次调用可能要等十几秒……）")

    # 交通时长：有 key 且允许联网就用高德路径规划，否则直线估算（逐段可回退）
    route_provider = routes.build_provider(args.route, allow_network=not args.offline)
    if getattr(route_provider, "name", "") != "直线估算":
        print(f"===== 交通：{route_provider.name}（失败的段会回退估算）=====")

    itinerary, result, trace = agent_planner.plan_with_agent(
        requirements, spots, start_date,
        use_llm=not args.offline, model=args.model, max_rounds=args.max_rounds,
        travel=route_provider,
    )
    review = browser_review.collect_evidence(
        requirements.city, spots, itinerary, progress, args.out,
        enabled=not args.offline and not args.no_browser_review,
    )
    itinerary, result = browser_review.apply_review(
        requirements, spots, itinerary, result, review, travel=route_provider, emit=progress,
    )
    if review["changes"] or review.get("ticket_updates"):
        trace.notes = "网页证据或票价参考触发规则重排，沿用用户总预算重新核算；票价证据及核实状态逐项列出。"
        trace.source += " + 网页证据/票价约束重排"
    print(review["message"])
    for item in review["checked"]:
        for page in item.get("pages", []):
            print(f"官网：{page['url']}")
            print(f"截图：{page.get('screenshot_path', '')}")
    stats = getattr(route_provider, "stats", None)
    if stats:
        print(f"交通数据：高德 {stats['api']} 段 / 缓存 {stats['cache']} 段 / 回退 {stats['fallback']} 段")

    print()
    print(trace.summary())
    if trace.notes:
        print(f"行程说明：{trace.notes}")
    print()
    for line in result.summary_lines():
        print(line)
    print()
    for day_result in result.results:
        names = "、".join(leg["name"] for leg in day_result.legs) or "（空）"
        start = day_result.legs[0]["arrive"].strftime("%H:%M") if day_result.legs else "—"
        print(f"Day {day_result.day_index} {day_result.date.isoformat()}"
              f"（周{'一二三四五六日'[day_result.date.weekday()]}）{start}–"
              f"{day_result.end_time.strftime('%H:%M')}：{names}")
    print()
    print(f"费用小计/估算：门票 ¥{result.ticket_total:.2f} + 餐饮 ¥{result.meal_total:.2f} "
          f"+ 市内交通 ¥{result.transport_total:.0f} = ¥{result.total_cost:.0f}"
          + (f"（{'估算结余' if result.budget_left >= 0 else '估算超支'} ¥{abs(result.budget_left):.2f}）"
             if result.budget_complete else "（部分门票待核实，不能确认结余）"))

    md_path, ics_path = report.write_outputs(itinerary, result, args.out, trace)
    print()
    print(f"报告：{md_path}")
    print(f"日历：{ics_path}")

    if args.strict and not result.ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
