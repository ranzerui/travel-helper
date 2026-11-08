"""为待核实景点抓候选值（必应 RSS + 规则抽取），写入 suggested 字段。

    python -m travel_planner.suggest_cli --city 杭州
    python -m travel_planner.suggest_cli --city 杭州 --name 雷峰塔 --limit 3

之后：
    python -m travel_planner.verify_cli export --city 杭州 --out pending.csv   # 候选已预填
    # 看一眼 evidence 列（引用），确认或改正
    python -m travel_planner.verify_cli import --city 杭州 --csv pending.csv   # 才变 verified
"""
import argparse

from . import data, suggest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="抓取开放时间/票价候选（Bing RSS + 规则）")
    parser.add_argument("--city", required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args(argv)

    spots = data.load_city(args.city)
    pending = [spot for spot in spots if not spot.verified]
    if args.name:
        pending = [spot for spot in pending if args.name in spot.name]
    if not pending:
        print("没有待核实的景点")
        return 1

    hits = 0
    for spot in pending[: args.limit]:
        suggestion = suggest.suggest_for(args.city, spot.name)
        path = suggest.write_suggestion(args.city, spot.name, suggestion)
        status = "命中" if suggestion.get("found") else "未命中"
        if suggestion.get("found"):
            hits += 1
        print(f"\n【{status}】{spot.name}")
        if suggestion.get("open_time"):
            print(f"  开放：{suggestion['open_time']}–{suggestion['close_time']}")
        if suggestion.get("ticket") is not None:
            print(f"  门票：¥{suggestion['ticket']:.0f}")
        if suggestion.get("closed_days"):
            print(f"  闭馆：{suggestion['closed_days']}")
        if suggestion.get("quote"):
            print(f"  引用：{suggestion['quote']}")
        if path:
            print(f"  已写入 {path.name}（verified 仍是 false）")

    print(f"\n命中 {hits}/{min(len(pending), args.limit)}")
    print("下一步：verify_cli export → 看 evidence 列 → import 才标成已核实")
    return 0
