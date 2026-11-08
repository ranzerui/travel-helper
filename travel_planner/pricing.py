from datetime import date
import re


CONDITIONS = re.compile(r"学生|儿童|未满|老年|老人|周岁|残疾|军人|消防|离休|优惠|半价|妇女|陪同|生日|持证|凭|身高|团体|团队|本市居民|[如若]遇|仅限")
EXTRAS = re.compile(r"珍宝馆|钟表馆|联票|套票|特展|展览|讲解|索道|缆车|观光车|停车|游船")
RECURRING = re.compile(r"每年\s*(\d{1,2})月(\d{1,2})日\s*至\s*(?:次年|来年)?\s*(\d{1,2})月(\d{1,2})日")
FULL_RANGE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s*至\s*(20\d{2})年(\d{1,2})月(\d{1,2})日")
PRICE = re.compile(r"(?:大门票|成人(?:门)?票|全价票|普通票|门票价格|门票|票价)\s*(?:为|是|[:：])?\s*(\d+(?:\.\d{1,2})?)\s*元")


def extract_search_prices(text, source, title=""):
    aliases = source.get("aliases", [])
    body = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", text or "")
    body = re.sub(r"[ \t]+", " ", body).replace("：", ":")
    if not any(alias in body + title for alias in aliases):
        return []
    for alias in aliases:
        if re.search(re.escape(alias) + r"(?:广场|城楼|展厅|展馆|博物院|博物馆|纪念馆)", title) and not any(
                longer != alias and longer in title for longer in aliases):
            return []
    rules = []
    segments = []
    for paragraph in re.split(r"[\r\n；;。]+", body):
        excluded = False
        for segment in paragraph.split("，"):
            scope = segment
            for alias in aliases:
                scope = scope.replace(alias, "")
            excluded = excluded or bool(CONDITIONS.search(segment) or EXTRAS.search(scope))
            if not excluded:
                segments.append((segment, paragraph))
    for segment, paragraph in segments:
        segment = segment.strip()
        if not segment or len(segment) > 600 or CONDITIONS.search(segment) or re.search(r"还是|或者|[?？]", segment):
            continue
        scope = segment
        for alias in aliases:
            scope = scope.replace(alias, "")
        if EXTRAS.search(scope) or re.search(r"原价|曾经|原定|此前|不再免费|取消免费|不免费|起售", segment):
            continue
        label = re.search(r"大门票|成人(?:门)?票|成人|全价票|普通票|门票|票价|淡季|旺季", segment)
        free = re.search(r"免费开放|免费参观|免收门票|门票免费|门票(?:价格)?\s*[:]?\s*免费", segment)
        price = re.search(r"(?:[¥￥]\s*(\d+(?:\.\d{1,2})?)(?:\s*[-~—～至]\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?))?|(?<!\d)(\d+(?:\.\d{1,2})?)(?:\s*[-~—～至]\s*(\d+(?:\.\d{1,2})?))?\s*元(?:/[人张])?)", segment)
        if not free and not (label and price and label.start() < price.start()):
            continue
        if re.search(r"\d+(?:\.\d+)?\s*元\s*起|[¥￥]\s*\d+(?:\.\d+)?\s*起|\d+\s*折", segment):
            continue
        if free and re.search(r"[不未]免费|仅|限时|活动|节日", segment):
            continue
        years = re.findall(r"(?<!\d)(20\d{2})年", paragraph)
        low = (price.group(1) or price.group(3)) if price else None
        high = (price.group(2) or price.group(4)) if price else None
        rule = {"amount": 0.0 if free else float(high or low),
                "quote": segment, "ticket_type": "搜索摘要普通成人门票参考，实际票种及价格待确认",
                "evidence_status": "search"}
        if high and not free:
            rule.update({"amount_min": float(low), "amount_max": float(high),
                         "ticket_type": "搜索摘要门票区间，按上限估算；票种及日期待确认"})
            if rule["amount_min"] > rule["amount_max"]:
                continue
        recurring = re.search(r"(\d{1,2})月(\d{1,2})日?\s*(?:至|到|[-—~～])\s*(?:次年|来年)?\s*(\d{1,2})月(\d{1,2})日?", segment)
        if recurring:
            values = [int(value) for value in recurring.groups()]
            try:
                date(2000, *values[:2])
                date(2000, *values[2:])
            except ValueError:
                continue
            rule.update({"month_day_start": f"{values[0]:02d}-{values[1]:02d}",
                         "month_day_end": f"{values[2]:02d}-{values[3]:02d}"})
        elif re.search(r"旺季|淡季|假期|期间|限时", segment):
            continue
        if years:
            rule["reference_years"] = years
        rules.append(rule)
    return rules


def extract_ticket_prices(text, source, title=""):
    rules = []
    aliases = source.get("aliases", [])
    for alias in aliases:
        expanded = re.search(re.escape(alias) + r"(?:广场|城楼|展厅|展馆|博物院|博物馆|纪念馆|地区)", title)
        if expanded and expanded.group() not in aliases:
            return []
    for paragraph in re.split(r"[\r\n；;]+|(?<=[。])", text or ""):
        paragraph = paragraph.strip()
        if not paragraph or len(paragraph) > 400 or CONDITIONS.search(paragraph):
            continue
        scope = paragraph
        for alias in source.get("aliases", []):
            scope = scope.replace(alias, "")
        if EXTRAS.search(scope):
            continue
        match = PRICE.search(paragraph)
        free = re.search(r"免费(?:开放|参观|入馆|入园)|免收门票|门票免费", paragraph)
        if not match and not free:
            continue
        if not match and not (any(alias in paragraph for alias in source.get("aliases", []))
                               or re.search(r"本馆|本院|本园|实行免费开放|免收门票", paragraph)):
            continue
        if re.search(r"起|\d\s*[-~至]\s*\d+\s*元|原价|曾经|原定|此前|不再免费|取消免费|不免费", paragraph):
            continue
        rule = {"amount": float(match.group(1)) if match else 0.0,
                "quote": paragraph, "ticket_type": "成人普通大门票（不含特展、附加项目和优惠票）"}
        recurring = RECURRING.search(paragraph)
        dated = FULL_RANGE.search(paragraph)
        if recurring:
            values = [int(value) for value in recurring.groups()]
            try:
                date(2000, *values[:2])
                date(2000, *values[2:])
            except ValueError:
                continue
            rule.update({"month_day_start": f"{values[0]:02d}-{values[1]:02d}",
                         "month_day_end": f"{values[2]:02d}-{values[3]:02d}"})
        elif dated:
            values = [int(value) for value in dated.groups()]
            try:
                start, end = date(*values[:3]), date(*values[3:])
            except ValueError:
                continue
            if end < start:
                continue
            rule.update({"start": start.isoformat(), "end": end.isoformat()})
        elif re.search(r"旺季|淡季|\d+月|20\d{2}年|即日起|起执行|起实行|起调整|假期|期间", paragraph):
            continue
        rules.append(rule)
    return rules


def matching_price(rules, current):
    applicable = []
    for rule in rules:
        if rule.get("reference_years") and str(current.year) not in rule["reference_years"]:
            continue
        if "month_day_start" in rule:
            start, end = rule["month_day_start"], rule["month_day_end"]
            value = current.strftime("%m-%d")
            if not (start <= value <= end if start <= end else value >= start or value <= end):
                continue
        if "start" in rule and not rule["start"] <= current.isoformat() <= rule["end"]:
            continue
        applicable.append(rule)
    if not applicable or len({rule["amount"] for rule in applicable}) != 1:
        return None
    return applicable[0]


def ticket_details(spot, current):
    if spot.ticket_prices:
        official = [rule for rule in spot.ticket_prices if rule.get("evidence_status") != "search"]
        rule = matching_price(official, current)
        if rule is not None:
            return {"amount": rule["amount"], "status": "official", "quote": rule["quote"],
                    "url": rule.get("url", ""), "captured_at": rule.get("captured_at", ""),
                    "ticket_type": rule["ticket_type"]}
        search = [rule for rule in spot.ticket_prices if rule.get("evidence_status") == "search"]
        rule = matching_price(search, current)
        if rule is not None:
            return {"amount": rule["amount"], "status": "search", "quote": rule["quote"],
                    "url": rule.get("url", ""), "captured_at": rule.get("captured_at", ""),
                    "ticket_type": rule["ticket_type"], "engine": rule.get("engine", ""),
                    "result_url": rule.get("result_url", ""), "screenshot_url": rule.get("screenshot_url", ""),
                    "amount_min": rule.get("amount_min"), "amount_max": rule.get("amount_max")}
    elif spot.verified:
        return {"amount": spot.ticket, "status": "local", "quote": "本地数据标记已核实，未进行本次实时票价查询",
                "url": spot.source_url, "captured_at": spot.captured_at, "ticket_type": "本地票价"}
    return {"amount": spot.ticket if spot.ticket > 0 else None, "status": "reference" if spot.ticket > 0 else "unknown",
            "quote": "票价未经本次官网核实", "url": "", "captured_at": "", "ticket_type": "待核实"}
