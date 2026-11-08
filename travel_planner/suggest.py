"""从搜索摘要里抽"开放时间/门票/闭馆日"，产出候选 + 原文引用。

为什么不用 browser-use：
实测两次（一次允许点击、一次只读）都没抓到——8B 会自己拼网址（`baike/leifengtowershanghai`
→ 404），或者干脆不输出 JSON。而开放时间这类信息，**通常就写在搜索摘要里**，
不需要开浏览器。所以这里用纯规则抽取：快、可测、可解释。

产出仍然只是"候选"：写进 data 的 suggested 字段，verified 保持 false。
"""
import re
from datetime import date
from pathlib import Path

from . import bing

TIME_RANGE = re.compile(
    r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[-–—~～至到]\s*(\d{1,2})\s*[:：]\s*(\d{2})"
)
TICKET = re.compile(r"门票\s*(?:价格|票价|费用)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*元")
TICKET_ALT = re.compile(r"(\d+(?:\.\d+)?)\s*元\s*/\s*人")
CLOSED = re.compile(r"(?:周[一二三四五六日]|星期[一二三四五六日])\s*(?:全天|正常)?\s*(?:闭馆|休息|不开放)")
FREE = re.compile(r"免费开放|免票|不收门票")
WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
SENTENCE_SPLIT = re.compile(r"[。；;\n！!？?]")


def _sentence_with(text: str, start: int, end: int) -> str:
    """取出命中位置所在的那一句，作为可核对的引用。"""
    left = 0
    for match in SENTENCE_SPLIT.finditer(text, 0, start):
        left = match.end()
    right = len(text)
    for match in SENTENCE_SPLIT.finditer(text, end):
        right = match.start()
        break
    return text[left:right].strip()[:80]


def _closed_days(text: str) -> list[int]:
    days = []
    for match in CLOSED.finditer(text):
        token = match.group(0)
        for char in token:
            if char in WEEKDAY_MAP:
                days.append(WEEKDAY_MAP[char])
                break
    return sorted(set(days))


def suggest_from_text(text: str) -> dict:
    """从一段搜索摘要里抽字段。抽不到就是 None，绝不猜。"""
    text = text or ""
    result = {"open_time": None, "close_time": None, "ticket": None,
              "closed_days": [], "quote": "", "found": False}

    time_match = TIME_RANGE.search(text)
    if time_match:
        open_h, open_m, close_h, close_m = (int(item) for item in time_match.groups())
        if 0 <= open_h <= 23 and 0 <= close_h <= 23 and open_h < close_h:
            result["open_time"] = f"{open_h:02d}:{open_m:02d}"
            result["close_time"] = f"{close_h:02d}:{close_m:02d}"
            result["quote"] = _sentence_with(text, time_match.start(), time_match.end())
            result["found"] = True

    ticket_match = TICKET.search(text) or TICKET_ALT.search(text)
    if ticket_match:
        value = float(ticket_match.group(1))
        if 0 <= value <= 2000:
            result["ticket"] = value
            result["found"] = True
            if not result["quote"]:
                result["quote"] = _sentence_with(text, ticket_match.start(), ticket_match.end())
    elif FREE.search(text):
        result["ticket"] = 0.0
        result["found"] = True
        if not result["quote"]:
            match = FREE.search(text)
            result["quote"] = _sentence_with(text, match.start(), match.end())

    days = _closed_days(text)
    if days:
        result["closed_days"] = days
        result["found"] = True

    return result


def suggest_for(city: str, name: str, timeout: float = 20) -> dict:
    """跑一次必应 RSS，抽字段；返回带 source_url 的候选。"""
    query = f"{city} {name} 开放时间 门票 闭馆"
    try:
        result = bing.search_rss(query, [name], timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - 抓不到就当没有候选
        return {"found": False, "error": str(exc), "open_time": None, "close_time": None,
                "ticket": None, "closed_days": [], "quote": "", "source_url": ""}

    suggestion = suggest_from_text(result.text)
    suggestion["source_url"] = result.url
    suggestion["query"] = query
    suggestion["matched"] = name in result.matched
    return suggestion


def write_suggestion(city: str, name: str, suggestion: dict) -> Path | None:
    """写进数据文件的 suggested 字段。绝不改 verified。"""
    from .data import _locate

    path = _locate(city)
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    hit = False
    for spot in payload.get("spots", []):
        if spot.get("name") != name:
            continue
        spot["suggested"] = suggestion
        spot["suggested_at"] = date.today().isoformat()
        hit = True
        break
    if not hit:
        return None
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
