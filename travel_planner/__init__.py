"""带可行性校验的旅游行程生成器。

模块划分：
- models:    数据结构（景点、行程、天）
- data:      数据层（当前为本地 JSON，可替换为 OSM / 官网抓取）
- simulator: 行程模拟器（纯代码校验：闭馆日、时间窗、预算、重复）
- planner:   规划器（贪心基线 + 校验回炉循环；模型可替换）
- report:    交付层（Markdown 报告 + .ics 日历）
"""

__version__ = "0.1.0"

__all__ = ["models", "data", "simulator", "planner", "report"]
