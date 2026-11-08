"""把高德 key 写进项目 .env 并做一次连通性检查。

    python -m travel_planner.setup_key YOUR_AMAP_KEY
    python -m travel_planner.setup_key --check      # 只检查现有 key
"""
import argparse

from . import amap, settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="配置高德 Web服务 key（写入 .env）")
    parser.add_argument("key", nargs="?", default="", help="高德 key；不填则只做检查")
    parser.add_argument("--check", action="store_true", help="只验证现有 key 是否可用")
    args = parser.parse_args(argv)

    if args.key:
        path = settings.set_amap_key(args.key)
        print(f"已写入 {path}（.env 已在 .gitignore 里，不会提交）")

    key = settings.amap_key()
    if not key:
        print("没有可用的 AMAP_KEY：网页版只能读本地已有城市的数据。")
        return 1

    masked = key[:6] + "..." + key[-4:] if len(key) > 12 else "***"
    print(f"当前 key：{masked}")
    try:
        pois = amap.search_pois("杭州", keywords="景点", page_size=3)
    except amap.AmapError as exc:
        print(f"连通性检查失败：{exc}")
        return 1
    print(f"连通性正常，返回 {len(pois)} 条 POI")
    for poi in pois[:3]:
        print("  ·", poi.get("name"))
    print("\n现在启动网页版会自动按需生成任意城市的数据。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
