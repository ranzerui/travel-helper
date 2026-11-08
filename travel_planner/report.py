"""交付层：把行程渲染成 Markdown 报告和 .ics 日历。

两个产出物都只依赖行程与校验结果，不再调用模型——保证可复现。
"""
from datetime import date, datetime, timezone

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _risk_lines(city: str, evidence_dir=None) -> list[str]:
    """读 browser-use 采到的官网公告，生成"风险提示"。没有就返回空。"""
    import json
    from pathlib import Path

    base = Path(evidence_dir) if evidence_dir else (Path(__file__).resolve().parent.parent
                                                 / "outputs" / "evidence")
    path = base / city / "announcements.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    notices = payload.get("notices") or []
    if not notices:
        return []
    lines = ["## ⚠️ 风险提示（来自官网公告）", ""]
    for notice in notices[:5]:
        stamp = f"[{notice['date']}] " if notice.get("date") else ""
        lines.append(f"- {stamp}{notice.get('text', '')[:120]}")
    lines.append("")
    lines.append(f"> 来源：{payload.get('url', '')}（browser-use 采集于 {payload.get('captured_at', '')}）")
    lines.append("")
    return lines


def to_markdown(itinerary, report, generated_at=None, trace=None, evidence_dir=None) -> str:
    generated_at = generated_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# {report.city} · {len(itinerary.days)} 天行程（预算 ¥{report.budget:.0f}）",
        "",
        f"生成时间：{generated_at}",
        "",
    ]
    if trace is not None and getattr(trace, "notes", ""):
        lines.extend(["## 行程说明", "", trace.notes, ""])
    lines.extend(report.summary_lines())
    lines.append("")

    for result in report.results:
        weekday = WEEKDAY_NAMES[result.date.weekday()]
        lines.append(f"## Day {result.day_index} · {result.date.isoformat()}（{weekday}）")
        if not result.legs:
            lines.append("")
            lines.append("_当天没有安排（可用景点不足或都已安排过）。_")
            lines.append("")
            continue
        lines.append("")
        lines.append("| 时间 | 安排 | 交通 | 门票 | 开放时间 | 备注 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for leg in result.legs:
            window = f"{leg['arrive'].strftime('%H:%M')}–{leg['depart'].strftime('%H:%M')}"
            source = leg.get("travel_source", "估算")
            mark = "高德" if source.startswith("高德") else "估"
            travel = "—" if leg["travel_min"] == 0 else f"{leg['travel_min']} 分钟（{mark}）"
            ticket = ("待核实" if leg["ticket"] is None else
                      f"搜索参考 ¥{leg['ticket']:.2f}（待核实）" if leg.get("ticket_status") == "search" else
                      f"参考 ¥{leg['ticket']:.2f}（待核实）" if leg.get("ticket_status") == "reference" else
                      ("免费" if leg["ticket"] == 0 else f"¥{leg['ticket']:.2f}") +
                      ("（官网成人大门票）" if leg.get("ticket_status") == "official" else "（本地核实价）"))
            open_window = f"{leg['open_time']}–{leg['close_time']}"
            note = leg["desc"] or ""
            if leg["wait_min"]:
                note = f"（需等 {leg['wait_min']} 分钟开门）" + note
            lines.append(f"| {window} | {leg['name']} | {travel} | {ticket} | {open_window} | {note} |")
        lines.append("")
        lines.append(f"当天结束时间：{result.end_time.strftime('%H:%M')}")
        lines.append("")

    lines.append("## 预算核算")
    lines.append("")
    lines.append("| 项目 | 金额 | 说明 |")
    lines.append("| --- | --- | --- |")
    lines.append(f"| 门票小计 | ¥{report.ticket_total:.2f} | 已有价格合计；未知价格未计入，参考价格未核实 |")
    lines.append(f"| 餐饮 | ¥{report.meal_total:.0f} | 按 {itinerary.meals_per_day:.0f} 元/天估算 |")
    lines.append(f"| 市内交通 | ¥{report.transport_total:.0f} | 按 {itinerary.transport_per_day:.0f} 元/天估算 |")
    balance = (f"{'估算结余' if report.budget_left >= 0 else '估算超支'} ¥{abs(report.budget_left):.2f}"
               if report.budget_complete else "部分门票待核实，不能确认结余或预算可行")
    lines.append(f"| **小计/估算** | **¥{report.total_cost:.2f}** | 用户总预算 ¥{report.budget:.2f}，{balance} |")
    lines.append("")
    lines.append("按1名普通成人，仅核算门票、餐饮和市内交通；住宿、城际交通及附加项目未计入。餐饮和市内交通为估算，不是实时报价。")
    if report.unknown_tickets:
        lines.append("门票待核实：" + "、".join(report.unknown_tickets))
    lines.extend(["", "### 门票价格依据", ""])
    for current in report.results:
        for leg in current.legs:
            evidence = leg.get("ticket_evidence", {})
            if evidence.get("status") in {"official", "local", "search"}:
                lines.append(f"- {leg['name']}（{current.date.isoformat()}）：{leg['ticket']:.2f}元；{'搜索参考价，未经官网核实；' if evidence.get('status') == 'search' else ''}{evidence.get('quote', '')}；来源：{evidence.get('url', '')}；采集/核实时间：{evidence.get('captured_at', '')}")
    lines.append("")

    lines.append("## 信息来源")
    lines.append("")
    lines.append("| 景点 | 状态 | 来源 | 核实日期 | 链接 |")
    lines.append("| --- | --- | --- | --- | --- |")
    seen = set()
    unverified = 0
    for result in report.results:
        for leg in result.legs:
            if leg["name"] in seen:
                continue
            seen.add(leg["name"])
            ok = bool(leg.get("verified"))
            if not ok:
                unverified += 1
            lines.append(f"| {leg['name']} | {'✅ 已核实' if ok else '⚠️ 未核实'} | "
                         f"{leg['source_name'] or '未标注'} | "
                         f"{leg['captured_at'] or '未核实'} | {leg['source_url'] or '—'} |")
    lines.append("")
    if unverified:
        lines.append(f"> ⚠️ 有 {unverified} 个景点包含未核实字段。开放时间可能来自默认值；门票核实状态逐项列出，未知票价不按免费处理。")
        lines.append("")
    lines.append("> 说明：交通时长按直线距离与市内平均速度估算，实际以导航为准；"
                 "开放时间与票价以来源页面为准。")
    lines.append("")

    # browser-use 采到的官网公告（有就附上，没有就整段省略）
    if getattr(report, "browser_review", None):
        from .browser_review import markdown_lines

        lines.extend(markdown_lines(report.browser_review))
    else:
        lines.extend(_risk_lines(report.city, evidence_dir))

    if trace is not None:
        lines.append("## 智能体决策记录")
        lines.append("")
        lines.append(f"- 规划来源：{trace.source}")
        if getattr(trace, "model", ""):
            lines.append(f"- 模型：{trace.model}")
        for item in getattr(trace, "rounds", []):
            status = "校验通过" if not item["errors"] else "；".join(item["errors"][:3])
            lines.append(f"- 第 {item['round']} 轮：选了 {item['spots']} 个景点，{status}")
        if getattr(trace, "filled", 0):
            lines.append(f"- 代码补排：{trace.filled} 个景点（模型提前收工，按时间窗补足后重新校验）")
        if not getattr(trace, "rounds", []):
            lines.append("- 未调用模型（离线模式或模型不可用）")
        lines.append("")
    return "\n".join(lines) + "\n"


def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def to_ics(itinerary, report) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//travel-planner//CN",
        "CALSCALE:GREGORIAN",
    ]
    for result in report.results:
        for index, leg in enumerate(result.legs):
            start = datetime.combine(result.date, leg["arrive"])
            end = datetime.combine(result.date, leg["depart"])
            lines.extend([
                "BEGIN:VEVENT",
                f"UID:{result.date.strftime('%Y%m%d')}-{index}-{abs(hash(leg['name'])) % 10**8}@travel-planner",
                f"DTSTAMP:{stamp}",
                f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{_ics_escape(leg['name'])}",
                f"LOCATION:{_ics_escape(report.city + ' ' + leg['name'])}",
                f"DESCRIPTION:{_ics_escape(leg['desc'])}",
                "END:VEVENT",
            ])
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def write_outputs(itinerary, report, out_dir, trace=None):
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    md_path = out / f"itinerary_{report.city}_{stamp}.md"
    ics_path = out / f"itinerary_{report.city}_{stamp}.ics"
    md_path.write_text(to_markdown(itinerary, report, trace=trace), encoding="utf-8")
    # newline="" 防止 Windows 把 \r\n 再翻译成 \r\r\n，日历文件必须严格按 RFC 换行
    ics_path.write_text(to_ics(itinerary, report), encoding="utf-8", newline="")
    return md_path, ics_path
