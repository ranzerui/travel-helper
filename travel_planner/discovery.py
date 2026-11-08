import base64
import ipaddress
import re
import socket
from urllib.parse import parse_qs, quote, urldefrag, urlparse


SEARCH_DOMAINS = ["cn.bing.com", "www.bing.com", "bing.com"]
THIRD_PARTIES = ("baidu.com", "ctrip.com", "trip.com", "mafengwo.cn", "dianping.com",
                 "meituan.com", "xiaohongshu.com", "douyin.com", "zhihu.com",
                 "wikipedia.org", "sohu.com", "163.com", "qq.com")


def search_request(name):
    query = f'"{name}" 官网'
    return query, "https://cn.bing.com/search?q=" + quote(query)


def public_url(url, resolve=False):
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or "." not in host
                or host.endswith((".local", ".localhost", ".internal"))):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            if not resolve:
                return True
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(address[4][0]).is_global for address in addresses)
    except (ValueError, OSError):
        return False


def result_url(url):
    try:
        parsed = urlparse(url)
        if parsed.hostname in SEARCH_DOMAINS and parsed.path.startswith("/ck/"):
            token = parse_qs(parsed.query).get("u", [""])[0]
            if token.startswith("a1"):
                encoded = token[2:]
                url = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        return urldefrag(url)[0] if public_url(url) else ""
    except (ValueError, UnicodeError):
        return ""


def rank_candidates(results, aliases, limit=2):
    choices = []
    seen = set()
    for item in results:
        url = result_url(item.get("url") or item.get("link") or "")
        if not url or url in seen:
            continue
        host = urlparse(url).hostname
        if host in SEARCH_DOMAINS or any(host == domain or host.endswith("." + domain) for domain in THIRD_PARTIES):
            continue
        title = item.get("title") or ""
        description = item.get("description") or ""
        if not any(alias in title + description for alias in aliases):
            continue
        score = 5 * any(alias in title for alias in aliases) + 3 * bool(re.search(r"官网|官方网站", title))
        score += 2 * (host.endswith(".gov.cn")) + int(urlparse(url).path in {"", "/"})
        choices.append((score, {"url": url, "title": title}))
        seen.add(url)
    return [item for _, item in sorted(choices, key=lambda item: -item[0])[:limit]]


def source_identity(title, text, aliases):
    if not any(alias in title for alias in aliases):
        return {"matched": False, "reason": "页面标题与景点名称不匹配"}
    for line in text.splitlines():
        if len(line) > 400 or not any(alias in line for alias in aliases):
            continue
        if re.search(r"版权所有|版权归|主办(?:单位)?|网站(?:管理|维护)|Copyright|©", line, re.I):
            return {"matched": True, "quote": line.strip(),
                    "reason": "页面标题及主办/版权声明与景点名称匹配；网站自述，未经人工认证"}
    return {"matched": False, "reason": "缺少与景点名称一致的主办或版权声明，暂不采信为官网"}
