"""旅游攻略生成器：数据结构。

设计原则：事实（开放时间、票价、坐标、来源）全部来自数据层，模型只负责编排顺序。
"""
from dataclasses import dataclass, field
from datetime import date, time


@dataclass(frozen=True)
class Spot:
    """一个景点。字段全部来自数据层，禁止模型编造。"""

    id: str
    name: str
    city: str
    category: str          # 人文 / 自然 / 美食 / 亲子
    lat: float
    lon: float
    open_time: str         # "09:00"
    close_time: str        # "18:00"
    duration_min: int      # 建议游览时长
    ticket: float = 0.0    # 门票（元）
    closed_days: tuple[int, ...] = ()   # 0=周一 ... 6=周日
    desc: str = ""
    source_name: str = ""
    source_url: str = ""
    captured_at: str = ""  # 抓取/核实日期，用于报告溯源
    verified: bool = False  # 开放时间/票价是否人工或官方核实过；抓取生成的一定是 False
    closed_dates: tuple[str, ...] = ()
    ticket_prices: tuple[dict, ...] = ()


@dataclass
class Leg:
    """一天中的一段安排：去哪、几点到、几点走、路上花多久。"""

    spot_id: str
    travel_min: int = 0
    arrive: time | None = None
    depart: time | None = None
    wait_min: int = 0


@dataclass
class DayPlan:
    day_index: int
    date: date
    legs: list[Leg] = field(default_factory=list)
    start: time = time(9, 0)
    end_limit: time = time(20, 0)


@dataclass
class Itinerary:
    city: str
    days: list[DayPlan]
    budget: float
    meals_per_day: float = 120.0       # 餐饮估算（元/天）
    transport_per_day: float = 30.0    # 市内交通估算（元/天）

    @property
    def spot_ids(self) -> list[str]:
        return [leg.spot_id for day in self.days for leg in day.legs]
