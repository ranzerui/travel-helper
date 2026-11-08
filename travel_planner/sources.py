"""数据源层：把"事实"从哪取回来，与规划逻辑解耦。

三种实现，接口一致（fetch 返回 list[Spot]）：
- LocalSource   读本地 JSON：离线、可复现，测试和演示的默认；
- BrowserSource 调 browser-use 抓真实页面：作为"取数工具"，失败不影响主流程；
- CachedSource  先读缓存，缓存命中就不再去抓。

设计要点：抓取是"尽力而为"的取数手段，不是主流程。抓不到就退回本地数据，
保证"永远能出一份行程"，同时在报告里标明数据来源与核实时间。
"""
import dataclasses
import json
import os
import sys
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from . import data
from .models import Spot

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKER = PROJECT_ROOT / "workers" / "browser_fetch.py"
DEFAULT_CACHE = PROJECT_ROOT / "cache"
_SPOT_FIELDS = {field.name for field in dataclasses.fields(Spot)}


def _to_spot(record: dict, city: str, payload: dict) -> Spot:
    """把抓取结果规整成 Spot：补默认值、兼容 url 字段、丢掉不认识的键。"""
    values = dict(record)
    if "url" in values and "source_url" not in values:
        values["source_url"] = values.pop("url")
    values.setdefault("city", city)
    values.setdefault("category", "人文")
    values.setdefault("lat", 0.0)
    values.setdefault("lon", 0.0)
    values.setdefault("open_time", "09:00")
    values.setdefault("close_time", "18:00")
    values.setdefault("duration_min", 90)
    values.setdefault("ticket", 0.0)
    values.setdefault("source_name", payload.get("source_name", "browser-use 抓取"))
    values.setdefault("captured_at", payload.get("captured_at", ""))
    values["closed_days"] = tuple(values.get("closed_days") or ())
    values = {key: value for key, value in values.items() if key in _SPOT_FIELDS}
    return Spot(**values)


class SpotSource(Protocol):
    name: str

    def fetch(self, city: str, query: str = "") -> list[Spot]:
        ...


@dataclass
class LocalSource:
    """读本地 JSON。"""

    name: str = "本地数据"
    data_dir: Path | None = None

    def fetch(self, city: str, query: str = "") -> list[Spot]:
        return data.load_city(city)


@dataclass
class BrowserSource:
    """用 browser-use 抓取：独立的 venv 里跑 workers/browser_fetch.py。

    为什么不直接 import：browser-use 装在另一个虚拟环境（独立 browser-use 环境），
    用子进程调用最省事，也让"引擎可替换"这件事在代码上成立。
    """

    python_exe: str
    worker: Path = DEFAULT_WORKER
    model: str = "qwen3:8b"
    timeout: int = 900
    name: str = "browser-use 抓取"

    def fetch(self, city: str, query: str = "") -> list[Spot]:
        if not Path(self.python_exe).exists():
            raise RuntimeError(f"找不到 browser-use 的解释器：{self.python_exe}")
        if not Path(self.worker).exists():
            raise RuntimeError(f"找不到抓取脚本：{self.worker}")

        out_dir = DEFAULT_CACHE
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"fetched_{city}.json"
        if target.exists():
            target.unlink()

        command = [
            self.python_exe, str(self.worker),
            "--city", city,
            "--model", self.model,
            "--out", str(target),
        ]
        completed = subprocess.run(command, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=self.timeout)
        if completed.returncode != 0:
            raise RuntimeError(f"抓取失败（退出码 {completed.returncode}）：{(completed.stderr or '')[-400:]}")
        if not target.exists():
            raise RuntimeError("抓取脚本没有产出结果文件")

        payload = json.loads(target.read_text(encoding="utf-8"))
        self.notes = payload.get("notes", [])
        spots = [_to_spot(item, city, payload) for item in payload.get("spots", [])]
        if not spots:
            raise RuntimeError("抓取结果里没有景点")
        return spots


@dataclass
class CachedSource:
    """缓存优先：命中就用缓存，未命中才去抓，抓完写缓存。"""

    inner: SpotSource
    cache_dir: Path = DEFAULT_CACHE
    name: str = "缓存"

    def _path(self, city: str) -> Path:
        return Path(self.cache_dir) / f"spots_{city}.json"

    def fetch(self, city: str, query: str = "") -> list[Spot]:
        path = self._path(city)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [Spot(**{**item, "closed_days": tuple(item.get("closed_days", []))})
                    for item in payload["spots"]]
        spots = self.inner.fetch(city, query)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"city": city, "spots": [
            {**spot.__dict__, "closed_days": list(spot.closed_days)} for spot in spots
        ]}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return spots


@dataclass
class BingSource:
    """必应搜索（RSS 输出）+ 本地景点词典匹配。

    分工：必应提供"这个景点在搜索结果里被提到"的证据和链接；
    开放时间/票价仍用本地核实值——摘要里的时间格式太乱，不可靠。
    """

    name: str = "必应搜索（RSS）"
    extra_queries: int = 6      # 概览查询没命中的景点，最多再单独查几次
    timeout: float = 20.0

    def fetch(self, city: str, query: str = "") -> list[Spot]:
        from . import bing

        local = data.load_city(city)
        names = [spot.name for spot in local]
        overview = bing.search_rss(f"{city} 景点 开放时间 门票", names, timeout=self.timeout)
        hits = {name: overview.url for name in overview.matched}
        notes = [f"概览查询命中 {len(hits)} 个"]

        extra_used = 0
        for name in names:
            if name in hits or extra_used >= self.extra_queries:
                continue
            extra_used += 1
            single = bing.search_rss(f"{city} {name} 开放时间", names, timeout=self.timeout)
            if name in single.matched:
                hits[name] = single.url
                notes.append(f"{name}：单点查询命中")
        if extra_used:
            notes.append(f"单点补查 {extra_used} 次，新增 {len(hits) - len(overview.matched)} 个")

        self.notes = notes
        spots = []
        for spot in local:
            matched = spot.name in hits
            spots.append(Spot(**{
                **spot.__dict__,
                "source_name": ("必应搜索提及（时间/票价未核实）" if matched
                                else "本地示例数据（未核实）"),
                "source_url": hits.get(spot.name, ""),
                "captured_at": date.today().isoformat() if matched else spot.captured_at,
            }))
        return spots


def build_source(mode: str, python_exe: str | None = None) -> SpotSource:
    """按模式挑数据源；抓取失败由调用方降级。"""
    if mode == "local":
        return LocalSource()
    if mode == "bing":
        return BingSource()
    if mode in {"browser", "cache"}:
        exe = python_exe or os.environ.get(
            "BROWSER_USE_PY", sys.executable)
        source = BrowserSource(python_exe=exe)
        return CachedSource(source) if mode == "cache" else source
    raise ValueError(f"未知的数据源模式：{mode}")
