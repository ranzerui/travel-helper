"""城市名录：判断"这个词是不是一个真城市"。

为什么需要：
正则只能猜"城市长什么样"，猜不出"景色好"不是城市。识别错城市的根因不是语序，
是**没有校验**——只要位置像城市就认了。所以这里维护一份全国城市名录，
抽取出来的候选必须在这份名单里才算数。

名录来源：高德行政区划接口（省级→地级，一次请求拿全），存成 data/cities.json，
之后完全离线可用。新增城市只需要跑一次 `python -m travel_planner.citydb refresh`。
"""
import json
from pathlib import Path

from .data import DATA_DIR


# 城市名常见后缀，用于"大理"→"大理白族自治州"这类前缀匹配
SUFFIXES = ("市", "地区", "盟", "自治州", "自治县", "特别行政区")
_NAMES: set[str] | None = None


def refresh() -> dict:
    """从高德拉一份城市名录并落盘（需要 AMAP_KEY，一次就够）。"""
    from . import amap

    payload = amap._get("/v3/config/district", {
        "keywords": "中国", "subdistrict": "3", "extensions": "base",
    })
    root = (payload.get("districts") or [{}])[0]
    cities = []
    for province in root.get("districts") or []:
        for item in province.get("districts") or []:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            cities.append({
                "name": name,
                "short": _short(name),
                "level": item.get("level", ""),
                "adcode": item.get("adcode", ""),
            })
    if not cities:
        raise RuntimeError("高德没有返回城市名录")
    target = _city_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"count": len(cities), "cities": cities},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    global _NAMES
    _NAMES = None
    return {"count": len(cities), "path": str(_city_file())}


def _short(name: str) -> str:
    for suffix in SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix) + 1:
            return name[: -len(suffix)]
    return name


def _city_file() -> Path:
    from . import data

    return data.DATA_DIR / "cities.json"


def _load() -> set[str]:
    global _NAMES
    if _NAMES is not None:
        return _NAMES
    names: set[str] = set()
    path = _city_file()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            for item in payload.get("cities", []):
                if item.get("name"):
                    names.add(item["name"])
                if item.get("short"):
                    names.add(item["short"])
        except (OSError, json.JSONDecodeError):
            pass
    # 注意：不要再把 data/ 下的文件名当城市名。
    # 否则一旦某个错词（比如"景色好"）被误抓并落盘，它就会自己认证自己，永远错下去。
    return names


def available() -> bool:
    """名录是否可用（没有名录时不能做校验，只能放行）。"""
    return bool(_load())


def is_city(name: str) -> bool:
    """候选词是不是城市：精确命中，或者是某个城市名的前缀（大理→大理白族自治州）。

    名录不可用时放行（宁可识别错也不能全不认），但会由调用方给出提示。
    """
    name = (name or "").strip()
    if not 2 <= len(name) <= 10:
        return False
    names = _load()
    if not names:
        return True
    if name in names:
        return True
    for candidate in names:
        if len(candidate) > len(name) and candidate.startswith(name):
            return True
    return False


def all_names() -> list[str]:
    return sorted(_load())


def count() -> int:
    return len(_load())


# ---------- 县级市/旅游城市兜底 ----------
CHECK_CACHE = Path(__file__).resolve().parent.parent / "cache" / "city_check.json"
_CACHE: dict | None = None


def _load_check_cache() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        _CACHE = json.loads(CHECK_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _CACHE = {}
    return _CACHE


def _save_check_cache() -> None:
    try:
        CHECK_CACHE.parent.mkdir(parents=True, exist_ok=True)
        CHECK_CACHE.write_text(json.dumps(_CACHE or {}, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except OSError:
        pass


def lookup_online(name: str) -> bool:
    """名录（只到地级市）查不到时，用高德行政区接口确认一次，结果缓存。

    用途：赤壁、敦煌、义乌这类县级市/旅游城市。
    """
    cache = _load_check_cache()
    if name in cache:
        return bool(cache[name])
    try:
        from . import amap

        payload = amap._get("/v3/config/district", {
            "keywords": name, "subdistrict": "0", "extensions": "base",
        })
    except Exception:  # 网络/配额问题不能影响主流程
        return False
    ok = False
    for item in payload.get("districts") or []:
        district = str(item.get("name") or "")
        level = str(item.get("level") or "")
        if level not in {"city", "district", "province"}:
            continue
        if district == name or (district.startswith(name) and len(district) <= len(name) + 8):
            ok = True
            break
    cache[name] = ok
    _save_check_cache()
    return ok


def is_city_anywhere(name: str, online: bool = True) -> bool:
    """先查本地名录，再（可选）联网确认一次。"""
    if is_city(name):
        return True
    if not online:
        return False
    return lookup_online(name)
