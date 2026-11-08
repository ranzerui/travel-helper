"""命令行：生成城市数据文件（data/<city>.json）。

优先级：给了 --seed 用种子+必应核实；否则用高德 POI 搜索；两者都没有才退到冷启动发现。

    python -m travel_planner.ingest_cli --city 西安
    python -m travel_planner.ingest_cli --city 杭州 --limit 12 --force
    python -m travel_planner.ingest_cli --city 苏州 --seed "拙政园,虎丘,平江路"
"""
import argparse
import os

from . import amap, ingest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成城市数据文件（抓取/POI → data/<city>.json）")
    parser.add_argument("--city", required=True)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--source", choices=["auto", "amap", "seed", "discover"], default="auto",
                        help="amap=高德POI（推荐）/ seed=人工种子+必应核实 / discover=冷启动抽取（质量差）")
    parser.add_argument("--seed", default="", help="人工种子景点名，逗号分隔")
    parser.add_argument("--force", action="store_true", help="忽略高德结果缓存，强制重新请求")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    seeds = [item.strip() for item in args.seed.replace("，", ",").split(",") if item.strip()]
    source = args.source
    if source == "auto":
        source = "seed" if seeds else ("amap" if os.environ.get("AMAP_KEY") else "discover")

    try:
        if source == "amap":
            report = amap.build_city(args.city, limit=args.limit, force=args.force)
            label = "高德 POI 搜索"
        else:
            report = ingest.build_for_city(args.city, limit=args.limit,
                                           path=args.out or None, seeds=seeds if source == "seed" else None)
            label = "人工种子 + 必应核实" if source == "seed" else "冷启动抽取（质量不稳定）"
    except (RuntimeError, amap.AmapError) as exc:
        print(f"生成失败：{exc}")
        return 1

    print(f"城市：{report['city']}｜数据来源：{label}｜景点 {report['count']} 个")
    for index, name in enumerate(report["names"], 1):
        print(f"  {index:02d}. {name}")
    print(f"\n数据文件：{report['path']}")
    print("注意：开放时间和票价是默认值（09:00-18:00 / 免费），verified=false，")
    print("     需要人工或官方源核实后再当事实使用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
