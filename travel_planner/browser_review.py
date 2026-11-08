import json
import os
import sys
import re
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import uuid4

from . import planner, pricing, simulator

ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILE = Path(__file__).with_name("official_sources.json")
DATE_PATTERN = re.compile(r"(20\d{2})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})\s*日?")
CLOSURE_PATTERN = re.compile(r"暂停开放|停止开放|临时闭馆|临时关闭|暂停对外开放|闭馆|停止对外开放")
PARTIAL_PATTERN = re.compile(r"展厅|展馆|展区|殿|宫|部分区域|局部|通道|入口|出口")


def official_source(city, name):
    payload = json.loads(SOURCE_FILE.read_text(encoding="utf-8"))
    return next((source for source in payload["sources"]
                 if source["city"] == city and name in source["aliases"]), None)


def allowed_url(url, domains):
    try:
        parsed = urlparse(url)
        return (parsed.scheme in {"http", "https"} and not parsed.username
                and not parsed.password and parsed.hostname in domains
                and parsed.port in {None, 80, 443})
    except ValueError:
        return False


@contextmanager
def exclusive_browser():
    lock_path = ROOT / "cache" / "browser-review.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock_file.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _date_matches(text):
    result = []
    for match in DATE_PATTERN.finditer(text):
        try:
            result.append((match, date(*(int(value) for value in match.groups()))))
        except ValueError:
            continue
    return result


def extract_findings(text, source, title=""):
    findings = []
    seen = set()
    paragraphs = re.split(r"[\r\n]+|(?<=[。！？；])", text)
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if len(paragraph) < 8 or paragraph in seen or len(paragraph) > 1200:
            continue
        seen.add(paragraph)
        closure = CLOSURE_PATTERN.search(paragraph)
        if closure and re.search(r"如遇|如果|如逢|通行|半价|优惠|儿童节|妇女节|建军节", paragraph):
            closure = None
        reservation = re.search(r"预约|实名|提前.{0,6}天|放票", paragraph)
        hours = re.search(r"开放时间|停止入馆|停止检票", paragraph) and re.search(r"\d", paragraph)
        reopening = re.search(r"恢复开放|恢复正常开放|恢复对外开放|重新开放|取消闭馆", paragraph)
        if reopening and any(alias in paragraph for alias in source["aliases"]):
            scope_text = paragraph
            for alias in sorted(source["aliases"], key=len, reverse=True):
                scope_text = scope_text.replace(alias, "")
            dated = _date_matches(paragraph)
            if dated and not PARTIAL_PATTERN.search(scope_text):
                match, effective = dated[0]
                if (match.end() <= reopening.start() and
                        not re.search(r"发布|发表|更新", paragraph[match.end():reopening.start()])):
                    findings.append({"kind": "reopening", "quote": paragraph, "scope": "full",
                                     "actionable": False, "effective_start": effective.isoformat(),
                                     "effective_end": "", "message": "整馆恢复开放，覆盖较早的关闭约束"})
                    continue
        if not (closure or reservation or hours):
            continue
        finding = {"kind": "reservation" if reservation else "opening_hours",
                   "quote": paragraph, "scope": "unknown", "actionable": False,
                   "effective_start": "", "effective_end": "",
                   "message": "预约要求需在官方入口确认" if reservation else "开放时间请核对原文"}
        if closure:
            finding["kind"] = "closure"
            subject_present = any(alias in paragraph for alias in source["aliases"])
            title_scope = title
            for alias in sorted(source["aliases"], key=len, reverse=True):
                title_scope = title_scope.replace(alias, "")
            if (CLOSURE_PATTERN.search(title_scope) and not PARTIAL_PATTERN.search(title_scope)
                    and re.search(r"我院|本院|我馆|本馆|全馆", paragraph)):
                subject_present = True
            scope_text = paragraph
            for alias in sorted(source["aliases"], key=len, reverse=True):
                scope_text = scope_text.replace(alias, "")
            partial = bool(PARTIAL_PATTERN.search(scope_text))
            ambiguous = bool(re.search(r"附近|周边|其他|另一|相邻", paragraph))
            full = subject_present and not ambiguous and (not partial or bool(re.search(r"全馆|整个景区|全院", paragraph)))
            finding["scope"] = "full" if full else "partial" if partial else "unknown"
            dated = _date_matches(paragraph)
            if dated:
                first_match, first_date = dated[0]
                tail = paragraph[first_match.end():]
                applies_from = bool(re.match(r"\s*(?:起|开始|至|到|[-~—－])", tail))
                if len(dated) >= 2:
                    second_match, second_date = dated[1]
                    between = paragraph[first_match.end():second_match.start()]
                    if re.search(r"至|到|[-~—－]", between) and second_date >= first_date:
                        finding["effective_start"] = first_date.isoformat()
                        finding["effective_end"] = second_date.isoformat()
                else:
                    short_end = re.match(r"\s*(?:至|到|[-~—－])\s*(?:(\d{1,2})月)?(\d{1,2})日", tail)
                    if short_end:
                        try:
                            end_date = date(first_date.year, int(short_end.group(1) or first_date.month),
                                            int(short_end.group(2)))
                            if end_date >= first_date:
                                finding["effective_start"] = first_date.isoformat()
                                finding["effective_end"] = end_date.isoformat()
                        except ValueError:
                            pass
                    elif applies_from and re.match(r"\s*(?:起|开始)", tail):
                        finding["effective_start"] = first_date.isoformat()
                    elif (closure.start() > first_match.end() and re.search(r"闭馆|暂停|停止", tail)
                          and (any(alias in paragraph[:first_match.start()] for alias in source["aliases"])
                               or re.search(r"我院|本院|我馆|本馆|全馆|于|自", paragraph[:first_match.start()]))
                          and not re.search(r"发布|发表|更新|公告日期", tail[:closure.start() - first_match.end()])):
                        finding["effective_start"] = first_date.isoformat()
                        finding["effective_end"] = first_date.isoformat()
            recovery = bool(re.search(r"已恢复|恢复正常|恢复开放|恢复对外开放|取消闭馆|不闭馆|正常开放|除外|原定|不再闭馆", paragraph))
            finding["actionable"] = bool(full and finding["effective_start"] and not recovery)
            finding["message"] = ("整馆关闭，按公告日期调整行程" if finding["actionable"]
                                  else "局部关闭，仅提示受影响区域" if partial
                                  else "固定闭馆规则，节假日例外需确认" if re.search(r"周[一二三四五六日天]", paragraph)
                                  else "闭馆范围或日期不明确，待确认")
        findings.append(finding)
        if len(findings) >= 40:
            break
    return sorted(findings, key=lambda item: (not item["actionable"], item["kind"] != "closure"))[:12]


def collect_evidence(city, spots, itinerary, emit, output_dir, enabled=True, limit=3):
    review = {"status": "skipped", "checked": [], "unchecked": [], "changes": [],
              "initial": _snapshot(itinerary, spots), "final": [], "message": "本轮已跳过官网核验（离线模式或已关闭核验）"}
    if not enabled:
        return review
    scheduled = set(itinerary.spot_ids)
    candidates = []
    scheduled_spots = [spot for spot in spots if spot.id in scheduled]
    scheduled_spots.sort(key=lambda spot: official_source(city, spot.name) is None)
    for spot in scheduled_spots:
        source = official_source(city, spot.name)
        if len(candidates) < limit:
            source = source or {"city": city, "name": spot.name, "aliases": [spot.name], "discover": True}
            candidates.append({"spot_id": spot.id, "spot_name": spot.name, **source})
        else:
            review["unchecked"].append({"name": spot.name, "reason": "本轮核验数量上限"})
            candidates.append({"spot_id": spot.id, "spot_name": spot.name, "city": city,
                               "aliases": [spot.name], "price_only": True})
    if not candidates:
        review["message"] = "本轮没有可核验的入选景点"
        return review
    python_exe = os.environ.get("BROWSER_USE_PY", sys.executable)
    review["status"] = "unavailable"
    if not Path(python_exe).is_file():
        review["message"] = "未找到 browser-use 运行环境；行程已生成，官网信息未核验"
        emit(review["message"])
        return review
    run_dir = Path(output_dir).resolve() / "evidence" / uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "request.json"
    trip_dates = [day.date.isoformat() for day in itinerary.days]
    manifest.write_text(json.dumps({"city": city, "targets": candidates, "trip_dates": trip_dates}, ensure_ascii=False), encoding="utf-8")
    review["trip_dates"] = trip_dates
    result_file = run_dir / "result.json"
    environment = os.environ.copy()
    environment.update({"PYTHONUTF8": "1", "ANONYMIZED_TELEMETRY": "false",
                        "BROWSER_USE_CLOUD_SYNC": "false",
                        "BROWSER_USE_CONFIG_DIR": str(Path(output_dir).resolve() / ".browser-use"),
                        "BROWSER_USE_LOGGING_LEVEL": "error"})
    environment["NO_PROXY"] = ",".join(filter(None, [environment.get("NO_PROXY", ""), "localhost,127.0.0.1,::1"]))
    environment["no_proxy"] = environment["NO_PROXY"]
    emit(f"独立后台 Edge 正在查询 {len(candidates)} 个景点票价，前 {limit} 个同时核验官网公告；可以继续使用自己的浏览器")
    if any(item.get("discover") for item in candidates):
        emit("未配置官网的景点将自动搜索，再打开候选网站核对主办/版权声明并采集页面")
    try:
        try:
            with exclusive_browser():
                completed = subprocess.run(
                    [python_exe, str(ROOT / "workers" / "review_browser.py"),
                     "--manifest", str(manifest), "--out", str(result_file)],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=max(150, len(candidates) * 150) + 30, env=environment, cwd=ROOT,
                )
        except (BlockingIOError, PermissionError):
            review["status"] = "busy"
            review["message"] = "另一项官网核验正在使用浏览器，本轮保留行程并标记未核验，请稍后重试"
            emit(review["message"])
            return review
        (run_dir / "worker.log").write_text((completed.stdout + "\n" + completed.stderr)[-20000:], encoding="utf-8")
        if not result_file.is_file():
            raise RuntimeError(f"浏览器未产出结果，退出码 {completed.returncode}")
        payload = json.loads(result_file.read_text(encoding="utf-8"))
        review["checked"] = payload["checked"]
        returned_ids = {item["spot_id"] for item in review["checked"]}
        for candidate in candidates:
            if candidate["spot_id"] not in returned_ids:
                review["unchecked"].append({"name": candidate["spot_name"], "reason": "核验中断或达到时间上限，未取得完整结果"})
        for item in review["checked"]:
            pages = list(item.get("pages", []))
            if item.get("discovery", {}).get("search_page"):
                pages.append(item["discovery"]["search_page"])
            for attempt in item.get("discovery", {}).get("search_attempts", []):
                pages.extend(attempt[key] for key in ("page", "blocked_page") if attempt.get(key))
            for attempt in item.get("ticket_search", {}).get("attempts", []):
                pages.extend(attempt[key] for key in ("page", "blocked_page") if attempt.get(key))
            for page in pages:
                if page.get("screenshot"):
                    page["screenshot_url"] = "/evidence/" + quote(run_dir.name + "/" + page["screenshot"], safe="/")
                    page["screenshot_path"] = str(run_dir / page["screenshot"])
                if page.get("ticket_screenshot"):
                    page["ticket_screenshot_url"] = "/evidence/" + quote(run_dir.name + "/" + page["ticket_screenshot"], safe="/")
                    page["ticket_screenshot_path"] = str(run_dir / page["ticket_screenshot"])
            for attempt in item.get("ticket_search", {}).get("attempts", []):
                for rule in attempt.get("prices", []):
                    rule["screenshot_url"] = attempt.get("page", {}).get("ticket_screenshot_url") or attempt.get("page", {}).get("screenshot_url", "")
                for rule in item.get("ticket_search", {}).get("prices", []):
                    if rule.get("engine") == attempt.get("engine"):
                        rule["screenshot_url"] = attempt.get("page", {}).get("ticket_screenshot_url") or attempt.get("page", {}).get("screenshot_url", "")
            emit(f"官网核验：{item['name']}，{item['status']}，保存 {len(item.get('pages', []))} 页证据")
            if item.get("status") != "captured":
                if not any(unchecked["name"] == item["name"] for unchecked in review["unchecked"]):
                    review["unchecked"].append({"name": item["name"], "reason": item.get("error") or "来源尚未确认"})
        official_count = sum(not item.get("price_only") for item in candidates)
        succeeded = sum(item["status"] == "captured" for item in review["checked"])
        references = sum(bool(item.get("ticket_search", {}).get("prices")) for item in review["checked"])
        review["status"] = "completed" if succeeded == official_count else "partial" if succeeded or references else "failed"
        review["message"] = f"已采集 {succeeded}/{official_count} 个景点官网；另取得 {references} 个景点搜索参考价。搜索摘要未经官网核实；未识别到公告不代表确认正常开放"
        if payload.get("error"):
            review["status"] = "partial" if succeeded or references else "failed"
            review["message"] = f"{payload['error']}；已保留取得的证据，未取得的票价待核实"
            emit(review["message"])
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        review["message"] = f"官网核验未完成：{exc}；保留初版行程，开放情况待确认"
        review["status"] = "failed"
        emit(review["message"])
    return review


def _snapshot(itinerary, spots):
    names = {spot.id: spot.name for spot in spots}
    return [{"date": day.date.isoformat(), "spots": [names.get(leg.spot_id, leg.spot_id) for leg in day.legs]}
            for day in itinerary.days]


def apply_review(requirements, spots, itinerary, result, review, travel=None, emit=None):
    emit = emit or (lambda message: None)
    constrained = {spot.id: spot for spot in spots}
    reasons = {}
    dates = {day.date.isoformat() for day in itinerary.days}
    ticket_updates = []
    for item in review.get("checked", []):
        if item.get("spot_id") not in constrained:
            continue
        ticket_rules = list(item.get("ticket_search", {}).get("prices", []))
        for page in item.get("pages", []) if item.get("status") == "captured" else []:
            for rule in page.get("ticket_prices", []):
                ticket_rules.append({**rule, "url": page["url"], "captured_at": page.get("captured_at", "")})
        if ticket_rules:
            current = constrained[item["spot_id"]]
            selected = pricing.matching_price(ticket_rules, itinerary.days[0].date)
            constrained[current.id] = replace(current, ticket=selected["amount"] if selected else current.ticket,
                                               ticket_prices=tuple(ticket_rules))
            ticket_updates.append({"name": current.name, "rules": ticket_rules})
        if item.get("status") != "captured":
            continue
        blocked = set()
        reopen_dates = [finding["effective_start"] for page in item.get("pages", [])
                        for finding in page.get("findings", [])
                        if finding["kind"] == "reopening" and finding.get("scope") == "full"]
        for page in item.get("pages", []):
            for finding in page.get("findings", []):
                if not finding.get("actionable"):
                    continue
                start, end = finding["effective_start"], finding.get("effective_end") or "9999-12-31"
                affected = {day for day in dates if start <= day <= end}
                affected = {day for day in affected if not any(start <= reopened <= day for reopened in reopen_dates)}
                if affected:
                    blocked.update(affected)
                    reasons[item["spot_id"]] = {"quote": finding["quote"], "url": page["url"]}
        if blocked:
            current = constrained[item["spot_id"]]
            constrained[current.id] = replace(current, closed_dates=tuple(sorted(set(current.closed_dates) | blocked)))
    violated = any(day.date.isoformat() in constrained[leg.spot_id].closed_dates
                   for day in itinerary.days for leg in day.legs if leg.spot_id in constrained)
    review["ticket_updates"] = ticket_updates
    if violated or ticket_updates:
        emit("正在将闭馆约束、官网票价或搜索参考价写入规划，按原预算重排并重新校验……")
        itinerary, result = planner.plan(
            requirements.city, list(constrained.values()), requirements.days, requirements.budget,
            prefer=requirements.prefer, start_date=itinerary.days[0].date,
            start_time=requirements.start_time, end_limit=requirements.end_time, travel=travel,
        )
        for spot_id, reason in reasons.items():
            before = [day["date"] for day in review["initial"] if constrained[spot_id].name in day["spots"]]
            after = [day.date.isoformat() for day in itinerary.days if any(leg.spot_id == spot_id for leg in day.legs)]
            if before != after:
                review["changes"].append({"name": constrained[spot_id].name, "before": before, "after": after,
                                          "reason": reason["quote"], "url": reason["url"]})
        scheduled_names = {constrained[spot_id].name for spot_id in itinerary.spot_ids}
        for day in itinerary.days:
            if not day.legs:
                result.issues.append(simulator.Issue("error", day.day_index, "行程", "官网约束重排后当天无可用景点，请调整日期或需求"))
        for must in requirements.must_see:
            if not any(must in name or name in must for name in scheduled_names):
                result.issues.append(simulator.Issue("error", 0, must, "必去景点未能安排，请调整日期或需求"))
        emit(f"重排完成：调整 {len(review['changes'])} 个景点；票价更新 {len(ticket_updates)} 个；已知费用校验{'通过' if result.ok else '未通过'}，其余票价逐项标注核实状态")
    review["final"] = _snapshot(itinerary, spots)
    checked_ids = {item["spot_id"] for item in review.get("checked", []) if item.get("status") == "captured"}
    unchecked_names = {item["name"] for item in review.get("unchecked", [])}
    for spot_id in itinerary.spot_ids:
        if spot_id not in checked_ids and constrained[spot_id].name not in unchecked_names:
            review["unchecked"].append({"name": constrained[spot_id].name, "reason": "未完成官网核验或为重排后新增景点"})
            unchecked_names.add(constrained[spot_id].name)
    result.browser_review = review
    return itinerary, result


def markdown_lines(review):
    if not review:
        return []
    lines = ["## 官网核验与调整", "", review.get("message", ""), ""]
    for change in review.get("changes", []):
        destination = "、".join(change["after"]) or "本次移除，使用其他景点补排"
        lines.extend([f"- {change['name']}：{'、'.join(change['before'])} → {destination}",
                      f"  - 依据：{change['reason']}", f"  - 来源：{change['url']}"])
    for item in review.get("checked", []):
        lines.extend(["", f"### {item['name']}", "", f"状态：{item['status']}"])
        if item.get("source_method"):
            lines.append("来源方式：" + item["source_method"])
        search = item.get("ticket_search", {})
        if search:
            lines.extend(["", "票价搜索（百度、必应及 360 回退），搜索摘要未经官网核实："])
            for attempt in search.get("attempts", []):
                lines.append(f"- {attempt['engine']}：{attempt['url']}")
                if attempt.get("submitted"):
                    lines.append(f"  - 已按回车提交：{attempt['query']}；匹配结果 {attempt.get('matched_result_count', 0)} 条")
                if attempt.get("answer_wait"):
                    lines.append("  - 首栏加载状态：" + attempt["answer_wait"]["status"])
                    if not attempt.get("prices"):
                        lines.append("  - 首栏原文：" + attempt["answer_wait"].get("first_card", ""))
                if attempt.get("error"):
                    lines.append("  - " + attempt["error"])
                for price in attempt.get("prices", []):
                    lines.append("  - 搜索参考原文：" + price["quote"])
                page = attempt.get("page") or attempt.get("blocked_page") or {}
                screenshot = page.get("ticket_screenshot_path") or page.get("screenshot_path")
                if screenshot:
                    lines.extend(["", "![票价搜索网页截图](" + Path(screenshot).as_uri() + ")", ""])
        discovery = item.get("discovery", {})
        if discovery:
            lines.extend(["", f"搜索词：{discovery['query']}", f"搜索地址：{discovery['search_url']}"])
            identity = discovery.get("identity", {})
            if identity:
                lines.extend(["归属判断：" + identity["reason"], "> " + identity["quote"]])
            if discovery.get("search_page", {}).get("screenshot_path"):
                image_uri = Path(discovery["search_page"]["screenshot_path"]).as_uri()
                lines.extend(["", f"![{item['name']}自动搜索截图]({image_uri})", ""])
        if item.get("error"):
            lines.append(item["error"])
        for page in item.get("pages", []):
            lines.extend(["", f"来源：{page['url']}", f"采集时间：{page['captured_at']}"])
            for price in page.get("ticket_prices", []):
                lines.append("- 成人大门票价格原文：" + price["quote"])
            if page.get("ticket_screenshot_path"):
                lines.extend(["", "![票价区域截图](" + Path(page["ticket_screenshot_path"]).as_uri() + ")", ""])
            for finding in page.get("findings", []):
                lines.append(f"- {finding['message']}：{finding['quote']}")
            if page.get("screenshot_path"):
                image_uri = Path(page["screenshot_path"]).as_uri()
                lines.extend(["", f"![{item['name']}官网截图]({image_uri})", ""])
    if review.get("unchecked"):
        lines.extend(["", "未完成官网核验：" + "、".join(item["name"] for item in review["unchecked"])])
    return lines + [""]
