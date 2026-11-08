"""从必应搜索结果页取证据：纯 HTTP + 词典匹配，不经过模型。

为什么这么设计：
- 模型从搜索结果里认出"哪个词是景点"很不稳（实测 8B 会把文章标题当景点）；
- 但"某个景点名是否出现在搜索结果里"是纯字符串问题，代码比模型可靠；
- 我们只用本地已核实的景点词典去命中，命中的就是"必应结果里确实提到过"的证据。

不做的事：不绕过验证码、不加高频重试、不解析广告位。
"""
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# 本机/内网不要把代理带进来（Windows 注册表里的系统代理会影响请求）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class _TextCollector(HTMLParser):
    """把 HTML 里可见文字按块收集起来，够用就行，不引第三方解析库。"""

    SKIP = {"script", "style", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth:
            return
        text = _SPACE.sub(" ", data).strip()
        if text:
            self.blocks.append(text)


@dataclass
class BingResult:
    query: str
    url: str
    titles: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    text: str = ""


def search_url(query: str) -> str:
    return "https://cn.bing.com/search?q=" + urllib.parse.quote(query)


def fetch_html(url: str, timeout: float = 20) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"请求必应失败：{exc}") from exc


def visible_text(html: str) -> str:
    parser = _TextCollector()
    parser.feed(html or "")
    return "\n".join(parser.blocks)


def titles_from_text(text: str, limit: int = 10) -> list[str]:
    """把看起来像"标题行"的短句子挑出来，用于报告展示。"""
    result = []
    for line in text.splitlines():
        line = line.strip()
        if 4 <= len(line) <= 40 and not line.endswith(("。", ".", "！", "？")):
            result.append(line)
        if len(result) >= limit:
            break
    return result


def match_spots(text: str, spot_names) -> list[str]:
    """用本地景点词典在页面文本里做命中。命中的才是"有证据"。"""
    hit = []
    for name in spot_names:
        if not name:
            continue
        # 去掉"（...）"这类补充说明后再匹配
        core = re.sub(r"[（(【\[].*?[)）】\]]", "", name).strip()
        if core and core in text:
            hit.append(name)
    return hit


def search(query: str, spot_names, timeout: float = 20) -> BingResult:
    url = search_url(query)
    html = fetch_html(url, timeout=timeout)
    text = visible_text(html)
    return BingResult(
        query=query,
        url=url,
        titles=titles_from_text(text),
        matched=match_spots(text, spot_names),
        text=text,
    )


RSS_SUFFIX = "&format=rss"


def parse_rss(xml: str) -> list[dict]:
    """必应 RSS：每项有 title / link / description，description 常含景点名。"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    items = []
    for item in root.iter("item"):
        items.append({
            "title": (item.findtext("title") or "").strip(),
            "link": (item.findtext("link") or "").strip(),
            "description": _SPACE.sub(" ", _TAG.sub(" ", item.findtext("description") or "")).strip(),
        })
    return items


def search_rss(query: str, spot_names, timeout: float = 20) -> BingResult:
    """走必应的 RSS 输出：纯 XML、无 JS，摘要里能直接命中景点名。"""
    url = search_url(query) + RSS_SUFFIX
    xml = fetch_html(url, timeout=timeout)
    items = parse_rss(xml)
    text = "\n".join(f"{item['title']}\n{item['description']}" for item in items)
    return BingResult(
        query=query,
        url=url,
        titles=[item["title"] for item in items],
        matched=match_spots(text, spot_names),
        text=text,
    )
