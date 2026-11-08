"""从抓取结果生成城市数据文件（data/<city>.json）。

为什么要有这一步：
`data/*.json` 应该是**产物**，不是手写的。手写只能做 fixture。
但冷启动有个鸡生蛋问题：必应匹配法需要先有景点名单。

所以分成两种数据：
- fixture（手写）：完整但只是示例，`verified=false`；
- scraped（本模块生成）：从必应搜索结果里发现候选名，`verified=false`，
  开放时间/票价用默认值，必须人工核实后才能当事实用。

生成出来的文件会写清 `kind` 和 `verified`，报告里也会标注，不会把候选当真相。
"""
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

from . import bing
from .data import DATA_DIR
from .fetching import looks_like_article

SPLIT = re.compile(r"[、，,；;：:。！？!?\s/]+")
TRIM = "“”\"'（）()【】[]《》<>·—-"
# 出现在候选里就说明不是景点名
STOP_WORDS = {
    "景点", "旅游", "旅行", "攻略", "推荐", "必去", "门票", "开放", "时间", "免费",
    "公交", "地铁", "交通", "住宿", "酒店", "美食", "小吃", "天气", "地图", "路线",
    "一日", "两日", "三日", "周边", "排行", "榜", "大全", "指南", "游记", "自由行",
}
# 含这些字的几乎都是句子碎片，不是景点名
BANNED_CHARS = set("的了是我你他她它这那也都就还更最太很呢吧啊呀吗把被给对从到和与或")
DEFAULT_OPEN = "09:00"
DEFAULT_CLOSE = "18:00"


def _clean(token: str) -> str:
    return token.strip().strip(TRIM)


def candidate_names(texts, min_hits: int = 2, limit: int = 12) -> list[str]:
    """从搜索结果文本里抽候选景点名。

    规则：2~6 个字符、不像文章标题、不是常见词，且至少在两条结果里出现（共现过滤）。
    """
    counter = Counter()
    for text in texts:
        seen_in_this_text = set()
        for raw in SPLIT.split(text or ""):
            name = _clean(raw)
            if not 2 <= len(name) <= 8:
                continue
            if looks_like_article(name):
                continue
            if any(word in name for word in STOP_WORDS):
                continue
            if any(char in BANNED_CHARS for char in name):
                continue
            if name in seen_in_this_text:
                continue
            seen_in_this_text.add(name)
            counter[name] += 1
    ranked = [name for name, hits in counter.most_common() if hits >= min_hits]
    return ranked[:limit]


def build_spots(city: str, names, source_name: str, source_url: str) -> list[dict]:
    """候选名 → 景点记录。开放时间/票价为默认值，明确标 verified=false。"""
    today = date.today().isoformat()
    spots = []
    for index, name in enumerate(names):
        slug = re.sub(r"[^\w\u4e00-\u9fff]+", "_", name).strip("_")[:24] or f"spot{index}"
        spots.append({
            "id": f"scraped_{slug}_{index}",
            "name": name,
            "city": city,
            "category": "人文",
            "lat": 0.0,
            "lon": 0.0,
            "open_time": DEFAULT_OPEN,
            "close_time": DEFAULT_CLOSE,
            "duration_min": 90,
            "ticket": 0.0,
            "closed_days": [],
            "desc": "",
            "source_name": source_name,
            "source_url": source_url,
            "captured_at": today,
            "verified": False,
        })
    return spots


def discover(city: str, queries=None, per_query: int = 3, timeout: float = 20) -> tuple[list[str], list[dict]]:
    """跑几条必应查询，返回 (候选名, 用到的查询结果)。"""
    queries = queries or [
        f"{city} 景点 推荐",
        f"{city} 必去景点",
        f"{city} 一日游 景点",
    ]
    texts, used = [], []
    for query in queries:
        result = bing.search_rss(query, [], timeout=timeout)
        used.append({"query": query, "url": result.url, "titles": result.titles[:per_query]})
        texts.append(result.text)
    return candidate_names(texts), used


def write_city_file(city: str, spots: list[dict], sources: list[dict],
                    path: Path | None = None) -> Path:
    target = path or (DATA_DIR / f"{city}.json")
    payload = {
        "city": city,
        "kind": "scraped",
        "verified": False,
        "note": ("由 ingest 从必应搜索结果生成：只有景点名，开放时间/票价为默认值，"
                 "必须人工或官方源核实后使用（verified=false）。"),
        "generated_at": date.today().isoformat(),
        "queries": sources,
        "spots": spots,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def verify_seeds(city: str, seeds, timeout: float = 20) -> tuple[list[str], list[dict]]:
    """人工种子 → 用必应核实每个种子是否真的被搜索结果提到。

    这是推荐的冷启动方式：名字由人给（可信），证据由抓取给（可核查）。
    """
    queries, confirmed = [], []
    for name in seeds:
        query = f"{city} {name} 开放时间 门票"
        result = bing.search_rss(query, [name], timeout=timeout)
        queries.append({"query": query, "url": result.url, "titles": result.titles[:3]})
        if name in result.matched:
            confirmed.append(name)
    return confirmed, queries


def build_for_city(city: str, queries=None, limit: int = 10, path: Path | None = None,
                   seeds=None) -> dict:
    """生成城市数据文件。

    - 给了 seeds：走"人工种子 + 必应核实"，只保留能被搜索结果证实的；
    - 没给 seeds：走候选发现（冷启动，质量不稳定，仅作探索）。
    """
    if seeds:
        seeds = [str(name).strip() for name in seeds if str(name).strip()]
        confirmed, used = verify_seeds(city, seeds)
        if not confirmed:
            raise RuntimeError(f"{city} 的种子景点都没能被必应结果证实，请换个名字或直接用 --fetch local")
        spots = build_spots(city, confirmed, f"人工种子 + 必应核实（{city}）",
                            used[0]["url"] if used else "")
        for spot in spots:
            spot["seed"] = True
        target = write_city_file(city, spots, used, path=path)
        return {"city": city, "count": len(spots), "names": confirmed,
                "path": str(target), "queries": used, "mode": "seed"}

    names, used = discover(city, queries=queries)
    names = names[:limit]
    if not names:
        raise RuntimeError(f"没有从必应结果里抽出 {city} 的候选景点，可能查询被限流或页面结构变化")
    spots = build_spots(city, names, f"必应搜索（{city}）", used[0]["url"] if used else "")
    target = write_city_file(city, spots, used, path=path)
    return {"city": city, "count": len(spots), "names": names,
            "path": str(target), "queries": used, "mode": "discover"}
