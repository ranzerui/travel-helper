"""行程模拟器：纯代码推演时间线并校验。

这是整个项目的核心：
- 不调模型；
- 输入任意行程（包括模型瞎排的），输出可行性问题清单；
- 规划器拿到问题后针对性调整（见 planner.py 的重试循环）。

校验项：
1. 闭馆日（固定闭馆）
2. 到得比开门早 → 记录等待时间（不是错误）
3. 游览超出闭馆时间 → 错误
4. 一天结束晚于上限 → 提示
5. 同一景点重复安排 → 错误
6. 预算超支 → 错误
"""
import math
from dataclasses import dataclass, field
from datetime import date, time

from .models import DayPlan, Itinerary, Spot
from . import pricing

AVERAGE_SPEED_KMH = 12.0   # 市内公交+步行平均速度，保守取值
MIN_TRAVEL_MIN = 5


def parse_time(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def minutes_of(value: time) -> int:
    return value.hour * 60 + value.minute


def time_of(total_minutes: int) -> time:
    total_minutes = max(0, min(total_minutes, 24 * 60 - 1))
    return time(total_minutes // 60, total_minutes % 60)


def distance_km(first: Spot, second: Spot) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(first.lat), math.radians(second.lat)
    d_phi = phi2 - phi1
    d_lambda = math.radians(second.lon - first.lon)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def travel_minutes(first: Spot, second: Spot) -> int:
    estimate = distance_km(first, second) / AVERAGE_SPEED_KMH * 60
    return max(MIN_TRAVEL_MIN, int(round(estimate)))


@dataclass
class Issue:
    level: str        # error | warning
    day: int
    spot: str
    message: str


@dataclass
class DayResult:
    day_index: int
    date: date
    legs: list[dict] = field(default_factory=list)   # 供报告渲染的扁平结构
    end_time: time = time(0, 0)


@dataclass
class Report:
    city: str
    results: list[DayResult]
    issues: list[Issue]
    ticket_total: float
    meal_total: float
    transport_total: float
    budget: float
    warnings: list[Issue] = field(default_factory=list)
    browser_review: dict = field(default_factory=dict)
    unknown_tickets: list[str] = field(default_factory=list)

    @property
    def budget_complete(self) -> bool:
        return not self.unknown_tickets

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def total_cost(self) -> float:
        return self.ticket_total + self.meal_total + self.transport_total

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def budget_left(self) -> float:
        return self.budget - self.total_cost

    def summary_lines(self) -> list[str]:
        lines = [f"校验结果：{'✅ 通过' if self.ok else '❌ 未通过'}（{len(self.errors)} 个错误，{len(self.warnings)} 个提示）"]
        for issue in self.issues:
            lines.append(f"  [错误] Day{issue.day} {issue.spot}：{issue.message}")
        for issue in self.warnings:
            lines.append(f"  [提示] Day{issue.day} {issue.spot}：{issue.message}")
        if not self.budget_complete:
            lines[0] = lines[0].replace("校验结果", "时间线及已知费用校验")
            lines.append("预算尚未核实完整：" + "、".join(self.unknown_tickets) + "；当前费用为小计，不能确认结余或预算可行")
        return lines


def simulate(itinerary: Itinerary, spots: dict[str, Spot], travel=None) -> Report:
    """travel: 可选的路段时长提供者（routes.RouteProvider.minutes），
    不传就用直线估算。返回值里会写明每段是"估算"还是"高德"。"""
    travel_provider = travel
    issues: list[Issue] = []
    warnings: list[Issue] = []
    results: list[DayResult] = []
    ticket_total = 0.0
    seen: dict[str, int] = {}
    unknown_tickets = []

    for day in itinerary.days:
        clock = minutes_of(day.start)
        end_limit = minutes_of(day.end_limit)
        previous: Spot | None = None
        day_result = DayResult(day_index=day.day_index, date=day.date)

        for leg in day.legs:
            spot = spots.get(leg.spot_id)
            if spot is None:
                issues.append(Issue("error", day.day_index, leg.spot_id, "数据层没有这个景点"))
                continue
            if spot.id in seen:
                issues.append(Issue("error", day.day_index, spot.name, f"重复安排（已在 Day{seen[spot.id]} 去过）"))
                continue
            seen[spot.id] = day.day_index

            if not _open_on(spot, day.date):
                reason = "官网公告临时闭馆" if day.date.isoformat() in spot.closed_dates else "当天闭馆"
                issues.append(Issue("error", day.day_index, spot.name, f"{reason}（{_weekday_name(day.date)}）"))
                continue

            if previous is None:
                travel, travel_source = 0, "起点"
            elif travel_provider is not None:
                travel, travel_source = travel_provider.minutes(previous, spot)
            else:
                travel, travel_source = travel_minutes(previous, spot), "估算"
            arrive = clock + travel
            open_at = minutes_of(parse_time(spot.open_time))
            close_at = minutes_of(parse_time(spot.close_time))

            wait = 0
            if arrive < open_at:
                wait = open_at - arrive
                arrive = open_at
            if arrive >= close_at:
                issues.append(Issue("error", day.day_index, spot.name, f"{time_of(arrive).strftime('%H:%M')} 到达已闭馆（{spot.close_time} 关门）"))
                continue

            depart = arrive + spot.duration_min
            if depart > close_at:
                issues.append(Issue("error", day.day_index, spot.name, f"游览需 {spot.duration_min} 分钟，{spot.close_time} 闭馆时间不够"))
                depart = close_at
            if depart > end_limit:
                warnings.append(Issue("warning", day.day_index, spot.name, f"结束时间 {time_of(depart).strftime('%H:%M')} 超过当天上限 {day.end_limit.strftime('%H:%M')}"))

            leg.travel_min = travel
            leg.arrive = time_of(arrive)
            leg.depart = time_of(depart)
            leg.wait_min = wait
            ticket = pricing.ticket_details(spot, day.date)
            if ticket["status"] in {"unknown", "reference", "search"}:
                unknown_tickets.append(spot.name)
                message = ("门票采用搜索摘要参考价，尚未经官网核实，预算仍为估算" if ticket["status"] == "search"
                           else "门票待核实，未确认预算；未知价格不代表免费")
                warnings.append(Issue("warning", day.day_index, spot.name, message))
            day_result.legs.append({
                "name": spot.name,
                "category": spot.category,
                "travel_min": travel,
                "travel_source": travel_source,
                "arrive": leg.arrive,
                "depart": leg.depart,
                "wait_min": wait,
                "ticket": ticket["amount"],
                "ticket_status": ticket["status"],
                "ticket_evidence": ticket,
                "open_time": spot.open_time,
                "close_time": spot.close_time,
                "source_name": spot.source_name,
                "source_url": spot.source_url,
                "captured_at": spot.captured_at,
                "verified": spot.verified,
                "desc": spot.desc,
            })
            ticket_total += ticket["amount"] or 0.0
            clock = depart
            previous = spot

        day_result.end_time = time_of(clock)
        results.append(day_result)

    meal_total = itinerary.meals_per_day * len(itinerary.days)
    transport_total = itinerary.transport_per_day * len(itinerary.days)
    report = Report(
        city=itinerary.city,
        results=results,
        issues=issues,
        warnings=warnings,
        ticket_total=ticket_total,
        meal_total=meal_total,
        transport_total=transport_total,
        budget=itinerary.budget,
        unknown_tickets=unknown_tickets,
    )
    if report.total_cost > itinerary.budget + 1e-6:
        issues.append(Issue(
            "error", 0, "预算",
            f"已知/参考费用加餐饮交通估算 {report.total_cost:.2f} 元超过预算 {itinerary.budget:.2f} 元（超 {report.total_cost - itinerary.budget:.2f} 元）；不会提高用户预算，当前餐饮按 {itinerary.meals_per_day:.0f} 元/天、市内交通按 {itinerary.transport_per_day:.0f} 元/天估算",
        ))
    return report


def _open_on(spot: Spot, day: date) -> bool:
    return day.weekday() not in spot.closed_days and day.isoformat() not in spot.closed_dates


def _weekday_name(day: date) -> str:
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return names[day.weekday()]
