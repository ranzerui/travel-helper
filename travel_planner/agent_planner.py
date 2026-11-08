"""智能体规划器：模型决定去哪、怎么排；代码校验；不合格带着问题回炉。

分工（这也是这个项目的核心设计）：
- 模型：读需求 → 从候选景点里选、分配到每天、写行程说明
- 代码：把结果当输入做可行性推演（闭馆日、时间窗、预算、重复），有问题就把清单交还模型
- 兜底：模型不可用或连续失败，退回确定性规划器，保证永远有能跑通的行程
"""
import json
from dataclasses import dataclass, field
from datetime import date

from . import llm, planner, simulator
from .data import open_on
from .intent import Requirements
from .models import DayPlan, Itinerary, Leg, Spot

WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
PACE_HINT = {"轻松": "每天 2~3 个", "适中": "每天 3~4 个", "紧凑": "每天 4~5 个"}


@dataclass
class PlanTrace:
    """决策记录，写进报告，方便复盘"模型第几轮才排对"。"""

    model: str = ""
    source: str = "规则"
    rounds: list[dict] = field(default_factory=list)
    notes: str = ""
    filled: int = 0

    def summary(self) -> str:
        if not self.rounds:
            return f"规划来源：{self.source}"
        lines = [f"规划来源：{self.source}｜模型：{self.model}｜共 {len(self.rounds)} 轮"]
        for item in self.rounds:
            status = "通过" if not item["errors"] else f"{len(item['errors'])} 个问题"
            lines.append(f"  第{item['round']}轮：选了 {item['spots']} 个景点，校验{status}")
        if self.filled:
            lines.append(f"  代码补排：{self.filled} 个景点（模型提前收工，solver 按时间窗补足）")
        return "\n".join(lines)


def plan_with_agent(req: Requirements, spots: list[Spot], start_date: date,
                    use_llm: bool = True, model: str | None = None,
                    max_rounds: int = 3, travel=None) -> tuple[Itinerary, simulator.Report, PlanTrace]:
    # 每次现取：设置页改了模型后不用重启也能生效
    model = model or llm.default_model()
    trace = PlanTrace(model=model, source="规则")

    if not use_llm:
        trace.source = "规则（离线模式）"
        itinerary, report = planner.plan(
            city=req.city, spots=spots, days=req.days, budget=req.budget,
            prefer=req.prefer, start_date=start_date,
            start_time=req.start_time, end_limit=req.end_time, travel=travel,
        )
        return itinerary, report, trace

    if not llm.available():
        trace.source = "规则（Ollama 未运行）"
        itinerary, report = planner.plan(
            city=req.city, spots=spots, days=req.days, budget=req.budget,
            prefer=req.prefer, start_date=start_date,
            start_time=req.start_time, end_limit=req.end_time, travel=travel,
        )
        return itinerary, report, trace

    spot_map = {spot.id: spot for spot in spots}
    # 需求解析（尤其模型解析）可能编出候选里没有的"必去景点"，先按词典过滤
    known_names = [spot.name for spot in spots]
    req.must_see = [name for name in req.must_see
                    if any(name in known or known in name for known in known_names)]
    dates = [start_date.fromordinal(start_date.toordinal() + index) for index in range(req.days)]
    feedback = ""

    for round_index in range(1, max_rounds + 1):
        prompt = _planning_prompt(req, spots, dates, feedback)
        try:
            raw = llm.chat(prompt, model=model)
        except llm.LLMError as exc:
            trace.rounds.append({"round": round_index, "spots": 0, "errors": [str(exc)], "raw": ""})
            break

        chosen = _parse_days(raw, req.days)
        if chosen is None:
            trace.rounds.append({"round": round_index, "spots": 0, "errors": ["输出格式不符合要求"], "raw": raw[:200]})
            feedback = "上次输出不是合法JSON。只输出 {\"days\":[[\"景点id\",...], ...]}，天数必须正好相等。"
            continue

        itinerary = _build_itinerary(req, chosen, spot_map, dates)
        report = simulator.simulate(itinerary, spot_map, travel=travel)
        errors = [f"Day{issue.day} {issue.spot}：{issue.message}" for issue in report.errors]
        chosen_names = {spot_map[spot_id].name for spot_id in itinerary.spot_ids if spot_id in spot_map}
        missing = [name for name in req.must_see if name not in chosen_names]
        if missing:
            errors.append("没有安排用户明确要求必去的景点：" + "、".join(missing))
        trace.rounds.append({
            "round": round_index,
            "spots": len(itinerary.spot_ids),
            "errors": errors,
            "raw": raw[:300],
        })
        if report.ok and not missing:
            itinerary, report, filled = _fill_sparse_days(itinerary, report, req, spot_map, travel=travel)
            trace.filled = filled
            trace.source = f"模型（第 {round_index} 轮通过）"
            trace.notes = _write_notes(req, itinerary, report, model)
            return itinerary, report, trace
        feedback = "上一版行程校验没通过，请针对下面每条问题重排（只能从候选里选，不能新增景点）：\n" + \
                   "\n".join(f"- {item}" for item in errors)

    trace.source = f"规则（模型 {len(trace.rounds)} 轮未通过，已回退）"
    itinerary, report = planner.plan(
        city=req.city, spots=spots, days=req.days, budget=req.budget,
        prefer=req.prefer, start_date=start_date,
        start_time=req.start_time, end_limit=req.end_time,
    )
    return itinerary, report, trace


def _candidate_lines(spots: list[Spot], prefer: str) -> str:
    ordered = sorted(spots, key=lambda spot: (-(spot.category == prefer), spot.ticket, spot.duration_min))
    lines = []
    for spot in ordered:
        ticket_label = (f"{spot.ticket:g}元（来源价，按出游日期再核算）" if spot.verified or spot.ticket_prices
                        else f"参考{spot.ticket:g}元，待核实" if spot.ticket > 0 else "票价未知，不能按免费承诺预算")
        closed = "、".join(WEEKDAYS[day] for day in spot.closed_days) or "无"
        lines.append(
            f"{spot.id} | {spot.name} | {spot.category} | {ticket_label} | "
            f"{spot.open_time}-{spot.close_time} | {spot.duration_min}分钟 | 闭馆:{closed}"
        )
    return "\n".join(lines)


def _planning_prompt(req: Requirements, spots: list[Spot], dates: list[date], feedback: str) -> str:
    date_lines = "\n".join(f"第{index + 1}天：{current.isoformat()}（{WEEKDAYS[current.weekday()]}）"
                           for index, current in enumerate(dates))
    prompt = (
        "你是行程规划助手。根据用户需求，从候选景点里选择并分配到每天。\n"
        "规则：\n"
        "1. 只能从候选列表里选，id 必须一字不差；\n"
        "2. 闭馆日不能安排；开放时间太短的景点不要硬塞；\n"
        "3. 同一天的景点顺序按地理位置顺路排列，不要来回折返；\n"
        "4. 门票总额要留在预算内（餐饮和市内交通每天约 150 元会被另外计算）；\n"
        f"5. 节奏：{req.pace}，建议每天 {PACE_HINT.get(req.pace, '3~4')} 景点；\n"
        "6. 每天尽量排到 16:30 之后结束，但不要超过 20:00；上午和下午都要有安排，不要半天就结束。\n"
        f"用户需求：{req.describe()}，出发时间 {req.start_time}，每天最晚 {req.end_time}。\n"
        f"日期：\n{date_lines}\n"
        f"候选景点（id | 名称 | 类别 | 门票 | 开放 | 建议时长 | 闭馆日）：\n{_candidate_lines(spots, req.prefer)}\n"
        '只输出JSON：{"days": [["景点id", "景点id"], ["景点id"]]}，外层数组长度必须等于 '
        f"{req.days} 天。\n"
    )
    if feedback:
        prompt += feedback + "\n"
    return prompt


def _parse_days(raw: str, days: int) -> list[list[str]] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    start = text.find("{")
    if start < 0:
        return None
    try:
        payload = json.JSONDecoder().raw_decode(text[start:])[0]
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
        return None
    chosen = payload["days"]
    if len(chosen) != days:
        return None
    result = []
    for day in chosen:
        if not isinstance(day, list):
            return None
        result.append([str(item) for item in day if str(item).strip()])
    return result


def _reorder(ids: list[str], spot_map: dict[str, Spot]) -> list[str]:
    """同一天内按就近原则串行，减少折返（模型只负责选，顺序交给代码）。"""
    remaining = [spot_id for spot_id in ids if spot_id in spot_map]
    if len(remaining) <= 1:
        return remaining
    ordered = [remaining.pop(0)]
    while remaining:
        last = spot_map[ordered[-1]]
        nearest = min(remaining, key=lambda spot_id: simulator.distance_km(last, spot_map[spot_id]))
        remaining.remove(nearest)
        ordered.append(nearest)
    return ordered


def _build_itinerary(req: Requirements, chosen: list[list[str]], spot_map: dict[str, Spot],
                     dates: list[date]) -> Itinerary:
    days = []
    for index, ids in enumerate(chosen):
        ordered = _reorder(ids, spot_map)
        days.append(DayPlan(
            day_index=index + 1,
            date=dates[index],
            legs=[Leg(spot_id=spot_id) for spot_id in ordered],
            start=simulator.parse_time(req.start_time),
            end_limit=simulator.parse_time(req.end_time),
        ))
    return Itinerary(city=req.city, days=days, budget=req.budget)


def _from_ids(req: Requirements, days_ids: list[list[str]], template_days: list[DayPlan]) -> Itinerary:
    return Itinerary(city=req.city, budget=req.budget, days=[
        DayPlan(day_index=day.day_index, date=day.date,
                legs=[Leg(spot_id=spot_id) for spot_id in ids],
                start=day.start, end_limit=day.end_limit)
        for day, ids in zip(template_days, days_ids)
    ])


FILL_UNTIL_MIN = 16 * 60  # 结束早于 16:00 就尝试补排


def _fill_sparse_days(itinerary: Itinerary, report: simulator.Report, req: Requirements,
                      spot_map: dict[str, Spot], travel=None) -> tuple[Itinerary, simulator.Report, int]:
    """模型常常半天就收工。在时间窗允许的前提下，用代码补排候选景点。

    这是"模型决策 + 代码兜底"的具体体现：模型负责选哪几个方向，
    代码负责把空着的下午补满，并且补完必须重新通过校验。
    """
    used = set(itinerary.spot_ids)
    ids = [[leg.spot_id for leg in day.legs] for day in itinerary.days]
    filled = 0

    for index, day in enumerate(itinerary.days):
        current_end = simulator.minutes_of(report.results[index].end_time)
        if current_end >= FILL_UNTIL_MIN:
            continue
        candidates = [spot for spot in spot_map.values()
                      if spot.id not in used and open_on(spot, day.date)]
        candidates.sort(key=lambda spot: (-planner.preference_score(spot, req.prefer), spot.ticket))
        for spot in candidates:
            if current_end >= FILL_UNTIL_MIN:
                break
            trial_ids = [list(item) for item in ids]
            trial_ids[index] = _reorder(trial_ids[index] + [spot.id], spot_map)
            trial = _from_ids(req, trial_ids, itinerary.days)
            trial_report = simulator.simulate(trial, spot_map, travel=travel)
            if not trial_report.ok or trial_report.warnings:
                continue
            new_end = simulator.minutes_of(trial_report.results[index].end_time)
            if new_end > simulator.minutes_of(day.end_limit):
                continue
            if new_end > current_end:
                ids, itinerary, report = trial_ids, trial, trial_report
                current_end = new_end
                used.add(spot.id)
                filled += 1
    return itinerary, report, filled


def _write_notes(req: Requirements, itinerary: Itinerary, report: simulator.Report,
                 model: str | None) -> str:
    """让模型写两句行程说明；失败就留空，不影响交付。"""
    names = [leg["name"] for result in report.results for leg in result.legs]
    prompt = (
        "用两句话总结这份行程的安排思路，只依据给定信息，不要新增景点或编造事实。\n"
        f"需求：{req.describe()}\n景点顺序：{'、'.join(names)}\n"
    )
    try:
        text = llm.chat(prompt, model=model, json_mode=False, temperature=0.4).strip()
    except llm.LLMError:
        return ""
    return text.replace("\n", " ")[:200]


def plan_fallback(req: Requirements, spots: list[Spot], start_date: date):
    """外部直接调用确定性规划（离线演示/测试用）。"""
    itinerary, report = planner.plan(
        city=req.city, spots=spots, days=req.days, budget=req.budget,
        prefer=req.prefer, start_date=start_date,
        start_time=req.start_time, end_limit=req.end_time,
    )
    return itinerary, report, PlanTrace(source="规则（直接调用）")
