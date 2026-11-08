import math
import re

from .travel_dates import without_date_expressions


AMOUNT = r"(?:\d{1,3}(?:[,，]\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?|[零一二两三四五六七八九十百千万]+)"


def _amount(token):
    token = token.replace(",", "").replace("，", "")
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", token):
        return float(token)
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    total, section, number = 0, 0, 0
    for char in token:
        if char in digits:
            number = digits[char]
        elif char == "万":
            total += (section + number) * 10000
            section, number = 0, 0
        else:
            section += (number or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            number = 0
    return float(total + section + number)


def extract_budget(text):
    body = without_date_expressions(text).strip()
    if re.search(r"(?:预算\s*(?:为|是|[:：])?\s*|^)\s*[-负]\s*\d+", body):
        raise ValueError("预算需为大于 0 的金额，请明确总预算")
    patterns = (
        rf"(?:总预算|预算|总共|一共|总花费|总费用|只有|仅有|带了|不超过|最多花|控制在)\s*(?:改成|改为|调整为|调整到|只有|仅有|是|为|大约|大概|不超过|最多|上限|以内|[:：])?\s*[¥￥]?\s*({AMOUNT})",
        rf"[¥￥]\s*({AMOUNT})",
        rf"({AMOUNT})\s*(?:元|块|人民币|rmb|RMB)",
        rf"(?:^|[，,；;\s])({AMOUNT})(?=\s*(?:[，,。；;]|$))",
    )
    for pattern in patterns:
        match = re.search(pattern, body)
        if match:
            value = _amount(match.group(1))
            if not math.isfinite(value) or value <= 0:
                raise ValueError("预算需为大于 0 的金额，请明确总预算")
            return value
    return None
