from datetime import date, datetime, timedelta
import re


FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*(?:月|[-/.])\s*(\d{1,2})\s*[日号]?(?!\d)")
MONTH_DATE = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?(?!\d)")
SLASH_DATE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?![\d/])")
WEEK_DATE = re.compile(r"(下下|下|本|这)?(?:周|星期|礼拜)([一二三四五六日天])")
WEEKEND = re.compile(r"(下下|下|本|这|这个)?周末")
RELATIVE_DAYS = re.compile(r"([0-9一二两三四五六七八九十]+)\s*天(?:以)?后")
RELATIVE_NAMES = re.compile(r"大后天|后天|明天|明日|今天|今日")
WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


def local_now():
    return datetime.now().astimezone()


def _date(year, month, day):
    try:
        return date(int(year), int(month), int(day))
    except ValueError as exc:
        raise ValueError(f"日期无效：{year}年{month}月{day}日，请检查月份和天数") from exc


def _next_occurrence(month, day, today):
    for year in range(today.year, today.year + 9):
        try:
            candidate = date(year, int(month), int(day))
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    raise ValueError(f"日期无效：{month}月{day}日")


def _number(token):
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if token.isdigit():
        return int(token)
    if token in digits:
        return digits[token]
    if token == "十":
        return 10
    if token.startswith("十") and token[1:] in digits:
        return 10 + digits[token[1:]]
    raise ValueError("暂时无法识别相对日期，请填写具体月日")


def extract_start_date(text, today):
    body = text or ""
    match = FULL_DATE.search(body)
    if match:
        return _date(*match.groups()), "用户指定完整日期"
    match = MONTH_DATE.search(body) or SLASH_DATE.search(body)
    if match:
        year_hint = re.search(r"(今年|明年|后年)\s*$", body[:match.start()])
        if year_hint:
            offset = {"今年": 0, "明年": 1, "后年": 2}[year_hint.group(1)]
            return _date(today.year + offset, *match.groups()), "用户指定年份和月日"
        return _next_occurrence(*match.groups(), today), "用户指定月日，按下一次该日期"
    match = RELATIVE_NAMES.search(body)
    if match:
        offsets = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3}
        return today + timedelta(days=offsets[match.group()]), "相对当前日期：" + match.group()
    match = RELATIVE_DAYS.search(body)
    if match:
        return today + timedelta(days=_number(match.group(1))), "相对当前日期：" + match.group()
    match = WEEKEND.search(body)
    if match:
        qualifier = match.group(1)
        offset = 14 if qualifier == "下下" else 7 if qualifier == "下" else 0
        monday = today - timedelta(days=today.weekday())
        candidate = monday + timedelta(days=5 + offset)
        if not offset and today.weekday() == 6:
            candidate = today
        return candidate, "相对当前日期：" + match.group()
    match = WEEK_DATE.search(body)
    if match:
        qualifier, weekday = match.groups()
        offset = 14 if qualifier == "下下" else 7 if qualifier == "下" else 0
        candidate = today - timedelta(days=today.weekday()) + timedelta(days=WEEKDAYS[weekday] + offset)
        if qualifier is None and candidate < today:
            candidate += timedelta(days=7)
        return candidate, "相对当前日期：" + match.group()
    if re.search(r"明年|下个月|下月|月底|年底|春节|中秋|端午", body):
        raise ValueError("请补充具体出游日期，例如 2027年2月8日；暂不自动猜测月份或节假日日期")
    return None, ""


def resolve_start_date(text="", explicit="", inherited="", now=None):
    current = now if now is not None else local_now()
    today = current.date()
    if explicit:
        try:
            selected = date.fromisoformat(explicit)
        except ValueError as exc:
            raise ValueError("出游日期格式应为 YYYY-MM-DD") from exc
        source = "用户通过日期参数指定"
    else:
        selected, source = extract_start_date(text, today)
        if selected is None:
            selected = date.fromisoformat(inherited) if inherited else today
            source = "沿用上一版出游日期" if inherited else "未指定日期，采用电脑当前当地日期"
    if selected < today:
        raise ValueError(f"出游日期 {selected.isoformat()} 已早于当前日期 {today.isoformat()}，请指定今天或之后的日期")
    return selected.isoformat(), source


def without_date_expressions(text):
    body = text or ""
    for pattern in (FULL_DATE, MONTH_DATE, SLASH_DATE, RELATIVE_DAYS, RELATIVE_NAMES, WEEKEND, WEEK_DATE):
        body = pattern.sub(" ", body)
    return body
