"""交通时长提供者：估算 / 高德路径规划，可回退、带缓存。

设计要点：
1. 接口统一 `minutes(a, b) -> (分钟, 来源标记)`，模拟器和规划器只认这个签名；
2. 高德失败（没 key、限流、网络）**逐段回退**到直线估算，并在来源里标出来，
   不能让整个规划失败——这是"取数是尽力而为"的延续；
3. 结果按坐标五舍五入后缓存，同一次规划里反复出现的路段不重复请求。
"""
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import amap, simulator
from .models import Spot

CACHE_PATH = Path(__file__).resolve().parent.parent / "cache" / "routes.json"
CACHE_TTL_HOURS = 24 * 30
WALK_MAX_KM = 3.0          # 3 公里内按步行算，更远按驾车（公交要城市编码，暂不做）


class RouteProvider(Protocol):
    name: str

    def minutes(self, first: Spot, second: Spot) -> tuple[int, str]:
        ...


@dataclass
class EstimateProvider:
    """原来的直线估算：离线可用，作为基准和兜底。"""

    name: str = "直线估算"

    def minutes(self, first: Spot, second: Spot) -> tuple[int, str]:
        return simulator.travel_minutes(first, second), "估算"


@dataclass
class AmapProvider:
    """高德路径规划：步行 / 驾车，带缓存和逐段回退。"""

    mode: str = "auto"          # auto / walking / driving
    cache_path: Path = CACHE_PATH
    timeout: float = 15.0
    name: str = "高德路径规划"
    _cache: dict = field(default_factory=dict, repr=False)
    _loaded: bool = field(default=False, repr=False)
    fallback: EstimateProvider = field(default_factory=EstimateProvider)
    stats: dict = field(default_factory=lambda: {"api": 0, "cache": 0, "fallback": 0})

    # ---------- 缓存 ----------
    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if time.time() - payload.get("saved_at", 0) < CACHE_TTL_HOURS * 3600:
            self._cache = payload.get("routes", {})

    def _save(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"saved_at": time.time(), "routes": self._cache}
        try:
            self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # 缓存写不进去不影响规划

    @staticmethod
    def _key(first: Spot, second: Spot, mode: str) -> str:
        return (f"{first.lat:.5f},{first.lon:.5f}|{second.lat:.5f},{second.lon:.5f}"
                f"|{mode}")

    # ---------- 主入口 ----------
    def minutes(self, first: Spot, second: Spot) -> tuple[int, str]:
        self._load()
        distance = simulator.distance_km(first, second)
        mode = self.mode if self.mode != "auto" else ("walking" if distance <= WALK_MAX_KM else "driving")
        key = self._key(first, second, mode)

        if key in self._cache:
            self.stats["cache"] += 1
            return int(self._cache[key]), f"高德·{'步行' if mode == 'walking' else '驾车'}(缓存)"

        try:
            seconds = self._request(first, second, mode)
            minutes = max(1, int(math.floor(seconds / 60 + 0.5)))
            self._cache[key] = minutes
            self._save()
            self.stats["api"] += 1
            return minutes, f"高德·{'步行' if mode == 'walking' else '驾车'}"
        except (amap.AmapError, KeyError, IndexError, TypeError, ValueError):
            self.stats["fallback"] += 1
            minutes, _ = self.fallback.minutes(first, second)
            return minutes, "估算(回退)"

    def _request(self, first: Spot, second: Spot, mode: str) -> float:
        origin = f"{first.lon:.6f},{first.lat:.6f}"
        destination = f"{second.lon:.6f},{second.lat:.6f}"
        payload = amap._get(f"/v5/direction/{mode}", {
            "origin": origin,
            "destination": destination,
            "show_fields": "cost",
        }, timeout=self.timeout)
        route = payload.get("route") or {}
        paths = route.get("paths") or []
        if not paths:
            raise ValueError("高德没有返回可用路线")
        cost = paths[0].get("cost") or {}
        duration = cost.get("duration")
        if duration is None:
            raise ValueError("高德返回里没有 duration")
        return float(duration)


def build_provider(mode: str = "auto", allow_network: bool = True) -> RouteProvider:
    """按命令行/网页选择构建：没 key 或不允许联网就退到估算。"""
    if mode == "estimate" or not allow_network:
        return EstimateProvider()
    from . import settings
    if not settings.amap_key():
        return EstimateProvider()
    return AmapProvider(mode="auto" if mode == "auto" else mode)
