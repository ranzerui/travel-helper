"""规划器：确定性贪心 + 校验回炉循环。

项目原则：模型可以换（哪怕换成 8B 本地模型），但"输出必须通过 simulate()"这条不变。
所以这里先给一个纯代码的基线规划器，保证演示不依赖模型；
接模型时把它放在 planner 之上，用同样的回炉循环兜住。
"""
from datetime import date, time, timedelta

from . import simulator
from .data import open_on
from .models import DayPlan, Itinerary, Leg, Spot


def preference_score(spot: Spot, prefer: str) -> float:
    score = 0.0
    if prefer and spot.category == prefer:
        score += 3.0
    if spot.ticket == 0 and (spot.verified or spot.ticket_prices):
        score += 1.0
    if spot.duration_min <= 90:
        score += 0.5
    return score


def _fill_days(city, spots, days, budget, prefer, start_date, start_time, end_limit, dropped,
               travel_provider=None):
    """按"每天景点数尽量均衡"分配：每个景点放进当前最空、且塞得下的那天。"""
    candidates = [spot for spot in spots if spot.id not in dropped]
    candidates.sort(key=lambda spot: (-preference_score(spot, prefer), not (spot.verified or spot.ticket_prices), spot.ticket, spot.duration_min))
    limit = simulator.minutes_of(end_limit)
    states = []
    for index in range(days):
        states.append({
            "plan": DayPlan(day_index=index + 1, date=start_date + timedelta(days=index),
                            start=start_time, end_limit=end_limit),
            "clock": simulator.minutes_of(start_time),
            "previous": None,
        })

    used: set[str] = set()
    for spot in candidates:
        if spot.id in used:
            continue
        for state in sorted(states, key=lambda item: len(item["plan"].legs)):
            current = state["plan"].date
            if not open_on(spot, current):
                continue
            if state["previous"] is None:
                travel = 0
            elif travel_provider is not None:
                travel, _ = travel_provider.minutes(state["previous"], spot)
            else:
                travel = simulator.travel_minutes(state["previous"], spot)
            open_at = simulator.minutes_of(simulator.parse_time(spot.open_time))
            close_at = simulator.minutes_of(simulator.parse_time(spot.close_time))
            arrive = max(state["clock"] + travel, open_at)
            depart = arrive + spot.duration_min
            if depart > min(close_at, limit):
                continue
            state["plan"].legs.append(Leg(spot_id=spot.id))
            state["clock"] = depart
            state["previous"] = spot
            used.add(spot.id)
            break

    return Itinerary(city=city, days=[state["plan"] for state in states], budget=budget)


def plan(city, spots, days, budget, prefer="", start_date=None,
         start_time="09:00", end_limit="20:00", max_rounds=6, travel=None):
    """生成行程并校验；不合格就针对性去掉问题景点后重排。"""
    start_date = start_date or date.today()
    start = simulator.parse_time(start_time)
    limit = simulator.parse_time(end_limit)
    spot_map = {spot.id: spot for spot in spots}
    dropped: set[str] = set()
    itinerary = None
    report = None

    for _ in range(max_rounds):
        itinerary = _fill_days(city, spots, days, budget, prefer, start_date, start, limit, dropped,
                               travel_provider=travel)
        report = simulator.simulate(itinerary, spot_map, travel=travel)
        if report.ok:
            return itinerary, report

        bad_names = {issue.spot for issue in report.errors}
        to_drop = {spot.id for spot in spots if spot.name in bad_names and spot.id not in dropped}
        if not to_drop:
            # 预算类错误没有具体景点：去掉当前最贵的收费景点再试
            paid = [spot for spot in spots if spot.ticket > 0 and spot.id not in dropped]
            if not paid:
                break
            to_drop = {max(paid, key=lambda spot: spot.ticket).id}
        dropped |= to_drop

    return itinerary, report
