"""需求理解：把用户的一句话变成结构化约束。

先用模型解析（能理解"带老人、别太赶、想多看古迹"这类说法），
模型不可用或输出不合法时退回规则解析——保证永远有一份可用的约束。
"""
import json
import re
from dataclasses import dataclass, field

from . import budget as budget_parser, llm, travel_dates

# 只是常见城市的提示表，不是白名单：任何中文城市名都应该能用（数据按需生成）
KNOWN_CITIES = {"成都": "chengdu", "北京": "beijing", "上海": "shanghai",
                "西安": "xian", "杭州": "hangzhou", "广州": "guangzhou",
                "南京": "nanjing", "重庆": "chongqing", "苏州": "suzhou",
                "武汉": "wuhan", "厦门": "xiamen", "青岛": "qingdao"}
CITY_STOPWORDS = ("旅游", "旅行", "景点", "攻略", "周末", "明天", "后天", "今天", "预算",
                  "人文", "自然", "美食", "亲子", "轻松", "紧凑", "周边", "国内", "国外",
                  "一趟", "这边", "那边", "出差", "玩")
CITY_PATTERNS = (
    r"(?:去|到|在|游|往)(?:趟|一趟|个|一下)?([\u4e00-\u9fa5]{2,6}?)(?:玩|旅游|旅行|出差|待|呆|逛|转|的|吃|看|\d|[一二两三四五六七八九十]|$)",
    r"([\u4e00-\u9fa5]{2,4})市",
    r"^([\u4e00-\u9fa5]{2,4}?)(?:\d|[一二两三四五六七八九十]|[，,\s])",
    # "3天昆明" / "三天昆明"：城市在时间后面
    r"(?:\d|[一二两三四五六七八九十])\s*[天日](?:游|玩|去|到|在)?([\u4e00-\u9fa5]{2,4})",
    # "昆明玩3天" / "昆明逛三天"
    r"([\u4e00-\u9fa5]{2,4}?)(?:玩|游|逛|耍)(?:\d|[一二两三四五六七八九十])",
)
PREFERENCE_WORDS = {
    "人文": ["人文", "古迹", "博物馆", "历史", "文化", "寺庙", "祠"],
    "自然": ["自然", "公园", "熊猫", "风景", "爬山", "绿道"],
    "美食": ["美食", "小吃", "吃", "夜市", "火锅"],
    "亲子": ["亲子", "孩子", "小朋友", "带娃"],
}
PACE_WORDS = {"轻松": ["轻松", "悠闲", "慢", "别太赶", "宽松", "不赶"],
              "紧凑": ["紧凑", "高效", "多去", "多逛", "赶一点"]}


def _clean_candidate(raw: str) -> str | None:
    """候选词清洗：去掉尾部动词、排除时间词和形容词短语。"""
    name = (raw or "").strip().strip("，,。！？、 “”\"'")
    for verb in ("玩", "游", "逛", "耍", "去", "到", "待", "呆"):
        if name.endswith(verb) and len(name) > 2:
            name = name[:-1]
            break
    if not 2 <= len(name) <= 6:
        return None
    if re.fullmatch(r"[0-9一二两三四五六七八九十]+", name):
        return None
    if name.endswith(("天", "日", "的", "元", "好", "美", "多", "少", "远", "近", "大", "小", "贵", "冷", "热", "安静", "热闹")):
        return None
    if any(word in name for word in CITY_STOPWORDS):
        return None
    return name


def city_candidates(text: str) -> list[str]:
    """按模式抽出所有可能的城市名（已清洗，未校验）。"""
    body = (text or "").strip()
    seen, result = set(), []
    for pattern in CITY_PATTERNS:
        for match in re.finditer(pattern, body):
            name = _clean_candidate(match.group(1))
            if not name or name in seen:
                continue
            seen.add(name)
            result.append(name)
    return result


def extract_city(text: str, online: bool = True) -> str | None:
    """从话里抽城市：候选必须通过城市名录校验，"景色好"这种不算城市。

    online=True 时，地名录查不到的（县级市等）会联网确认一次并缓存。
    """
    from . import citydb

    body = travel_dates.without_date_expressions(text).strip()
    for name in city_candidates(body):
        if citydb.is_city_anywhere(name, online=online):
            return name

    # 兜底：语序实在太乱（"预算3000，五天，哈尔滨"）时，直接在城市名录里
    # 找"文本中出现过的名字"，最长优先。名录本身就是过滤器，不会把普通词认成城市。
    if not citydb.available():
        return None
    hits = [name for name in citydb.all_names() if len(name) >= 2 and name in body]
    for name in sorted(hits, key=len, reverse=True):
        if citydb.is_city_anywhere(name, online=online):
            return name
    return None


@dataclass
class Requirements:
    city: str = "成都"
    days: int = 3
    budget: float = 1500.0
    prefer: str = ""
    pace: str = "适中"
    start_time: str = "09:00"
    end_time: str = "20:00"
    must_see: list[str] = field(default_factory=list)
    raw: str = ""
    source: str = "规则"      # 规则 / 模型
    start_date: str = ""
    date_source: str = ""
    budget_source: str = "未指定，暂按默认总预算"

    def describe(self) -> str:
        parts = [f"{self.city} {self.days} 天", f"预算 ¥{self.budget:g}"]
        if self.prefer:
            parts.append(f"偏好{self.prefer}")
        parts.append(f"节奏{self.pace}")
        if self.start_date:
            parts.append(f"出游 {self.start_date}")
        return " · ".join(parts)


def parse_requirements(text: str, use_llm: bool = True, now=None, explicit_start_date="") -> Requirements:
    text = (text or "").strip()
    start_date, date_source = travel_dates.resolve_start_date(text, explicit=explicit_start_date, now=now)
    specified_budget = budget_parser.extract_budget(text)
    parsed = None
    if use_llm:
        try:
            parsed = _from_llm(travel_dates.without_date_expressions(text))
        except (llm.LLMError, ValueError, KeyError, TypeError):
            pass
    parsed = parsed or _from_rules(text)
    parsed.raw = text
    parsed.start_date, parsed.date_source = start_date, date_source
    if specified_budget is not None:
        parsed.budget = specified_budget
        parsed.budget_source = "用户明确指定的总预算"
    elif parsed.source == "模型":
        parsed.budget_source = "模型推断，用户未明确总预算"
    duration = re.search(r"([0-9一二两三四五六七八九十]+)\s*[天日]", travel_dates.without_date_expressions(text))
    if duration:
        parsed.days = max(1, min(_to_int(duration.group(1)) or parsed.days, 14))
    return parsed


def _from_llm(text: str) -> Requirements | None:
    prompt = (
        "你是旅行需求解析器。把用户的话变成JSON，不要解释。\n"
        "字段：city（城市，中文）、days（整数天）、budget（整数元）、"
        "prefer（人文/自然/美食/亲子，无法判断就空字符串）、"
        "pace（轻松/适中/紧凑）、must_see（必须去的景点名数组，可空）。\n"
        '只输出JSON，例如：{"city":"城市名","days":3,"budget":1500,"prefer":"人文","pace":"适中","must_see":[]}\n'
        "注意：city 必须是用户原话里出现的城市，用户没说城市就填空字符串，不要猜、不要套用示例。\n"
        f"用户：{text}\n"
    )
    raw = llm.chat(prompt)
    data = _loads(raw)
    if not isinstance(data, dict):
        return None
    days = int(data.get("days") or 0)
    budget = float(data.get("budget") or 0)
    if not 1 <= days <= 14 or budget <= 0:
        return None
    # 城市必须能在用户原话里找到，否则不采信（模型经常照抄示例里的城市）
    candidate = str(data.get("city") or "").strip()
    mentioned = extract_city(text) or ""
    if candidate and candidate in text:
        city = candidate
    elif mentioned:
        city = mentioned
    else:
        city = ""
    prefer = str(data.get("prefer") or "").strip()
    pace = str(data.get("pace") or "适中").strip()
    must_see = [str(item).strip() for item in (data.get("must_see") or []) if str(item).strip()]
    return Requirements(
        city=city or "成都",
        days=days,
        budget=budget,
        prefer=prefer if prefer in PREFERENCE_WORDS else "",
        pace=pace if pace in {"轻松", "适中", "紧凑"} else "适中",
        must_see=must_see,
        raw=text,
        source="模型",
    )


_CN_DIGITS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}


def _to_int(token: str) -> int | None:
    """支持"2 天"和"两天"两种写法。"""
    token = str(token).strip()
    if token.isdigit():
        return int(token)
    if token in _CN_DIGITS:
        return _CN_DIGITS[token]
    if token == "十":
        return 10
    if token.startswith("十") and token[1:] in _CN_DIGITS:      # 十二
        return 10 + _CN_DIGITS[token[1:]]
    if token.endswith("十") and token[:-1] in _CN_DIGITS:      # 二十
        return _CN_DIGITS[token[:-1]] * 10
    return None


def _from_rules(text: str) -> Requirements:
    days_match = re.search(r"([0-9一二两三四五六七八九十]+)\s*[天日]", travel_dates.without_date_expressions(text))
    days = _to_int(days_match.group(1)) if days_match else None
    days = days or 3

    specified_budget = budget_parser.extract_budget(text)
    budget = specified_budget if specified_budget is not None else 1500.0

    city = extract_city(text) or "成都"

    prefer = ""
    for name, words in PREFERENCE_WORDS.items():
        if any(word in text for word in words):
            prefer = name
            break

    pace = "适中"
    for name, words in PACE_WORDS.items():
        if any(word in text for word in words):
            pace = name
            break

    return Requirements(city=city, days=max(1, min(days, 14)), budget=budget,
                        prefer=prefer, pace=pace, raw=text, source="规则")


def _loads(raw: str):
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except json.JSONDecodeError:
        return None


# ============ 多轮追问：在上一版需求上做增量调整 ============

REFINE_PREFIXES = ("再", "改", "换", "加", "减", "多", "少", "别", "缩短", "延长", "预算", "调整")


def is_refine(text: str) -> bool:
    """短句或以"再/改/换/加/减/预算……"开头的，当作对上一版行程的追问。"""
    body = (text or "").strip()
    if not body:
        return False
    if len(body) <= 12:
        return True
    return any(body.startswith(prefix) for prefix in REFINE_PREFIXES)


def apply_delta(req: Requirements, text: str, now=None, explicit_start_date="") -> tuple[Requirements, list[str]]:
    """把"再紧凑一点""预算改成800""加一天"这类话叠加到上一版需求上。"""
    body = (text or "").strip()
    changes: list[str] = []
    values = dict(req.__dict__)
    start_date, date_source = travel_dates.resolve_start_date(body, explicit=explicit_start_date,
                                                            inherited=req.start_date, now=now)
    if start_date != req.start_date:
        changes.append(f"出游日期 {req.start_date or '未指定'} → {start_date}")
    values["start_date"] = start_date
    values["date_source"] = date_source
    duration_body = travel_dates.without_date_expressions(body)

    # 换城市
    mentioned = extract_city(body)
    if mentioned and mentioned != req.city:
        values["city"] = mentioned
        changes.append(f"城市 {req.city} → {mentioned}")

    # 天数：加/减/直接给
    match = re.search(r"(加|多|增加|再多)\s*([0-9一二两三四五六七八九十]+)?\s*[天日]", duration_body)
    if match:
        delta = _to_int(match.group(2)) if match.group(2) else 1
        new_days = min(14, req.days + (delta or 1))
        if new_days != req.days:
            values["days"] = new_days
            changes.append(f"天数 {req.days} → {new_days}")
    else:
        match = re.search(r"(减|少|去掉|缩短)\s*([0-9一二两三四五六七八九十]+)?\s*[天日]", duration_body)
        if match:
            delta = _to_int(match.group(2)) if match.group(2) else 1
            new_days = max(1, req.days - (delta or 1))
            if new_days != req.days:
                values["days"] = new_days
                changes.append(f"天数 {req.days} → {new_days}")
        else:
            match = re.search(r"([0-9一二两三四五六七八九十]+)\s*[天日]", duration_body)
            new_days = _to_int(match.group(1)) if match else None
            if new_days and new_days != req.days:
                values["days"] = max(1, min(new_days, 14))
                changes.append(f"天数 {req.days} → {values['days']}")

    # 预算
    new_budget = budget_parser.extract_budget(body)
    if new_budget is not None:
        values["budget_source"] = "用户明确指定的总预算"
        if new_budget != req.budget:
            values["budget"] = new_budget
            changes.append(f"预算 ¥{req.budget:.0f} → ¥{new_budget:.0f}")

    # 偏好
    for name, words in PREFERENCE_WORDS.items():
        if any(word in body for word in words) and name != req.prefer:
            values["prefer"] = name
            changes.append(f"偏好 → {name}")
            break

    # 节奏
    for name, words in PACE_WORDS.items():
        if any(word in body for word in words) and name != req.pace:
            values["pace"] = name
            changes.append(f"节奏 → {name}")
            break

    if "免费" in body:
        changes.append("暂不支持只看免费景点，可以把预算调低来逼近")

    values["raw"] = body
    values["source"] = f"{req.source}+追问"
    return Requirements(**values), changes
