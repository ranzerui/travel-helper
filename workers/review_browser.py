import argparse
import asyncio
import base64
import json
from datetime import datetime
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, quote, urldefrag, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from travel_planner.browser_review import allowed_url, extract_findings
from travel_planner import bing, discovery, pricing
from workers.browser_session import open_browser


SEARCH_ENGINES = {
    "百度": {"home": "https://www.baidu.com/", "base": "https://www.baidu.com/s?wd=", "parameter": "wd", "domains": ["www.baidu.com", "baidu.com"],
           "input": "#chat-textarea, #kw", "blocks": "#content_left > div"},
    "必应": {"home": "https://cn.bing.com/", "base": "https://cn.bing.com/search?q=", "parameter": "q", "domains": discovery.SEARCH_DOMAINS,
           "input": "#sb_form_q", "blocks": "#b_results > li.b_algo, #b_results > li.b_ans"},
    "360": {"home": "https://www.so.com/", "base": "https://www.so.com/s?q=", "parameter": "q", "domains": ["www.so.com", "so.com"],
            "input": "textarea[name=q], input[name=q]", "blocks": "li.res-list"},
}


async def wait_search_answer(page, selector, max_polls=30):
    previous = None
    stable = 0
    for poll in range(max_polls):
        state = json.loads(await page.evaluate("(selector) => JSON.stringify({blocks:Array.from(document.querySelectorAll(selector)).filter(item => item.getClientRects().length).slice(0,12).map(item=>({text:item.innerText || '',title:item.querySelector('h3,h2')?.innerText || ''}))})", selector))
        blocks = state["blocks"]
        first = blocks[0]["text"].strip() if blocks else ""
        pending = bool(re.search(r"正在(?:思考|搜索|生成|回答|整理|分析)|思考中|搜索中|生成中|加载中", first))
        stable = stable + 1 if first and first == previous and not pending else 0
        if stable >= 2:
            return {"status": "ready", "polls": poll + 1, "first_card": first}
        previous = first
        if poll + 1 < max_polls:
            await asyncio.sleep(0.6)
    return {"status": "answer_pending" if pending else "results_unstable", "polls": max_polls, "first_card": first}


async def capture_page(page, url, source, folder, prefix, analyze=True, submit=None, cdp_client=None):
    if not allowed_url(url, source["domains"]):
        raise ValueError("页面不在本次来源允许的域名内")
    if not await asyncio.to_thread(discovery.public_url, url, resolve=True):
        raise ValueError("页面不是可访问的公网网址")
    for navigation_attempt in range(2):
        try:
            await asyncio.wait_for(page.goto(url), timeout=30)
            break
        except RuntimeError as exc:
            if not submit or navigation_attempt or "ERR_ABORTED" not in str(exc):
                raise
            await asyncio.sleep(0.6)
    if submit:
        await asyncio.sleep(2)
        submission = None
        for attempt in range(8):
            await asyncio.sleep(0.3)
            try:
                field_index = json.loads(await page.evaluate("(selector) => JSON.stringify(Array.from(document.querySelectorAll(selector)).findIndex(item => item.getClientRects().length && !item.disabled && getComputedStyle(item).visibility !== 'hidden'))", submit["input"]))
                if field_index < 0:
                    continue
                elements = await page.get_elements_by_css_selector(submit["input"])
                await elements[field_index].focus()
                await page.press("Control+a")
                await cdp_client.send.Input.insertText({"text": submit["query"]}, session_id=await page.session_id)
                typed = json.loads(await page.evaluate("() => JSON.stringify({input:document.activeElement?.value || '',encoding:document.characterSet,url:location.href,time_origin:performance.timeOrigin})"))
                if typed["input"] != submit["query"]:
                    raise ValueError("搜索框未收到完整文字，未按回车提交")
                submission = {"method": "Enter", "input_method": "Input.insertText", "query": submit["query"],
                              "input_query": typed["input"], "encoding": typed["encoding"], "starting_url": typed["url"]}
                break
            except RuntimeError:
                continue
        if not submission:
            raise ValueError("未找到可见且可输入的搜索框，未完成回车提交")
        await page.press("Enter")
    for attempt in range(8):
        await asyncio.sleep(0.6)
        text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 60000) : ''")
        if len((text or "").strip()) > 100 and attempt >= 2:
            if submit:
                current = await page.get_url()
                if not parse_qs(urlparse(current).query).get(submit["parameter"]):
                    continue
            break
    title = await page.get_title()
    final_url = await page.get_url()
    if not allowed_url(final_url, source["domains"]):
        raise ValueError(f"页面跳转至本次来源范围之外，未采纳：{final_url}")
    if not text or len(text.strip()) < 50:
        raise ValueError("正文为空或过短，无法核验")
    if any(word in title for word in ("404", "安全验证", "Access Denied", "Just a moment", "页面不存在")):
        raise ValueError(f"页面不可用：{title}")
    if re.search(r"访问过于频繁|请完成安全验证|验证码验证", text[:500]):
        raise ValueError("页面要求验证，无法核验")
    if submit:
        answer_wait = await wait_search_answer(page, submit["blocks"])
        text = await page.evaluate("() => document.body ? document.body.innerText.slice(0, 60000) : ''")
        final_url = await page.get_url()
        title = await page.get_title()
        if not allowed_url(final_url, source["domains"]):
            raise ValueError("等待搜索回答时页面跳转至来源范围之外")
    screenshot = await page.screenshot()
    png_bytes = base64.b64decode(screenshot)
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("截图不是有效 PNG")
    filename = prefix + ".png"
    (folder / filename).write_bytes(png_bytes)
    links = await page.evaluate("() => JSON.stringify(Array.from(document.querySelectorAll('a[href]')).map(anchor => ({text: (anchor.innerText || anchor.title || '').trim(), url: anchor.href})).filter(link => link.text).slice(0, 1200))")
    if isinstance(links, str):
        links = json.loads(links)
    payload = {"title": title, "url": final_url, "captured_at": datetime.now().astimezone().isoformat(),
               "screenshot": filename, "text": text,
               "findings": extract_findings(text, source, title) if analyze else [],
               "ticket_prices": pricing.extract_ticket_prices(text, source, title) if analyze else []}
    if submit:
        payload["submission"] = submission
        payload["answer_wait"] = answer_wait
    if payload["ticket_prices"]:
        quote = payload["ticket_prices"][0]["quote"]
        await page.evaluate("(quote) => {const needle = quote.replace(/\\s+/g, ''); const matches = Array.from(document.querySelectorAll('p,li,td,div')).filter(item => item.innerText && item.innerText.replace(/\\s+/g, '').includes(needle)).sort((first, second) => first.innerText.length - second.innerText.length); if (matches.length) matches[0].scrollIntoView({block: 'center'}); return true;}", quote)
        payload["ticket_screenshot"] = prefix + "-tickets.png"
        (folder / payload["ticket_screenshot"]).write_bytes(base64.b64decode(await page.screenshot()))
    (folder / (prefix + ".json")).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload, links


def validate_search(query, input_query, url_query, blocks, aliases):
    def normalize(value):
        return re.sub(r"\s+", "", value or "").casefold()

    matched = [block for block in blocks if not block.get("ad") and not re.match(
        r"\s*(?:大家还在搜|相关搜索|其他人还搜|为您推荐|正在思考|正在搜索|正在生成)", block.get("text", "")) and any(
        normalize(alias) in normalize(block.get("title", "") + block.get("text", ""))
        for alias in aliases if normalize(alias))]
    query_matches = normalize(input_query) == normalize(query) == normalize(url_query)
    status = "matched" if query_matches and matched else "query_mismatch" if not query_matches else "result_mismatch"
    return {"status": status, "input_query": input_query, "url_query": url_query,
            "result_count": len(blocks), "matched_result_count": len(matched),
            "result_titles": [block.get("title", "") for block in blocks]}, matched


async def capture_search(page, engine, query, target, folder, prefix, attempt, cdp_client=None):
    config = SEARCH_ENGINES[engine]
    attempt.update({"engine": engine, "query": query, "url": config["base"] + quote(query)})
    try:
        captured, _ = await asyncio.wait_for(capture_page(
            page, config["home"], {"domains": config["domains"]}, folder, prefix,
            analyze=False, submit={"query": query, "input": config["input"], "parameter": config["parameter"], "blocks": config["blocks"]}, cdp_client=cdp_client), timeout=35)
        attempt["page"] = captured
        attempt["submitted"] = captured.get("submission", {}).get("method") == "Enter"
        attempt["answer_wait"] = captured.get("answer_wait", {})
        state = json.loads(await page.evaluate("(config) => JSON.stringify({input:Array.from(document.querySelectorAll(config.input)).find(item => item.getClientRects().length && getComputedStyle(item).visibility !== 'hidden')?.value || '',blocks:Array.from(document.querySelectorAll(config.blocks)).slice(0,8).map(item => {const link=item.querySelector('h3 a,h2 a'); return {title:item.querySelector('h3,h2')?.innerText || '',text:item.innerText || '',url:link?.getAttribute('data-mdurl') || link?.href || '',ad:/广告|广告推广/.test(item.innerText.slice(-100))};})})", config))
        url_query = parse_qs(urlparse(captured["url"]).query).get(config["parameter"], [""])[0]
        validation, matched = validate_search(query, state["input"], url_query, state["blocks"], target["aliases"])
        attempt.update(validation)
        if captured.get("answer_wait", {}).get("status") in {"answer_pending", "results_unstable"}:
            attempt["status"] = captured["answer_wait"]["status"]
            matched = []
        if not attempt["submitted"]:
            attempt["status"] = "not_submitted"
            matched = []
        captured["search_validation"] = {**validation, "submitted": attempt["submitted"]}
        captured["search_results"] = state["blocks"]
        if attempt["status"] != "matched":
            attempt["error"] = ("首栏回答仍在生成或结果尚未稳定，未将加载状态当作无票价" if attempt["status"] in {"answer_pending", "results_unstable"} else
                                "搜索词未完整提交，未采信结果" if attempt["status"] != "result_mismatch" else
                                "搜索结果与完整景点名称不匹配，未采信；搜索框文字不作为成功依据")
        elif matched:
            await page.press("Escape")
            await page.press("Tab")
            visible = next((block for block in matched if block.get("title")), matched[0])
            await page.evaluate("(selection) => {const item = Array.from(document.querySelectorAll(selection.selector)).find(item => item.innerText === selection.text); if (item) item.scrollIntoView({block:'center'}); return true;}",
                                {"selector": config["blocks"], "text": visible["text"]})
            captured["screenshot"] = prefix + "-matched.png"
            (folder / captured["screenshot"]).write_bytes(base64.b64decode(await page.screenshot()))
        (folder / (prefix + ".json")).write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
        return matched
    except Exception as exc:
        attempt.update({"status": "unavailable", "error": str(exc) or type(exc).__name__})
        try:
            final_url = await page.get_url()
            title = await page.get_title()
            filename = prefix + "-blocked.png"
            (folder / filename).write_bytes(base64.b64decode(await page.screenshot()))
            attempt["blocked_page"] = {"url": final_url, "title": title, "screenshot": filename,
                                       "captured_at": datetime.now().astimezone().isoformat()}
            if any(word in final_url.lower() for word in ("captcha", "antispider")) or "验证" in title:
                attempt["error"] = "搜索引擎要求安全验证，未读取到结果；请在可用会话完成验证后重试"
        except Exception:
            pass
        return []


async def discover_source(page, target, record, folder, index, cdp_client=None):
    query, search_url = discovery.search_request(target["spot_name"])
    details = {"query": query, "search_url": search_url, "candidates": [], "attempts": []}
    record["discovery"] = details
    results = []
    details["search_attempts"] = []
    for engine in ("必应", "360"):
        attempt = {}
        details["search_attempts"].append(attempt)
        blocks = await capture_search(page, engine, query, target, folder, f"spot-{index}-search-{engine}", attempt, cdp_client)
        if attempt.get("page"):
            details["search_page"] = attempt["page"]
        if blocks:
            results = [{**block, "description": block["text"]} for block in blocks]
            break
        details["search_warning"] = attempt.get("error", "搜索结果未匹配")
    candidates = discovery.rank_candidates(results, target["aliases"])
    if not candidates:
        xml = await asyncio.to_thread(bing.fetch_html, search_url + bing.RSS_SUFFIX, timeout=12)
        candidates = discovery.rank_candidates(bing.parse_rss(xml), target["aliases"])
        details["fallback"] = "必应 RSS 搜索结果；非截图页面内容"
    details["candidates"] = candidates
    for candidate_index, candidate in enumerate(candidates):
        host = urlparse(candidate["url"]).hostname
        domains = [host, host[4:]] if host.startswith("www.") else [host, "www." + host]
        source = {**target, "url": candidate["url"], "domains": domains}
        try:
            prefix = f"spot-{index}-candidate-{candidate_index}"
            entry, links = await capture_page(page, source["url"], source, folder, prefix, analyze=False)
            identity = discovery.source_identity(entry["title"], entry["text"], target["aliases"])
            details["attempts"].append({"url": entry["url"], **identity})
            if not identity["matched"]:
                continue
            details["identity"] = identity
            entry["findings"] = extract_findings(entry["text"], source, entry["title"])
            entry["ticket_prices"] = pricing.extract_ticket_prices(entry["text"], source, entry["title"])
            (folder / (prefix + ".json")).write_text(json.dumps(entry, ensure_ascii=False, indent=2), encoding="utf-8")
            record["source_method"] = "自动搜索，页面归属声明匹配"
            return source, entry, links
        except Exception as exc:
            details["attempts"].append({"url": candidate["url"], "matched": False, "reason": str(exc)})
    raise ValueError("自动搜索未找到可确认归属的官网；不采用攻略站或搜索摘要作为闭馆依据")


def choose_links(links, source, current_url):
    choices = []
    seen = {urldefrag(current_url)[0]}
    for link in links:
        url = urldefrag(link.get("url", ""))[0]
        text = link.get("text", "")
        if url in seen or not allowed_url(url, source["domains"]):
            continue
        if not re.search(r"公告|闭馆|暂停开放|停止开放|预约|参观须知|开放时间|参观服务|票价|票务|门票|收费标准", text):
            continue
        seen.add(url)
        score = 4 * bool(re.search(r"闭馆|暂停开放|停止开放", text)) + 2 * bool(re.search(r"公告|参观须知", text))
        choices.append((score, url))
    return [url for _, url in sorted(choices, key=lambda item: -item[0])[:2]]


async def search_ticket_price(page, target, folder, index, trip_dates, cdp_client=None):
    query = f"{target['spot_name']}门票多少钱"
    search = {"query": query.strip(), "attempts": [], "prices": []}
    engines = [("百度", query), ("必应", query), ("必应", f'"{target["spot_name"]}" 门票'), ("360", query)]
    for engine_index, (engine, search_query) in enumerate(engines):
        if engine_index == 2 and search["attempts"][-1].get("status") != "result_mismatch":
            continue
        attempt = {}
        search["attempts"].append(attempt)
        prefix = f"spot-{index}-ticket-search-{engine_index}"
        blocks = await capture_search(page, engine, search_query, target, folder, prefix, attempt, cdp_client)
        if not blocks:
            continue
        try:
            captured = attempt["page"]
            selected_rules = []
            for block in blocks:
                rules = pricing.extract_search_prices(block["text"], target, block["title"])
                if not any(pricing.matching_price(rules, datetime.fromisoformat(day).date()) for day in trip_dates):
                    continue
                selected_rules = [{**rule, "url": captured["url"], "captured_at": captured["captured_at"],
                                   "engine": engine, "result_url": block["url"]} for rule in rules]
                attempt["selected_result"] = block
                await page.evaluate("(selection) => {const matches = Array.from(document.querySelectorAll(selection.selector)).filter(item => item.innerText === selection.text); if (matches.length) matches[0].scrollIntoView({block:'center'}); return true;}", {"selector": SEARCH_ENGINES[engine]["blocks"], "text": block["text"]})
                filename = prefix + "-price-card.png"
                (folder / filename).write_bytes(base64.b64decode(await page.screenshot()))
                captured["ticket_screenshot"] = filename
                break
            attempt["prices"] = selected_rules
            (folder / (prefix + ".json")).write_text(json.dumps(captured, ensure_ascii=False, indent=2), encoding="utf-8")
            if selected_rules:
                search["prices"] = selected_rules
                search["status"] = "reference"
                break
            attempt["error"] = "搜索页可读取，但首批结果没有识别到适用于出游日期的明确票价"
        except Exception as exc:
            attempt["error"] = str(exc)
    search.setdefault("status", "unavailable")
    return search


async def run(manifest, result_file):
    folder = result_file.parent
    owned_page = None
    checked = []
    async with open_browser(folder) as browser:
        owned_page = await browser.new_page()
        try:
            await owned_page.set_viewport_size(1280, 900)
            for index, source in enumerate(manifest["targets"]):
                target = source
                record = {"spot_id": source["spot_id"], "name": source["spot_name"], "status": "failed", "pages": []}
                try:
                    page = owned_page
                    if source.get("price_only"):
                        record["source_method"] = "仅查询搜索票价，未核验官网闭馆公告"
                        record["error"] = "本景点本轮仅搜索票价，未做官网公告核验"
                    elif source.get("discover"):
                        source, entry, links = await discover_source(page, source, record, folder, index, browser.cdp_client)
                    else:
                        record["source_method"] = "预置官方来源"
                        entry, links = await capture_page(page, source["url"], source, folder, f"spot-{index}-entry")
                    if not target.get("price_only"):
                        record["pages"].append(entry)
                        for detail_index, url in enumerate(choose_links(links, source, entry["url"])):
                            try:
                                detail, _ = await capture_page(page, url, source, folder, f"spot-{index}-detail-{detail_index}")
                                record["pages"].append(detail)
                            except Exception as exc:
                                record.setdefault("warnings", []).append(f"详情页未完成核验：{exc}")
                        record["status"] = "captured"
                except Exception as exc:
                    record["error"] = f"{type(exc).__name__}: {exc}"
                official_rules = [rule for captured in record["pages"] for rule in captured.get("ticket_prices", [])]
                trip_dates = manifest.get("trip_dates") or [datetime.now().date().isoformat()]
                if not all(pricing.matching_price(official_rules, datetime.fromisoformat(day).date()) for day in trip_dates):
                    try:
                        record["ticket_search"] = await asyncio.wait_for(search_ticket_price(owned_page, target, folder, index, trip_dates, browser.cdp_client), timeout=110)
                    except Exception as exc:
                        record["ticket_search"] = {"status": "unavailable", "attempts": [], "prices": [], "error": str(exc) or type(exc).__name__}
                checked.append(record)
                result_file.write_text(json.dumps({"checked": checked}, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            try:
                await browser.close_page(owned_page)
            except Exception:
                pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    result_file = Path(args.out).resolve()
    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    try:
        asyncio.run(asyncio.wait_for(run(manifest, result_file), timeout=max(150, len(manifest["targets"]) * 150)))
    except Exception as exc:
        payload = json.loads(result_file.read_text(encoding="utf-8")) if result_file.exists() else {"checked": []}
        payload["error"] = ("后台查价达到时间上限，已保留完成的证据" if isinstance(exc, TimeoutError)
                            else f"后台查价失败：{type(exc).__name__}: {str(exc)[:400]}")
        result_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(payload["error"], file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
