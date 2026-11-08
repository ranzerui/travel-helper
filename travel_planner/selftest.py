"""端到端自检：直接调用规划入口，批量校验不变量，打印失败清单。

默认**进程内**跑（不需要起服务，也不受"改了代码就自动重启"影响）；
要验证真实 HTTP 链路时加 `--base http://127.0.0.1:8778`。

    python -m travel_planner.selftest                 # 离线（规则规划，快）
    python -m travel_planner.selftest --live          # 联网（高德 POI + 路径）
    python -m travel_planner.selftest --base http://127.0.0.1:8778   # 走真实接口

校验的不变量：
1. 城市识别正确；模糊输入落到默认城市并给出提示
2. 天数、预算按需求生效
3. checks.ok 为 True（行程可行）
4. 预算不超支
5. 多轮追问只改被提到的字段
6. 不出现"风景好"这类伪城市
"""
import argparse
import json
import urllib.parse
import urllib.request

BASE_DEFAULT = ""

FRESH_CASES = [
    ("成都两天，预算1200，喜欢人文", "成都", 2, 1200, "基本"),
    ("北京3天，预算2000", "北京", 3, 2000, "城市+天数"),
    ("去广州玩三天，预算2500，喜欢美食", "广州", 3, 2500, "去X玩N天"),
    ("南京两天，预算800", "南京", 2, 800, "城市+天数(短)"),
    ("昆明3天", "昆明", 3, 1500, "无预算走默认"),
    ("3天西安，预算1800，喜欢人文", "西安", 3, 1800, "时间在前"),
    ("预算3000，五天，哈尔滨", "哈尔滨", 5, 3000, "城市在最后"),
    ("三天在昆明玩", "昆明", 3, 1500, "城市在中间"),
    ("青岛啤酒节三天，预算2000", "青岛", 3, 2000, "城市+事件"),
    ("拉萨两天，预算1500，喜欢自然", "拉萨", 2, 1500, "自然偏好"),
    ("桃3天，预算2000，去风景好的地方", "成都", 3, 2000, "伪城市→默认"),
    ("景色好就行，三天", "成都", 3, 1500, "纯模糊"),
    ("随便找个地方，两天", "成都", 2, 1500, "纯模糊2"),
    ("上海一天，预算1000", "上海", 1, 1000, "一天"),
    ("重庆两天，预算1200，别太赶", "重庆", 2, 1200, "轻松节奏"),
    ("杭州三天，预算2000", "杭州", 3, 2000, "杭州"),
    ("贵阳两天，预算900，喜欢自然", "贵阳", 2, 900, "贵阳"),
    ("金华两天，预算1000", "金华", 2, 1000, "金华"),
]

REFINE_CHAINS = [
    ("成都两天，预算1200，喜欢人文", [
        ("预算改成800", "budget", 800),
        ("加一天", "days", 3),
        ("再紧凑一点", "pace", "紧凑"),
        ("换自然景点", "prefer", "自然"),
        ("改去昆明", "city", "昆明"),
    ]),
    ("北京3天，预算2000", [
        ("预算改成2500", "budget", 2500),
        ("减一天", "days", 2),
        ("轻松点", "pace", "轻松"),
    ]),
]


# 边界用例：(输入, 期望行为, 说明)
#   "error"   应该报错但**不能崩**
#   "default" 应该落到默认城市
#   "clamp"   天数应被夹到上限 14
EDGE_CASES = [
    ("去火星三天，预算1000", "default", "不存在的城市→默认且不落盘"),
    ("昆名两天，预算800", "default", "错字"),
    ("昆明99天，预算5000", "clamp", "天数超上限"),
    ("成都0天，预算1000", "default", "0 天"),
    ("成都两天，预算200", "tight", "预算很紧"),
    ("杭州一天", "ok", "极简输入"),
]


def check_edges(call, base, mode, failures, results):
    from . import citydb, intent

    for query, expect, label in EDGE_CASES:
        payload, stages, error = call(base, {"q": query, "mode": mode})
        if expect == "error":
            if not error:
                failures.append(f"[边界·{label}] {query} -> 应该报错却成功了")
                results.append((label, query, "FAIL", "没报错"))
            else:
                results.append((label, query, "OK", error[:40]))
            continue
        if error:
            failures.append(f"[边界·{label}] {query} -> 不该报错：{error}")
            results.append((label, query, "FAIL", error[:40]))
            continue
        req = payload["requirements"]
        if expect == "default" and req["city"] != "成都":
            failures.append(f"[边界·{label}] {query} -> 城市 {req['city']}，期望默认成都")
        if expect == "clamp" and not (payload["requirements"]["days"] <= 14):
            failures.append(f"[边界·{label}] {query} -> 天数 {req['days']} 没被夹住")
        if expect == "tight":
            # 预算极紧：允许校验不通过，但必须给出原因且不超支太多
            if payload["checks"]["ok"] and payload["budget"]["total"] > payload["budget"]["budget"] + 1:
                failures.append(f"[边界·{label}] {query} -> 校验通过却超支")
            if not payload["checks"]["ok"] and not payload["checks"]["errors"]:
                failures.append(f"[边界·{label}] {query} -> 校验失败但没说原因")
        results.append((label, query, "OK", f"{req['city']}/{len(payload['days'])}天/预算{payload['budget']['budget']:.0f}"))
        # 伪城市绝不能被落盘成数据文件
        from .data import DATA_DIR

        if label == "不存在的城市→默认且不落盘" and any(
            "火星" in path.name for path in DATA_DIR.glob("*.json")
        ):
            failures.append(f"[边界·{label}] {query} -> 生成了 data/火星.json（不该落盘）")
def call_http(base, params, timeout=240):
    url = base + "/api/plan/stream?" + urllib.parse.urlencode(params)
    stages, payload = [], None
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[6:])
            if event["type"] == "stage":
                stages.append(event["text"])
            elif event["type"] == "done":
                payload = event["payload"]
                break
            elif event["type"] == "error":
                return None, stages, event["text"]
    return payload, stages, None


def make_inproc():
    from . import web

    def call(params, timeout=240):
        stages = []
        try:
            payload = web.run_plan(params, stages.append)
        except ValueError as exc:
            return None, stages, str(exc)
        return payload, stages, None

    return lambda base, params, timeout=240: call(params, timeout)


# ---------- 校验 ----------
def check_fresh(call, base, mode, failures, results):
    for query, want_city, want_days, want_budget, label in FRESH_CASES:
        payload, stages, error = call(base, {"q": query, "mode": mode})
        if error:
            failures.append(f"[{label}] {query} -> 接口报错：{error}")
            results.append((label, query, "ERROR", error[:50]))
            continue
        req = payload["requirements"]
        problems = []
        if req["city"] != want_city:
            problems.append(f"城市 {req['city']}≠{want_city}")
        if len(payload["days"]) != want_days:
            problems.append(f"天数 {len(payload['days'])}≠{want_days}")
        if abs(payload["budget"]["budget"] - want_budget) > 1:
            problems.append(f"预算 {payload['budget']['budget']}≠{want_budget}")
        if not payload["checks"]["ok"]:
            problems.append("校验未通过：" + str(payload["checks"]["errors"][:1]))
        if payload["budget"]["total"] > payload["budget"]["budget"] + 1:
            problems.append(f"超支 {payload['budget']['left']}")
        if not any("没听出城市名" in text for text in stages) and want_city == "成都" and "成都" not in query:
            problems.append("模糊输入没给提示")
        if problems:
            failures.append(f"[{label}] {query} -> " + "；".join(problems))
        results.append((label, query, "OK" if not problems else "FAIL", "；".join(problems)))


def check_refine(call, base, mode, failures, results):
    for start, steps in REFINE_CHAINS:
        payload, _, error = call(base, {"q": start, "mode": mode})
        if error:
            failures.append(f"[追问起点] {start} -> {error}")
            continue
        base_params = {f"base_{k}": v for k, v in payload["base"].items()}
        for text, field, expected in steps:
            payload, _, error = call(base, {"q": text, "mode": mode, "refine": "1", **base_params})
            if error:
                failures.append(f"[追问] {start} + {text} -> {error}")
                results.append(("追问", text, "ERROR", error[:50]))
                continue
            base_params = {f"base_{k}": v for k, v in payload["base"].items()}
            actual = payload["requirements"].get(field)
            if isinstance(expected, (int, float)):
                ok = actual is not None and abs(float(actual) - float(expected)) < 1
            else:
                ok = str(actual) == str(expected)
            if not ok:
                failures.append(f"[追问] {start} + {text} -> {field}={actual}，期望 {expected}")
            results.append(("追问", text, "OK" if ok else "FAIL", f"{field}={actual}"))
            if not payload["checks"]["ok"]:
                failures.append(f"[追问] {text} 之后校验未通过")


def check_stress(call, base, mode, failures, results):
    """刁钻输入：只要求"不崩、不胡来"，不要求结果多漂亮。"""
    import threading

    def one(label, query, params=None, note=""):
        payload, stages, error = call(base, {"q": query, "mode": mode, **(params or {})})
        if error:
            failures.append(f"[压力·{label}] {query[:24]} -> 报错：{error}")
            results.append((label, query[:24], "FAIL", error[:40]))
            return None
        results.append((label, query[:24], "OK", note or payload["requirements"]["text"][:28]))
        return payload

    payload = one("双城市", "成都和重庆三天，预算2000")
    if payload and payload["requirements"]["city"] not in {"成都", "重庆"}:
        failures.append(f"[压力·双城市] 城市 {payload['requirements']['city']} 不在成都/重庆里")

    payload = one("半天", "昆明两天半，预算1500")
    if payload and not 2 <= int(payload["requirements"]["days"]) <= 3:
        failures.append(f"[压力·半天] 天数 {payload['requirements']['days']} 不合理")

    one("英文", "beijing 3 days, budget 2000")

    payload = one("必去", "成都两天，预算1500，必去武侯祠")
    if payload and payload["requirements"]["must"] and "武侯祠" not in payload["requirements"]["must"]:
        failures.append("[压力·必去] must 列表里没有武侯祠")

    one("超长", "我想去昆明玩，" * 40 + "预算2000，三天")

    payload = one("emoji", "  成都  两天 😀 预算 1200  ")
    if payload and (payload["requirements"]["city"] != "成都" or len(payload["days"]) != 2):
        failures.append("[压力·emoji] 成都两天没识别对")

    payload = one("预算1元", "成都两天，预算1")
    if payload and not payload["checks"]["ok"] and not payload["checks"]["errors"]:
        failures.append("[压力·预算1元] 校验失败却没给原因")

    payload, _, error = call(base, {"q": "成都两天，预算1200", "mode": mode})
    if error:
        failures.append(f"[压力·连续追问] 起点失败：{error}")
    else:
        base_params = {f"base_{k}": v for k, v in payload["base"].items()}
        for text in ["加一天", "预算改成1500", "轻松点", "再紧凑一点", "改去昆明", "减一天"]:
            payload, _, error = call(base, {"q": text, "mode": mode, "refine": "1", **base_params})
            if error:
                failures.append(f"[压力·连续追问] {text} -> {error}")
                break
            base_params = {f"base_{k}": v for k, v in payload["base"].items()}
        else:
            results.append(("连续追问", "6 步", "OK", payload["requirements"]["text"][:28]))

    payload = one("14天", "西安14天，预算5000")
    if payload and not payload["checks"]["ok"]:
        failures.append(f"[压力·14天] 校验未通过：{payload['checks']['errors'][:1]}")

    errors = []

    def worker(query):
        try:
            _, _, error = call(base, {"q": query, "mode": mode})
            if error:
                errors.append(f"{query} -> {error}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{query} -> {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=worker, args=(q,))
               for q in ("成都两天，预算1200", "杭州三天，预算2000", "广州两天，预算1800")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        failures.append("[压力·并发] " + "；".join(errors))
    else:
        results.append(("并发", "3 个", "OK", "无错误"))


def check_data(failures, results):
    """数据质量：坐标、时间窗、重复 id、字段缺失。"""
    import json
    from pathlib import Path

    from .data import DATA_DIR

    bad = []
    checked = 0
    for path in sorted(Path(DATA_DIR).glob("*.json")):
        if path.name == "cities.json":
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        spots = payload.get("spots", [])
        city = payload.get("city") or path.stem
        if not spots:
            bad.append(f"{path.name}: 没有景点")
            continue
        checked += 1
        ids = set()
        for spot in spots:
            name = spot.get("name") or "?"
            if spot.get("id") in ids:
                bad.append(f"{city}/{name}: id 重复")
            ids.add(spot.get("id"))
            lat, lon = float(spot.get("lat") or 0), float(spot.get("lon") or 0)
            if not (18 <= lat <= 54 and 73 <= lon <= 135):
                bad.append(f"{city}/{name}: 坐标越界 ({lat},{lon})")
            open_time, close_time = spot.get("open_time") or "", spot.get("close_time") or ""
            if not (len(open_time) == 5 and len(close_time) == 5):
                bad.append(f"{city}/{name}: 开放时间格式不对 {open_time}-{close_time}")
            elif open_time >= close_time:
                bad.append(f"{city}/{name}: 开放时间倒挂 {open_time}-{close_time}")
            if float(spot.get("ticket") or 0) < 0:
                bad.append(f"{city}/{name}: 门票为负")
            if not 10 <= int(spot.get("duration_min") or 0) <= 480:
                bad.append(f"{city}/{name}: 游览时长异常 {spot.get('duration_min')}")
    results.append(("数据质量", f"{checked} 个城市", "OK" if not bad else "FAIL",
                    "无问题" if not bad else f"{len(bad)} 处问题"))
    failures.extend(f"[数据] {item}" for item in bad[:20])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="", help="HTTP 地址；不填则进程内执行")
    parser.add_argument("--live", action="store_true", help="联网模式（调高德）")
    args = parser.parse_args(argv)

    mode = "auto" if args.live else "offline"
    call = call_http if args.base else make_inproc()
    where = args.base or "进程内"
    failures, results = [], []
    print(f"自检：{where}　模式：{mode}\n")

    check_fresh(call, args.base, mode, failures, results)
    check_edges(call, args.base, mode, failures, results)
    check_refine(call, args.base, mode, failures, results)
    check_stress(call, args.base, mode, failures, results)
    check_data(failures, results)

    width = max(len(item[1]) for item in results) + 2
    for label, query, status, detail in results:
        mark = {"OK": "✓", "FAIL": "✗", "ERROR": "!"}[status]
        print(f"{mark} [{label:<6}] {query:<{width}} {detail}")

    print(f"\n共 {len(results)} 条，失败 {len(failures)} 条")
    for item in failures:
        print("  -", item)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
