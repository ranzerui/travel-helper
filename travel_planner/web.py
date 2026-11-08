"""本地网页版：像聊天一样用行程生成器。

实现要点：
- 只用标准库 http.server，不引 Flask/FastAPI，不联网也能开；
- 生成过程用 SSE 推事件，前端能看到"解析需求 → 取证据 → 规划 → 校验 → 补排"的进度；
- 产出文件通过 /files/<文件名> 下载，路径做了白名单校验。
"""
import argparse
import hashlib
import json
import mimetypes
import os
import sys
import queue
import re
import threading
import urllib.request
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import agent_planner, browser_review, data, intent, report, routes, sources, travel_dates
from .runtime import browser_available

ROOT = Path(__file__).resolve().parent
UI_FILE = ROOT / "webui" / "index.html"
OUTPUT_DIR = ROOT.parent / "outputs"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _compute_build() -> str:
    """按代码内容算构建号：改一行代码就变，旧进程没法冒充新代码。"""
    digest = hashlib.sha1()
    for path in (sorted(ROOT.glob("*.py")) + sorted(ROOT.glob("webui/*"))
                 + sorted((ROOT.parent / "workers").glob("*.py")) + [ROOT / "official_sources.json"]):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}".encode("utf-8"))
    return digest.hexdigest()[:8]


BUILD = _compute_build()


def _leg_payload(leg: dict) -> dict:
    return {
        "name": leg["name"],
        "arrive": leg["arrive"].strftime("%H:%M"),
        "depart": leg["depart"].strftime("%H:%M"),
        "travel": leg["travel_min"],
        "travel_source": leg.get("travel_source", "估算"),
        "ticket": leg["ticket"],
        "ticket_status": leg.get("ticket_status", "unknown"),
        "ticket_evidence": leg.get("ticket_evidence", {}),
        "open": leg["open_time"],
        "close": leg["close_time"],
        "wait": leg["wait_min"],
        "desc": leg["desc"],
        "source": leg["source_name"],
        "url": leg["source_url"],
    }


def _base_requirements(params: dict):
    """从请求里恢复上一版需求（多轮追问用）。"""
    if not params.get("base_days"):
        return None
    try:
        return intent.Requirements(
            city=params.get("base_city") or "成都",
            days=int(params["base_days"]),
            budget=float(params.get("base_budget") or 1500),
            prefer=params.get("base_prefer") or "",
            pace=params.get("base_pace") or "适中",
            start_time=params.get("base_start") or "09:00",
            end_time=params.get("base_end") or "20:00",
            must_see=[name for name in (params.get("base_must") or "").split("|") if name],
            start_date=params.get("base_start_date") or "",
            budget_source=params.get("base_budget_source") or "沿用上一版总预算",
        )
    except (TypeError, ValueError):
        return None


def run_plan(params: dict, emit) -> dict:
    """执行一次完整流程，返回前端要渲染的结构。emit 用于推送进度。"""
    query = (params.get("q") or "").strip()
    mode = params.get("mode") or "auto"
    if not query:
        raise ValueError("请先说说你的需求，比如：成都两天，预算 1200，喜欢人文")

    base = _base_requirements(params)
    current = travel_dates.local_now()
    explicit_date = params.get("start_date") or ""
    refine = base is not None and (params.get("refine") == "1" or intent.is_refine(query))
    if refine:
        emit("收到调整，叠加到上一版需求……")
        requirements, changes = intent.apply_delta(base, query, now=current, explicit_start_date=explicit_date)
        for change in changes:
            emit("· " + change)
        if not changes:
            emit("没有识别到明确改动，按上一版需求重排")
    else:
        emit("正在理解需求……")
        requirements = intent.parse_requirements(query, use_llm=(mode == "model"), now=current,
                                                 explicit_start_date=explicit_date)
    start_date = date.fromisoformat(requirements.start_date)
    emit(f"需求：{requirements.describe()}（理解来源：{requirements.source}）")
    emit(f"总预算：¥{requirements.budget:g}（{requirements.budget_source}）；按1名成人核算门票、餐饮和市内交通")
    emit(f"出游日期：{requirements.start_date}（{requirements.date_source}；电脑时间 {current.isoformat(timespec='seconds')}）")
    if not refine and not intent.extract_city(query):
        emit("没听出城市名，先按默认城市处理；说「去昆明」这种就能指定")
    source_name = "本地数据"
    notes = []
    auto = mode != "offline"
    if not auto:
        emit("离线模式：只读本地已有城市，不联网取新城市")
    if mode == "bing":
        emit("正在从必应取证据（RSS + 词典匹配）……")
        data.ensure_city(requirements.city, auto=auto, progress=emit)
        source = sources.BingSource()
        spots = source.fetch(requirements.city)
        notes = list(getattr(source, "notes", []) or [])
        source_name = source.name
        emit(f"{source_name}：{len(spots)} 个景点" + (f"，{notes[0]}" if notes else ""))
    else:
        try:
            spots = data.ensure_city(requirements.city, auto=auto, progress=emit)
        except FileNotFoundError as exc:
            raise ValueError(str(exc)) from exc
        emit(f"读取本地数据：{len(spots)} 个景点")

    # 交通时长：联网且配了 key 就用高德，失败逐段回退估算
    route_provider = routes.build_provider("auto", allow_network=auto)
    if getattr(route_provider, "name", "") != "直线估算":
        emit(f"交通：{route_provider.name}（失败的段会回退估算）")

    emit("规划行程" + ("（本地模型，可能要等十几秒）……" if mode == "model" else "……"))
    itinerary, result, trace = agent_planner.plan_with_agent(
        requirements, spots, start_date,
        use_llm=(mode == "model"), max_rounds=3, travel=route_provider,
    )

    for item in trace.rounds:
        if item["errors"]:
            emit(f"第 {item['round']} 轮校验没通过：" + "；".join(item["errors"][:2]))
        else:
            emit(f"第 {item['round']} 轮校验通过")
    if trace.filled:
        emit(f"模型提前收工，代码按时间窗补排了 {trace.filled} 个景点，并重新校验")
    if not result.ok:
        emit("校验未通过：" + "；".join(issue.message for issue in result.errors[:3]))

    review = browser_review.collect_evidence(
        requirements.city, spots, itinerary, emit, OUTPUT_DIR,
        enabled=auto and params.get("browser_review", "1") != "0",
    )
    itinerary, result = browser_review.apply_review(
        requirements, spots, itinerary, result, review, travel=route_provider, emit=emit,
    )
    if review["changes"] or review.get("ticket_updates"):
        trace.notes = "网页证据或票价参考触发规则重排，沿用用户总预算重新核算；票价证据及核实状态逐项列出。"
        trace.source += " + 网页证据/票价约束重排"

    md_path, ics_path = report.write_outputs(itinerary, result, OUTPUT_DIR, trace)
    emit("已生成行程表和日历文件")

    return {
        "requirements": {
            "text": requirements.describe(),
            "source": requirements.source,
            "city": requirements.city,
            # 结构化字段也返回，前端/自检不用解析文本
            "days": requirements.days,
            "budget": requirements.budget,
            "prefer": requirements.prefer,
            "pace": requirements.pace,
            "start": requirements.start_time,
            "end": requirements.end_time,
            "must": list(requirements.must_see),
            "start_date": requirements.start_date,
            "date_source": requirements.date_source,
            "reference_time": current.isoformat(timespec="seconds"),
            "budget_source": requirements.budget_source,
        },
        "data_source": source_name,
        "data_notes": notes,
        "browser_review": review,
        "base": {
            "city": requirements.city,
            "days": requirements.days,
            "budget": requirements.budget,
            "prefer": requirements.prefer,
            "pace": requirements.pace,
            "start": requirements.start_time,
            "end": requirements.end_time,
            "must": "|".join(requirements.must_see),
            "start_date": requirements.start_date,
            "budget_source": requirements.budget_source,
        },
        "trace": {
            "source": trace.source,
            "model": trace.model,
            "rounds": trace.rounds,
            "filled": trace.filled,
            "notes": trace.notes,
        },
        "days": [{
            "index": item.day_index,
            "date": item.date.isoformat(),
            "weekday": WEEKDAYS[item.date.weekday()],
            "end": item.end_time.strftime("%H:%M"),
            "legs": [_leg_payload(leg) for leg in item.legs],
        } for item in result.results],
        "budget": {
            "ticket": result.ticket_total,
            "meal": result.meal_total,
            "transport": result.transport_total,
            "total": result.total_cost,
            "budget": result.budget,
            "left": result.budget_left if result.budget_complete else None,
            "complete": result.budget_complete,
            "unknown_tickets": result.unknown_tickets,
            "status": "over_budget" if result.total_cost > result.budget else "estimated_within_budget" if result.budget_complete else "incomplete",
            "scope": "按1名成人，仅含门票、餐饮、市内交通；住宿、城际交通及附加项目未计入",
            "assumptions": "餐饮120元/天、市内交通30元/天为估算，未查询实时报价",
        },
        "checks": {
            "ok": result.ok,
            "budget_ok": False if result.total_cost > result.budget else True if result.budget_complete else None,
            "feasible": result.ok and result.budget_complete,
            "errors": [f"Day{issue.day} {issue.spot}：{issue.message}" for issue in result.errors],
            "warnings": [f"Day{issue.day} {issue.spot}：{issue.message}" for issue in result.warnings],
        },
        "files": {"md": md_path.name, "ics": ics_path.name},
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "TravelPlannerUI/1.0"

    def log_message(self, fmt, *args):  # 安静一点，不刷屏
        return

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._send_file(UI_FILE, "text/html; charset=utf-8")
        if path == "/api/health":
            from . import llm
            from .settings import amap_key
            intent_file = ROOT / "intent.py"
            return self._send_json({
                "ok": True,
                "ollama": llm.available(),
                "model": llm.default_model(),
                "amap": bool(amap_key()),
                "browser_review": browser_available(),
                "cities": len(list((ROOT / "data").glob("*.json"))),
                "build": BUILD,
                "code_path": str(ROOT),
                "intent_mtime": datetime.fromtimestamp(intent_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
                if intent_file.exists() else "",
            })
        if path == "/api/settings":
            from . import llm, settings

            return self._send_json({
                "settings": settings.current(),
                "models": llm.list_models(),
                "ollama": llm.available(),
                "build": BUILD,
            })
        if path == "/api/plan/stream":
            params = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            return self._stream_plan(params)
        if path.startswith("/evidence/"):
            return self._serve_evidence(unquote(path[len("/evidence/"):]))
        if path.startswith("/files/"):
            # URL 里的中文文件名是百分号编码的，先解码再取 basename
            name = Path(unquote(path.split("/files/", 1)[1])).name
            target = OUTPUT_DIR / name
            if target.exists() and target.parent == OUTPUT_DIR:
                kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
                return self._send_file(target, kind, download=name)
        return self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/settings":
            return self._send_json({"error": "not found"}, status=404)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            updates = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(updates, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except (ValueError, OSError) as exc:
            return self._send_json({"ok": False, "errors": [f"请求解析失败：{exc}"]}, status=400)

        from . import llm, settings

        result = settings.apply(updates)
        result["models"] = llm.list_models()
        result["ollama"] = llm.available()
        return self._send_json(result, status=200 if result.get("ok") else 400)

    # ---------- 工具 ----------
    def _serve_evidence(self, relative):
        base = (OUTPUT_DIR / "evidence").resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base) or target.suffix.lower() != ".png" or not target.is_file():
            return self._send_json({"error": "截图不存在"}, status=404)
        return self._send_file(target, "image/png")

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str, download: str | None = None):
        if not path.exists():
            return self._send_json({"error": "文件不存在"}, status=404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 页面必须每次重新取，否则浏览器会缓存旧 UI，改了也看不到
        self.send_header("Cache-Control", "no-store, must-revalidate")
        if download:
            # HTTP 头只能用 latin-1，中文文件名必须走 RFC 5987 的 filename*
            ascii_fallback = re.sub(r"[^A-Za-z0-9._-]", "_", download)
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(download)}",
            )
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, event: dict):
        chunk = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(chunk)
        self.wfile.flush()

    def _stream_plan(self, params):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()

        events: queue.Queue = queue.Queue()

        def emit(text):
            events.put({"type": "stage", "text": text})

        def work():
            try:
                events.put({"type": "done", "payload": run_plan(params, emit)})
            except Exception as exc:  # noqa: BLE001 - 错误要回传给前端
                events.put({"type": "error", "text": str(exc)})

        threading.Thread(target=work, daemon=True).start()

        while True:
            try:
                event = events.get(timeout=900)
            except queue.Empty:
                self._sse({"type": "error", "text": "处理超时"})
                break
            try:
                self._sse(event)
            except (BrokenPipeError, ConnectionResetError):
                break
            if event["type"] in {"done", "error"}:
                break


def _probe_health(port: int, timeout: float = 2.0):
    """看看这个端口上已经跑着的是什么版本（没有 build 字段 = 旧实例）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


class _Server(ThreadingHTTPServer):
    # Windows 上 allow_reuse_address=True 会允许两个进程绑同一端口，
    # 结果新实例看似启动成功、请求却进了旧实例（表现就是"改了没生效"）。
    allow_reuse_address = False
    daemon_threads = True


def _try_bind(port: int):
    try:
        return _Server(("127.0.0.1", port), Handler)
    except OSError:
        return None


def _watch_code(build: str, on_change=None):
    """代码一改就让进程退出（退出码 3），由启动脚本自动拉起。

    这样"改了代码但页面还是旧行为"不会再出现——用户不用记得手动重启。
    """
    import time as _time

    def loop():
        while True:
            _time.sleep(2.0)
            if _compute_build() != build:
                print("\n检测到代码更新，自动重启中……")
                if on_change:
                    on_change()
                import os as _os
                _os._exit(3)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地网页版行程生成器")
    parser.add_argument("--port", type=int, default=8777)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-reload", action="store_true", help="代码改动时不自动重启")
    args = parser.parse_args(argv)

    port = args.port
    server = _try_bind(port)
    if server is None:
        existing = _probe_health(port)
        if existing and existing.get("build") == BUILD:
            print(f"端口 {port} 上已经跑着同一版本（构建 {BUILD}）：http://127.0.0.1:{port}/")
            print("直接用这个地址即可；要另开一个实例请加 --port。")
            return 0
        if existing:
            print(f"⚠️ 端口 {port} 上是一个旧实例（build={existing.get('build') or '无'}）。")
            print("   它跑的是旧代码，所以你会看到「改了没生效」。请关掉那个命令行窗口。")
        for candidate in range(port + 1, port + 6):
            server = _try_bind(candidate)
            if server is not None:
                port = candidate
                print(f"已改用端口 {port} 启动，打开：http://127.0.0.1:{port}/")
                break
        if server is None:
            print(f"端口 {args.port}~{args.port + 5} 都被占用，请用 --port 指定一个空闲端口。")
            return 1

    url = f"http://127.0.0.1:{port}/"
    print("=" * 56)
    print(f"  打开这个地址：{url}")
    print("=" * 56)
    print(f"构建：{BUILD}｜代码路径：{ROOT}")
    print(f"页面右上角会显示构建号；不是 {BUILD} 就是旧页面，按 Ctrl+F5 强刷。")
    print("关闭：在这个窗口按 Ctrl+C")

    # 地址写一份到文件，窗口被关掉/找不到时还能查
    try:
        address_file = OUTPUT_DIR / "网页地址.txt"
        address_file.parent.mkdir(parents=True, exist_ok=True)
        address_file.write_text(f"{url}\n构建 {BUILD}\n", encoding="utf-8")
    except OSError:
        pass

    if not args.no_reload:
        _watch_code(BUILD)
    if not args.no_browser:
        threading.Timer(0.8, lambda: _open_browser(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
    return 0


BROWSER_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def _open_browser(url: str) -> None:
    """按可靠性依次尝试：Windows 关联 → webbrowser → 直接拉 Edge/Chrome。

    用户环境里默认浏览器是"夸克"，它的协议处理不一定稳，
    所以最后一档是硬编码常见浏览器的绝对路径。
    """
    import os
    import subprocess

    if os.name == "nt":
        try:
            os.startfile(url)  # 走系统默认浏览器
            return
        except OSError:
            pass
    try:
        if webbrowser.open(url):
            return
    except Exception:  # noqa: BLE001 - 打不开浏览器不影响服务
        pass
    for path in BROWSER_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            subprocess.Popen([path, url], close_fds=True)
            return
        except OSError:
            continue
    print(f"自动打开浏览器失败，请手动在浏览器里输入：{url}")


if __name__ == "__main__":
    main()
