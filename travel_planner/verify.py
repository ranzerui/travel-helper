"""数据核实闭环：导出待核实清单 → 人工/官方源填写 → 回导并标记 verified。

为什么需要它：
抓取和 POI 搜索能给出"名字、坐标、地址"，但给不了"几点开门、多少钱、周几闭馆"。
这三项只能人工或官方源确认。没有这个闭环，`verified` 就只是一个永远为 false 的字段。

约定：
- 导出的 CSV 用 UTF-8 BOM，Excel 双击打开不乱码；
- 回导时逐行校验，格式不对的行跳过并给原因（不静默丢弃）；
- 只有回导成功的行才会被标 `verified: true`，并记录核实日期。
"""
import csv
import json
import re
from datetime import date
from pathlib import Path

from .data import DATA_DIR

HEADERS = ["id", "name", "address", "open_time", "close_time", "ticket",
           "closed_days", "verified", "source_name", "source_url", "captured_at", "evidence"]
WEEKDAYS = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")


def _city_file(city: str) -> Path:
    path = DATA_DIR / f"{city}.json"
    if not path.exists():
        for candidate in DATA_DIR.glob("*.json"):
            if json.loads(candidate.read_text(encoding="utf-8")).get("city") == city:
                return candidate
        raise FileNotFoundError(f"没有 {city} 的数据文件")
    return path


def _load(city: str) -> tuple[Path, dict]:
    path = _city_file(city)
    return path, json.loads(path.read_text(encoding="utf-8"))


def _weekday_names(days) -> str:
    return "、".join([name for name, index in WEEKDAYS.items() if index in (days or [])])


def _baseline(spot: dict) -> dict:
    """导出时预填的值来自哪里：有 suggested 用 suggested，否则用现值。

    回导时"和预填值一样"不算人工确认——否则"导出→原样存回"就会把建议值
    当成核实结果，可信度体系就废了。
    """
    suggested = spot.get("suggested") or {}
    if suggested:
        return {
            "open_time": suggested.get("open_time") or spot.get("open_time", ""),
            "close_time": suggested.get("close_time") or spot.get("close_time", ""),
            "ticket": suggested["ticket"] if suggested.get("ticket") is not None else spot.get("ticket", 0),
            "closed_days": suggested.get("closed_days") if suggested.get("closed_days") else (spot.get("closed_days") or []),
        }
    return {
        "open_time": spot.get("open_time", ""),
        "close_time": spot.get("close_time", ""),
        "ticket": spot.get("ticket", 0),
        "closed_days": spot.get("closed_days") or [],
    }


def export_pending(city: str, path: Path, only_pending: bool = True) -> int:
    """导出待核实清单（CSV）。有建议值的会预填，并附上引用供人工核对。"""
    _, payload = _load(city)
    rows = []
    for spot in payload.get("spots", []):
        if only_pending and spot.get("verified"):
            continue
        base = _baseline(spot)
        suggested = spot.get("suggested") or {}
        evidence = ""
        if suggested:
            parts = [suggested.get("quote") or "", suggested.get("source_url") or ""]
            evidence = " | ".join(part for part in parts if part)
        rows.append({
            "id": spot.get("id", ""),
            "name": spot.get("name", ""),
            "address": spot.get("desc", ""),
            "open_time": base["open_time"],
            "close_time": base["close_time"],
            "ticket": base["ticket"],
            "closed_days": _weekday_names(base["closed_days"]),
            "verified": "是" if spot.get("verified") else "否",
            "source_name": spot.get("source_name", ""),
            "source_url": spot.get("source_url", ""),
            "captured_at": spot.get("captured_at", ""),
            "evidence": evidence,
        })
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def _parse_closed_days(text: str) -> tuple[list[int], str]:
    text = (text or "").strip()
    if not text:
        return [], ""
    days = []
    for token in re.split(r"[、,，\s]+", text):
        token = token.strip()
        if not token:
            continue
        if token not in WEEKDAYS:
            return [], f"闭馆日写法不认识：{token}（用 周一、周三 这种）"
        days.append(WEEKDAYS[token])
    return sorted(set(days)), ""


def import_updates(city: str, path: Path) -> dict:
    """把人工填好的 CSV 回导。返回 {updated, skipped, errors}。"""
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")
    data_path, payload = _load(city)
    index = {str(spot.get("id")): spot for spot in payload.get("spots", [])}
    by_name = {str(spot.get("name")): spot for spot in payload.get("spots", [])}

    updated, unchanged, skipped, errors = 0, 0, 0, []
    with open(path, encoding="utf-8-sig", newline="") as stream:
        for line_no, row in enumerate(csv.DictReader(stream), start=2):
            spot = index.get((row.get("id") or "").strip()) or by_name.get((row.get("name") or "").strip())
            if spot is None:
                skipped += 1
                errors.append(f"第{line_no}行：找不到对应的景点（id/name 都对不上）")
                continue
            open_time = (row.get("open_time") or "").strip()
            close_time = (row.get("close_time") or "").strip()
            ticket_raw = (row.get("ticket") or "").strip()
            if not TIME_RE.match(open_time) or not TIME_RE.match(close_time):
                skipped += 1
                errors.append(f"第{line_no}行：开放时间格式应为 HH:MM（现在是 {open_time!r}/{close_time!r}）")
                continue
            try:
                ticket = float(ticket_raw or 0)
            except ValueError:
                skipped += 1
                errors.append(f"第{line_no}行：门票不是数字（{ticket_raw!r}）")
                continue
            if ticket < 0:
                skipped += 1
                errors.append(f"第{line_no}行：门票不能为负")
                continue
            closed_days, problem = _parse_closed_days(row.get("closed_days") or "")
            if problem:
                skipped += 1
                errors.append(f"第{line_no}行：{problem}")
                continue

            # 只有真的改过（或显式标了"是"）才算核实；原样返回的行不动。
            # 注意基准是"导出时预填的值"：如果抓取给了建议值，原样存回也不算确认，
            # 否则"导出→原样存回"就把机器候选当成人工核实了。
            base = _baseline(spot)
            changed = (
                open_time != str(base["open_time"] or "")
                or close_time != str(base["close_time"] or "")
                or abs(ticket - float(base["ticket"] or 0)) > 1e-9
                or closed_days != sorted(base["closed_days"] or [])
            )
            marked = str(row.get("verified") or "").strip().lower() in {"是", "y", "yes", "true", "1"}
            if not changed and not marked:
                unchanged += 1
                continue

            spot["open_time"] = open_time
            spot["close_time"] = close_time
            spot["ticket"] = ticket
            spot["closed_days"] = closed_days
            spot["verified"] = True
            spot["captured_at"] = date.today().isoformat()
            spot["source_name"] = (spot.get("source_name") or "") if "人工核实" in (spot.get("source_name") or "") \
                else f"{spot.get('source_name') or '未标注'} + 人工核实"
            updated += 1

    data_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"updated": updated, "unchanged": unchanged, "skipped": skipped,
            "errors": errors, "path": str(data_path)}


def health() -> list[dict]:
    """所有城市的数据健康度：多少已核实、来源分布、最老的核实日期。"""
    result = []
    for path in sorted(DATA_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        spots = payload.get("spots", [])
        total = len(spots)
        verified = sum(1 for spot in spots if spot.get("verified"))
        dates = sorted(spot.get("captured_at") for spot in spots if spot.get("captured_at"))
        kinds = {}
        for spot in spots:
            source = str(spot.get("source_name") or "未标注").split("（")[0]
            kinds[source] = kinds.get(source, 0) + 1
        result.append({
            "city": payload.get("city") or path.stem,
            "kind": payload.get("kind") or "fixture",
            "total": total,
            "verified": verified,
            "ratio": (verified / total) if total else 0.0,
            "oldest": dates[0] if dates else "",
            "sources": kinds,
        })
    return result
