"""命令行：维护城市名录。

    python -m travel_planner.citydb refresh   # 从高德拉全国城市名录（一次）
    python -m travel_planner.citydb check 昆明 # 检查某个词是不是城市
    python -m travel_planner.citydb count
"""
import argparse

from . import citydb


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="城市名录维护")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("refresh", help="从高德拉取全国城市名录")
    check = sub.add_parser("check", help="检查一个词是不是城市")
    check.add_argument("name")
    sub.add_parser("count", help="名录里有多个城市")

    args = parser.parse_args(argv)

    if args.command == "refresh":
        try:
            info = citydb.refresh()
        except RuntimeError as exc:
            print(f"刷新失败：{exc}")
            return 1
        print(f"已写入 {info['count']} 个城市：{info['path']}")
        return 0

    if args.command == "check":
        result = citydb.is_city(args.name)
        print(f"{args.name} -> {'是城市' if result else '不是城市'}")
        return 0 if result else 1

    print(f"名录共 {citydb.count()} 个名字（含简称）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
