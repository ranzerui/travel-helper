"""高德 POI 搜索：用它给一座城市"发现"景点，替代人工种子。

定位：这是**数据生成**用的，不是每次规划都调用（省配额）。
产出仍然是 `data/<city>.json`，字段 `verified=false`——高德给名字/坐标/地址，
不给开放时间和票价，后者仍要人工或官网核实。

接口（Web服务）：
- v5 搜索 POI： /v5/place/text
- v5 路径规划： /v5/direction/walking （预留，未启用）

配额保护：
- 结果缓存到 cache/amap_<city>.json，默认 7 天内不重复请求；
- 只按类别做少量请求（3~4 次/城市），不做逐页翻页。
"""
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

BASE = "https://restapi.amap.com"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_TTL_HOURS = 24 * 7
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 查询策略：高德没有"知名度"排序，但 keywords + sortrule=weight 出来的结果明显更像景点；
# 只按 types 查会返回村级文化广场这类 POI，所以两者都用。
QUERY_SPECS = [
    ("景点", "110000"),
    ("公园", "110101"),
    ("博物馆", "140100"),
]
EXCLUDE_WORDS = ("地铁站", "公交站", "停车场", "售票", "入口", "出口", "厕所", "服务中心",
                 "游客中心", "派出所", "银行", "药店", "酒店", "宾馆", "饭店")


class AmapError(RuntimeError):
    pass


TIME_RANGE = re.compile(r"(\d{1,2})[:：](\d{2})\s*[-–—~至到]\s*(\d{1,2})[:：](\d{2})")
SEASON = re.compile(r"(\d{1,2})-(\d{1,2})至(\d{1,2})-(\d{1,2})")


def _hhmm(hour: str, minute: str) -> str | None:
    h, m = int(hour), int(minute)
    if 0 <= h <= 23 and 0 <= m <= 59:
        return f"{h:02d}:{m:02d}"
    return None


def parse_opentime(business: dict | None, today=None) -> dict:
    """从高德 business.opentime_week 解析营业时间（这是我们之前漏掉的数据源）。

    格式形如：
      03-16至04-30 周一至周日 08:00-19:00 最晚进入18:30；05-01至10-31 … 11-01至03-15 …
    优先取"今天落在哪个季节区间"的那段，解析不出来取第一段。
    """
    business = business or {}
    raw = str(business.get("opentime_week") or business.get("opentime_today") or "").strip()
    result = {"open_time": None, "close_time": None, "raw": raw, "season": ""}
    if not raw:
        return result

    segments = [seg.strip() for seg in re.split(r"[；;]", raw) if seg.strip()]
    day = today or date.today()
    today_key = (day.month, day.day)
    chosen, chosen_season = None, ""
    for seg in segments:
        match = SEASON.search(seg)
        if not match:
            continue
        start = (int(match.group(1)), int(match.group(2)))
        end = (int(match.group(3)), int(match.group(4)))
        inside = start <= today_key <= end if start <= end else (today_key >= start or today_key <= end)
        if inside:
            chosen, chosen_season = seg, match.group(0)
            break
    if chosen is None:
        chosen = next((seg for seg in segments if TIME_RANGE.search(seg)),
                      segments[0] if segments else "")
    time_match = TIME_RANGE.search(chosen)
    if not time_match:
        return result
    open_time = _hhmm(time_match.group(1), time_match.group(2))
    close_time = _hhmm(time_match.group(3), time_match.group(4))
    if open_time and close_time and open_time < close_time:
        result["open_time"] = open_time
        result["close_time"] = close_time
        result["season"] = chosen_season
    return result


def api_key() -> str:
    from . import settings

    key = settings.amap_key()
    if not key:
        raise AmapError(
            "没有配置高德 key：设置 AMAP_KEY 环境变量，或写入项目根目录 .env（AMAP_KEY=你的key）"
        )
    return key


def _get(path: str, params: dict, timeout: float = 20, retries: int = 3) -> dict:
    params = {**params, "key": api_key()}
    url = f"{BASE}{path}?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "travel-planner/0.1"})
    last_error = None
    for attempt in range(retries):
        try:
            with _OPENER.open(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last_error = AmapError(f"高德请求失败：{exc}")
            time.sleep(1 + attempt)
            continue
        if str(payload.get("status")) == "1":
            return payload
        code = str(payload.get("infocode"))
        # 10021 = 每秒请求超限，退避后重试；其它错误直接抛
        if code == "10021" and attempt < retries - 1:
            time.sleep(1.2 * (attempt + 1))
            continue
        last_error = AmapError(f"高德返回错误：{payload.get('info')}（{code}）")
        break
    raise last_error or AmapError("高德请求失败：未知原因")


def search_pois(city: str, types: str = "", keywords: str = "",
                page_size: int = 20, page: int = 1, timeout: float = 20,
                sortrule: str = "weight") -> list[dict]:
    params = {"region": city, "city_limit": "true", "page_size": page_size,
              "page_num": page, "show_fields": "business"}
    if types:
        params["types"] = types
    if keywords:
        params["keywords"] = keywords
    if sortrule:
        params["sortrule"] = sortrule
    payload = _get("/v5/place/text", params, timeout=timeout)
    return payload.get("pois") or []


def _location(poi: dict) -> tuple[float, float]:
    raw = str(poi.get("location") or "")
    if "," not in raw:
        return 0.0, 0.0
    lon, lat = raw.split(",", 1)
    try:
        return float(lat), float(lon)
    except ValueError:
        return 0.0, 0.0


def _is_usable(poi: dict) -> bool:
    name = str(poi.get("name") or "").strip()
    if not name or any(word in name for word in EXCLUDE_WORDS):
        return False
    typecode = str(poi.get("typecode") or "")
    return typecode.startswith(("110", "140"))


def _short_name(name: str) -> str:
    """把"杭州西湖风景名胜区-断桥残雪"收成"断桥残雪"，太短就保留原名。"""
    if "-" in name:
        left, right = name.split("-", 1)
        right = right.strip()
        if len(right) >= 2:
            return right
    return name.strip()


def discover(city: str, per_query: int = 20, timeout: float = 20) -> list[dict]:
    """按几个关键词各查一次（按权重排序），合并去重。返回原始 POI 记录。"""
    seen, result = set(), []
    for keyword, types in QUERY_SPECS:
        for poi in search_pois(city, keywords=keyword, types=types,
                               page_size=per_query, timeout=timeout):
            if not _is_usable(poi):
                continue
            full_name = str(poi.get("name")).strip()
            name = _short_name(full_name)
            if name in seen:
                continue
            seen.add(name)
            lat, lon = _location(poi)
            business = poi.get("business") or {}
            hours = parse_opentime(business)
            result.append({
                "name": name,
                "lat": lat,
                "lon": lon,
                "address": str(poi.get("address") or ""),
                "category": keyword,
                "typecode": str(poi.get("typecode") or ""),
                "full_name": full_name,
                "open_time": hours["open_time"],
                "close_time": hours["close_time"],
                "opentime_raw": hours["raw"],
                "rating": str(business.get("rating") or ""),
                "tel": str(business.get("tel") or ""),
            })
    return result


def to_spot_records(pois: list[dict], city: str, limit: int = 12) -> list[dict]:
    """POI → Spot 记录。

    开放时间来自高德 business.opentime_week（有就用，没有才退到 09:00-18:00）；
    门票高德不给，仍是 0。verified 一律 false——POI 的季节/临时调整不算核实。
    """
    today = date.today().isoformat()
    records = []
    for index, poi in enumerate(pois[:limit]):
        slug = "".join(ch for ch in poi["name"] if ch.isalnum())[:24] or f"poi{index}"
        category = {"博物馆": "人文", "公园": "自然", "景点": "人文"}.get(poi["category"], "人文")
        records.append({
            "id": f"amap_{slug}_{index}",
            "name": poi["name"],
            "city": city,
            "category": category,
            "lat": poi["lat"],
            "lon": poi["lon"],
            "open_time": poi.get("open_time") or "09:00",
            "close_time": poi.get("close_time") or "18:00",
            "duration_min": 90,
            "ticket": 0.0,
            "closed_days": [],
            "desc": poi["address"][:120],
            "source_name": (f"高德 POI（{poi['category']}·营业时间）"
                            if poi.get("open_time") else f"高德 POI（{poi['category']}）"),
            "source_url": "https://lbs.amap.com/",
            "captured_at": today,
            "verified": False,
        })
    return records


def _cache_path(city: str) -> Path:
    return CACHE_DIR / f"amap_{city}.json"


def cached_discover(city: str, force: bool = False, timeout: float = 20) -> list[dict]:
    """带缓存的发现：同一城市 7 天内不重复请求，省配额。"""
    path = _cache_path(city)
    if not force and path.exists():
        age_hours = (time.time() - path.stat().st_mtime) / 3600
        if age_hours < CACHE_TTL_HOURS:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload.get("pois", [])
    pois = discover(city, timeout=timeout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"city": city, "fetched_at": date.today().isoformat(),
                                "pois": pois}, ensure_ascii=False, indent=2), encoding="utf-8")
    return pois


def build_city(city: str, limit: int = 12, force: bool = False, timeout: float = 20) -> dict:
    """高德 POI → data/<city>.json 的完整流程。"""
    from . import ingest

    pois = cached_discover(city, force=force, timeout=timeout)
    if not pois:
        raise AmapError(f"高德没有返回 {city} 的可用 POI")
    records = to_spot_records(pois, city, limit=limit)
    sources = [{"query": f"高德 POI 搜索 {city}", "url": "https://lbs.amap.com/", "titles": []}]
    target = ingest.write_city_file(city, records, sources)
    return {"city": city, "count": len(records), "names": [r["name"] for r in records],
            "path": str(target), "mode": "amap"}
