"""命令行：数据核实闭环。

    # 1) 导出待核实清单（Excel 打开即可填）
    python -m travel_planner.verify_cli export --city 杭州 --out pending_杭州.csv

    # 2) 用 Excel / WPS 填 open_time / close_time / ticket / closed_days 四列，保存

    # 3) 回导，填对的行会被标成 verified=true
    python -m travel_planner.verify_cli import --city 杭州 --csv pending_杭州.csv

    # 4) 看所有城市的数据健康度
    python -m travel_planner.verify_cli health
"""
import argparse
from pathlib import Path

from . import verify


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="导出/回导待核实清单，统计数据健康度")
    sub = parser.add_subparsers(dest="command", required=True)

    export_cmd = sub.add_parser("export", help="导出待核实清单")
    export_cmd.add_argument("--city", required=True)
    export_cmd.add_argument("--out", default="")
    export_cmd.add_argument("--all", action="store_true", help="连已核实的也导出（便于批量修改）")

    import_cmd = sub.add_parser("import", help="回导填好的清单")
    import_cmd.add_argument("--city", required=True)
    import_cmd.add_argument("--csv", required=True)

    sub.add_parser("health", help="查看所有城市的数据健康度")

    args = parser.parse_args(argv)

    if args.command == "export":
        target = Path(args.out or f"pending_{args.city}.csv")
        try:
            count = verify.export_pending(args.city, target, only_pending=not args.all)
        except FileNotFoundError as exc:
            print(exc)
            return 1
        print(f"已导出 {count} 条待核实记录：{target}")
        print("填写 open_time / close_time / ticket / closed_days 四列后，用 import 回导。")
        return 0

    if args.command == "import":
        try:
            result = verify.import_updates(args.city, Path(args.csv))
        except FileNotFoundError as exc:
            print(exc)
            return 1
        print(f"更新 {result['updated']} 条｜未改动 {result['unchanged']} 条｜格式错误跳过 {result['skipped']} 条")
        for error in result["errors"][:10]:
            print("  ·", error)
        if result["updated"]:
            print(f"数据文件已更新：{result['path']}")
        return 0 if result["skipped"] == 0 else 1

    rows = verify.health()
    if not rows:
        print("没有城市数据")
        return 0
    print(f"{'城市':<8}{'类型':<10}{'景点':>4}{'已核实':>7}{'比例':>8}  最老核实   来源")
    for row in rows:
        sources = "、".join(f"{name}×{count}" for name, count in row["sources"].items())
        print(f"{row['city']:<8}{row['kind']:<10}{row['total']:>4}{row['verified']:>7}"
              f"{row['ratio'] * 100:>7.0f}%  {row['oldest'] or '—':<10} {sources}")
    total = sum(row["total"] for row in rows)
    verified = sum(row["verified"] for row in rows)
    if total:
        print(f"\n合计 {total} 个景点，已核实 {verified} 个（{verified / total * 100:.0f}%）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
