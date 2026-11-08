"""数据层：从本地 JSON 读景点。

真实项目里这一层换成 OSM / 官网抓取，接口保持不变即可（load_city 返回 list[Spot]）。
"""
import dataclasses
import json
from datetime import date
from pathlib import Path

from .models import Spot

DATA_DIR = Path(__file__).with_name("data")
_FIELDS = {field.name for field in dataclasses.fields(Spot)}


def _locate(city: str) -> Path:
    """先按文件名找，再按 JSON 里的 city 字段找（支持中文城市名）。"""
    direct = DATA_DIR / f"{city}.json"
    if direct.exists():
        return direct
    for candidate in sorted(DATA_DIR.glob("*.json")):
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("city") == city:
            return candidate
    available = "、".join(available_cities()) or "无"
    raise FileNotFoundError(
        f"没有 {city} 的数据（当前可用：{available}）；"
        f"加城市只需在 travel_planner/data/ 下放一个 {city}.json"
    )


def available_cities() -> list[str]:
    """列出有数据文件的城市（优先取 JSON 里的 city 字段）。"""
    names = []
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name == "cities.json":  # 城市名录，不是城市数据
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not payload.get("spots"):
            continue
        names.append(str(payload.get("city") or path.stem))
    return names


def load_file(path) -> list[Spot]:
    """从任意 JSON 文件读景点（测试用固定数据，不依赖 data/ 目录）。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if "spots" not in payload:
        raise ValueError(f"{Path(path).name} 不是城市数据文件（缺少 spots）")
    spots = []
    for item in payload["spots"]:
        item = dict(item)
        item["closed_days"] = tuple(item.get("closed_days", []))
        item.setdefault("verified", False)
        # 生成的数据文件可能带 seed/kind 之类的元字段，只取 Spot 认识的
        item = {key: value for key, value in item.items() if key in _FIELDS}
        spots.append(Spot(**item))
    return spots


def load_city(city: str) -> list[Spot]:
    return load_file(_locate(city))


def open_on(spot: Spot, day: date) -> bool:
    """依据固定闭馆日与官网核验生成的临时关闭日期判断。"""
    return day.weekday() not in spot.closed_days and day.isoformat() not in spot.closed_dates


def ensure_city(city: str, auto: bool = False, progress=None) -> list[Spot]:
    """按需获取城市数据：本地没有就现抓（高德 POI），生成 data/<city>.json。

    这是"任意城市都能用"的关键：城市数据不再是预先备好的资产，而是按需产物。
    auto=False 时保持原来的行为（只读本地文件），供离线模式使用。

    注意：校验必须在**读文件之前**做——否则一旦有人生成过 data/风景好.json，
    它就会绕过校验一直可用（错误数据自我认证，实践里真踩过）。
    """
    from . import citydb

    if citydb.available() and not citydb.is_city_anywhere(city, online=True):
        raise FileNotFoundError(
            f"「{city}」不在城市名录里，看起来不是城市名。"
            f"已有数据：{'、'.join(available_cities())}"
        )

    try:
        return load_city(city)
    except FileNotFoundError:
        if not auto:
            raise

    from . import amap  # 延迟导入：只有需要现抓时才依赖它

    if progress:
        progress(f"本地没有 {city} 的数据，正在用高德 POI 现抓（第一次会慢一点）……")
    try:
        info = amap.build_city(city, limit=12)
    except (amap.AmapError, RuntimeError) as exc:
        raise FileNotFoundError(
            f"{city} 的数据不存在，现抓也失败了：{exc}。"
            f"可用 --offline 看已有城市（{'、'.join(available_cities())}），"
            f"或先跑 ingest_cli 生成数据"
        ) from exc
    if progress:
        progress(f"已生成 {info['count']} 个景点：{info['path']}")
    return load_city(city)
