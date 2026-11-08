"""抓取结果的解析与合并：纯函数，不碰浏览器，方便离线测试。

为什么单独一层：
1. browser-use 那边只负责"打开页面、让模型读出来"，输出是文本；
2. 文本 → 结构化景点，是容易出错的地方（格式、缺字段、名称不一致）；
3. 抓到的字段常常不全（很多页面不写票价），所以要能和本地已核实的字段合并。
"""
import json
import re
from urllib.parse import urlparse

_BRACKET = re.compile(r"[（(【\[].*?[)）】\]]")
_NON_WORD = re.compile(r"[^\w\u4e00-\u9fff]+")
_TIME = re.compile(r"^\d{1,2}:\d{2}$")

# 模型不一定用我们约定的键名，常见的都认
_NAME_KEYS = ("name", "title", "名称", "景点", "attraction")
_DESC_KEYS = ("desc", "description", "summary", "简介", "介绍")
_URL_KEYS = ("url", "link", "href", "链接")
_CONTAINER_KEYS = ("spots", "results", "attractions", "items", "data")
_FAKE_HOSTS = {"example.com", "example.org", "example.net", "localhost"}


def build_task(url: str, city: str, limit: int = 8) -> str:
    """给 browser-use 的任务描述。要求它只读页面、只输出 JSON。"""
    return (
        f"打开 {url} ，找出与“{city}景点/地标/公园/博物馆”相关的条目，取前 {limit} 个。\n"
        "对每个条目提取：name（名称）、desc（一句话介绍，取自页面原文）、url（页面内链接）。\n"
        "页面上没有的信息不要猜，宁可不写；链接必须是页面上真实出现的地址，不要自己编。\n"
        '只输出JSON，格式：{"spots":[{"name":"...","desc":"...","url":"..."}]}'
    )


def build_names_task(url: str, city: str, limit: int = 8) -> str:
    """只抽名称的窄任务：字段越少，小模型越不容易跑偏。"""
    return (
        f"打开 {url} ，这个页面是{city}的景点清单。\n"
        f"把页面里出现的景点名称抄下来，取前 {limit} 个，不要改写、不要杜撰。\n"
        '只输出JSON，格式：{"names":["杜甫草堂","武侯祠"]}'
    )


def build_bing_task(query: str, city: str, limit: int = 8) -> str:
    """以必应为入口：搜索结果标题是文章标题，必须点进正文再抽景点名。"""
    return (
        f"步骤1：打开这个必应搜索页 https://cn.bing.com/search?q={query}\n"
        "步骤2：页面上每条结果的标题都是**文章标题**（例如“成都必去景点大全”），"
        "它们不是景点名，不要把它们当景点。\n"
        "步骤3：点开前 1~2 条结果的标题进入正文，在正文里找出被提到的"
        f"{city}景点名称（例如“杜甫草堂”“武侯祠”“宽窄巷子”），最多 {limit} 个。\n"
        "步骤4：只抄正文里真实出现的名字，不要改写、不要编造、不要输出文章标题。\n"
        '最后只输出JSON：{"names":["杜甫草堂","武侯祠"]}'
    )


def _first(record: dict, keys) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_plausible_url(url: str) -> bool:
    """只信任 http(s) 且不是示例域名的链接——模型的链接经常是编的。"""
    try:
        parsed = urlparse(str(url))
    except (TypeError, ValueError):
        return False
    text = str(url)
    if "..." in text or "…" in text:      # 被截断的链接（模型常见输出）不可信
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) \
        and parsed.netloc.lower() not in _FAKE_HOSTS


# 搜索页/资讯页给的是文章标题，不是景点名：带这些词的整条丢掉
_ARTICLE_WORDS = ("攻略", "必去", "必打卡", "大全", "排行", "排名", "TOP", "Top", "top",
                  "清单", "整理", "推荐", "指南", "游记", "怎么玩", "一篇", "收藏", "体验",
                  "逛遍", "玩遍", "打卡", "自由行", "路线")


def looks_like_article(name: str) -> bool:
    text = str(name or "")
    if "..." in text or "…" in text:       # 被截断的标题
        return True
    if len(text) > 22:                     # 景点名一般不会这么长
        return True
    return any(word in text for word in _ARTICLE_WORDS)


def normalize_name(name: str) -> str:
    """名称归一：去括号补充说明、去空格、统一小写，用于和本地数据对齐。"""
    text = _BRACKET.sub("", str(name or ""))
    return _NON_WORD.sub("", text).lower()


def _load_json(text: str):
    candidate = (text or "").strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate)
        candidate = re.sub(r"\s*```$", "", candidate).strip()
    positions = [index for index in (candidate.find("{"), candidate.find("[")) if index >= 0]
    if not positions:
        return None
    start = min(positions)
    try:
        return json.JSONDecoder().raw_decode(candidate[start:])[0]
    except json.JSONDecodeError:
        return None


def _parse_all(answer: str) -> tuple[list[dict], int]:
    """返回 (保留的记录, 被丢弃的文章标题数)。"""
    payload = _load_json(answer)
    records = None
    if isinstance(payload, dict):
        names = payload.get("names")
        if isinstance(names, list):
            # 窄任务模式：{"names":["杜甫草堂", ...]}
            records = [{"name": item} for item in names if isinstance(item, str)]
        else:
            for key in _CONTAINER_KEYS:
                if isinstance(payload.get(key), list):
                    records = payload[key]
                    break
    elif isinstance(payload, list):
        records = payload
    if not isinstance(records, list):
        return [], 0
    kept, dropped = [], 0
    for item in records:
        if not isinstance(item, dict):
            continue
        name = _first(item, _NAME_KEYS)
        if not name:
            continue
        if looks_like_article(name):
            dropped += 1
            continue
        kept.append(item)
    return kept, dropped


def parse_answer(answer: str) -> list[dict]:
    """把模型输出解析成原始记录列表；解析不出来返回空列表。"""
    return _parse_all(answer)[0]


def _slug(name: str, index: int) -> str:
    cleaned = _NON_WORD.sub("_", str(name)).strip("_")
    return f"web_{cleaned[:24] or index}"


def to_spot_records(records: list[dict], city: str, source_name: str,
                    source_url: str, captured_at: str, limit: int = 8) -> list[dict]:
    """原始记录 → Spot 可构造的字典；缺失字段给 None，交给合并层决定用不用默认值。"""
    result = []
    for index, item in enumerate(records[:limit]):
        name = _first(item, _NAME_KEYS)
        if not name:
            continue
        candidate_url = _first(item, _URL_KEYS)
        link_ok = is_plausible_url(candidate_url)
        result.append({
            "id": _slug(name, index),
            "name": name,
            "city": city,
            "category": str(item.get("category") or "人文"),
            "lat": _as_float(item.get("lat")),
            "lon": _as_float(item.get("lon")),
            "open_time": _as_time(item.get("open_time")),
            "close_time": _as_time(item.get("close_time")),
            "duration_min": int(item.get("duration_min") or 90),
            "ticket": None if item.get("ticket") is None else _as_float(item.get("ticket")),
            "closed_days": list(item.get("closed_days") or []),
            "desc": _first(item, _DESC_KEYS)[:200],
            "source_name": source_name,
            "source_url": candidate_url if link_ok else source_url,
            "captured_at": captured_at,
            "link_verified": link_ok,
        })
    return result


def merge_with_local(fetched: list[dict], local_spots) -> tuple[list[dict], list[str]]:
    """抓取字段 + 本地已核实字段 → 完整记录。

    规则：名称能对上的，开放时间/票价/闭馆日用本地值（标为已核实）；
    对不上的，用 09:00-18:00 / 免费 兜底，并在 notes 里写明"未核实"。
    """
    by_name = {normalize_name(spot.name): spot for spot in local_spots}
    merged, notes = [], []
    for record in fetched:
        item = dict(record)
        match = by_name.get(normalize_name(item["name"]))
        if match is not None:
            item["open_time"] = match.open_time
            item["close_time"] = match.close_time
            item["ticket"] = match.ticket
            item["closed_days"] = list(match.closed_days)
            if match.desc and not item.get("desc"):
                item["desc"] = match.desc
            item["source_name"] = f"{item['source_name']} + 本地核实（开放时间/票价）"
            notes.append(f"{item['name']}：名称匹配本地数据，开放时间与票价用已核实值")
        else:
            item["open_time"] = item.get("open_time") or "09:00"
            item["close_time"] = item.get("close_time") or "18:00"
            item["ticket"] = item.get("ticket") if item.get("ticket") is not None else 0.0
            item["closed_days"] = list(item.get("closed_days") or [])
            item["source_name"] = f"{item['source_name']}（开放时间未核实，按 09:00–18:00 处理）"
            notes.append(f"{item['name']}：本地没有对应记录，开放时间未核实")
        merged.append(item)
    return merged, notes


def records_from_answer(answer: str, city: str, source_name: str, source_url: str,
                        captured_at: str, local_spots=None, limit: int = 8):
    """完整流水线：文本 → 原始记录 → 合并本地 → (records, notes)。"""
    raw, dropped = _parse_all(answer)
    if not raw:
        reason = "模型没有返回可解析的景点列表"
        if dropped:
            reason += f"（{dropped} 条是文章标题，不是景点名）"
        return [], [reason]
    records = to_spot_records(raw, city, source_name, source_url, captured_at, limit)
    if not records:
        return [], ["解析出的景点数为 0"]
    unverified_links = sum(1 for item in records if not item.get("link_verified", True))
    if local_spots:
        merged, notes = merge_with_local(records, local_spots)
    else:
        merged, notes = records, [f"未提供本地数据，{len(records)} 个景点的开放时间均为默认值"]
    if unverified_links:
        notes.append(f"{unverified_links} 个条目的链接不可信（模型编造或缺失），已回退到来源页")
    if dropped:
        notes.append(f"{dropped} 条结果被判定为文章标题，已丢弃")
    return merged, notes


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_time(value):
    """只接受 HH:MM；"08:00-18:00" 这种区间写法视为无效，交给合并层。"""
    if isinstance(value, str) and _TIME.match(value.strip()):
        return value.strip()
    return None
