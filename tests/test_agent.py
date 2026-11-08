"""智能体规划器与需求解析的离线测试：用假模型替换 Ollama。"""
import os
import sys
import unittest
from pathlib import Path
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CITY_FIXTURE = Path(__file__).with_name("fixtures") / "成都.json"



from travel_planner import agent_planner, data, intent, llm, report

MONDAY = date(2026, 11, 2)
TUESDAY = date(2026, 11, 3)


class FakeModel:
    """按顺序返回预设内容，记录调用次数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, prompt, model=None, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        if not self.responses:
            raise llm.LLMError("假模型响应已用尽")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class AgentPlannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.spots = data.load_file(CITY_FIXTURE)

    def setUp(self):
        self.req = intent.Requirements(city="成都", days=1, budget=1500, prefer="人文")
        self._orig_available = llm.available
        self._orig_chat = llm.chat
        llm.available = lambda *args, **kwargs: True

    def tearDown(self):
        llm.available = self._orig_available
        llm.chat = self._orig_chat

    def test_retries_after_validation_error(self):
        """第一轮排到周一闭馆的博物馆 → 校验拦下 → 第二轮改对。"""
        bad = '{"days": [["sichuan_museum"]]}'
        good = '{"days": [["dufu_caotang"]]}'
        model = FakeModel([bad, good, "第一天看杜甫草堂，上午人少。"])
        llm.chat = model

        itinerary, result, trace = agent_planner.plan_with_agent(
            self.req, self.spots, MONDAY, use_llm=True, max_rounds=3)
        # 第一轮必错，第二轮通过（说明意见回灌生效）
        self.assertGreaterEqual(len(trace.rounds), 2)
        self.assertTrue(result.ok, result.summary_lines())
        self.assertIn("模型", trace.source)
        self.assertTrue(any("闭馆" in error for error in trace.rounds[0]["errors"]))

    def test_falls_back_when_model_returns_garbage(self):
        model = FakeModel(["这不是JSON", "still not json"])
        llm.chat = model
        itinerary, result, trace = agent_planner.plan_with_agent(
            self.req, self.spots, TUESDAY, use_llm=True, max_rounds=2)
        self.assertTrue(result.ok, result.summary_lines())
        self.assertIn("回退", trace.source)
        self.assertGreaterEqual(len(trace.rounds), 2)

    def test_falls_back_when_model_unreachable(self):
        def boom(*args, **kwargs):
            raise llm.LLMError("拒绝连接")

        llm.available = lambda *args, **kwargs: False
        itinerary, result, trace = agent_planner.plan_with_agent(
            self.req, self.spots, TUESDAY, use_llm=True, max_rounds=2)
        self.assertTrue(result.ok)
        self.assertIn("Ollama 未运行", trace.source)

    def test_notes_are_written_when_available(self):
        good = '{"days": [["dufu_caotang", "kuanzhai"]]}'
        model = FakeModel([good, "这条线以市中心人文景点为主，步行为主。"])
        llm.chat = model

        itinerary, result, trace = agent_planner.plan_with_agent(
            self.req, self.spots, TUESDAY, use_llm=True, max_rounds=1)
        self.assertTrue(result.ok, result.summary_lines())
        self.assertIn("人文", trace.notes)
        # 第二次调用是写说明，不应该要求 JSON 模式
        self.assertFalse(model.calls[1]["kwargs"].get("json_mode", True))

    def test_sparse_plan_is_filled_by_code(self):
        """模型只排一个景点就收工，solver 按时间窗补排，补完仍需通过校验。"""
        llm.chat = FakeModel(['{"days": [["dufu_caotang"]]}', "以人文为主的一条线。"])
        itinerary, result, trace = agent_planner.plan_with_agent(
            self.req, self.spots, TUESDAY, use_llm=True, max_rounds=1)
        self.assertTrue(result.ok, result.summary_lines())
        self.assertGreater(len(itinerary.days[0].legs), 1)
        self.assertGreaterEqual(trace.filled, 1)

    def test_must_see_is_enforced(self):
        """必去景点没排进去，第一轮不给过，第二轮补上。"""
        req = intent.Requirements(city="成都", days=1, budget=1500, prefer="人文",
                                  must_see=["武侯祠"])
        llm.chat = FakeModel(['{"days": [["dufu_caotang"]]}', '{"days": [["dufu_caotang", "wuhouci"]]}', "说明"])
        itinerary, result, trace = agent_planner.plan_with_agent(
            req, self.spots, TUESDAY, use_llm=True, max_rounds=3)
        names = [leg["name"] for item in result.results for leg in item.legs]
        self.assertIn("武侯祠", names)
        self.assertTrue(any("必去" in error for error in trace.rounds[0]["errors"]))

    def test_unknown_must_see_is_dropped(self):
        """模型可能编出候选里没有的景点（比如三星堆），不应阻塞规划。"""
        req = intent.Requirements(city="成都", days=1, budget=1500, must_see=["三星堆"])
        llm.chat = FakeModel(['{"days": [["dufu_caotang", "wuhouci"]]}', "说明"])
        itinerary, result, trace = agent_planner.plan_with_agent(
            req, self.spots, TUESDAY, use_llm=True, max_rounds=2)
        self.assertTrue(result.ok, result.summary_lines())
        self.assertFalse(any("必去" in error for item in trace.rounds for error in item["errors"]))

    def test_offline_mode_never_calls_model(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("离线模式不应调用模型")

        llm.chat = forbidden
        itinerary, result, trace = agent_planner.plan_with_agent(
            self.req, self.spots, TUESDAY, use_llm=False)
        self.assertTrue(result.ok, result.summary_lines())
        self.assertIn("离线", trace.source)

    def test_trace_written_into_report(self):
        itinerary, result, trace = agent_planner.plan_with_agent(
            self.req, self.spots, TUESDAY, use_llm=False)
        text = report.to_markdown(itinerary, result, trace=trace)
        self.assertIn("智能体决策记录", text)


class IntentTest(unittest.TestCase):
    def setUp(self):
        self._orig_chat = llm.chat
        self._orig_available = llm.available

    def tearDown(self):
        llm.chat = self._orig_chat
        llm.available = self._orig_available

    def test_rule_parsing(self):
        req = intent.parse_requirements("成都三天，预算1500，喜欢人文，节奏轻松，想少走路", use_llm=False)
        self.assertEqual(req.days, 3)
        self.assertEqual(req.budget, 1500)
        self.assertEqual(req.prefer, "人文")
        self.assertEqual(req.pace, "轻松")
        self.assertEqual(req.source, "规则")

    def test_budget_and_days_variants(self):
        req = intent.parse_requirements("去成都玩5天，大概2000块", use_llm=False)
        self.assertEqual(req.days, 5)
        self.assertEqual(req.budget, 2000)

    def test_chinese_numeral_days(self):
        """'两天' 不是数字，规则解析也要认。"""
        self.assertEqual(intent.parse_requirements("成都两天，预算800", use_llm=False).days, 2)
        self.assertEqual(intent.parse_requirements("成都三天，喜欢人文", use_llm=False).days, 3)
        self.assertEqual(intent.parse_requirements("成都十二天", use_llm=False).days, 12)

    def test_llm_parsing(self):
        llm.chat = FakeModel(['{"city":"成都","days":2,"budget":800,"prefer":"自然","pace":"轻松","must_see":[]}'])
        req = intent.parse_requirements("想去看熊猫，两天，八百块，别太赶")
        self.assertEqual(req.days, 2)
        self.assertEqual(req.budget, 800)
        self.assertEqual(req.prefer, "自然")
        self.assertEqual(req.source, "模型")

    def test_llm_failure_falls_back_to_rules(self):
        llm.chat = FakeModel([llm.LLMError("超时")])
        req = intent.parse_requirements("成都4天，预算3000")
        self.assertEqual(req.days, 4)
        self.assertEqual(req.budget, 3000)
        self.assertEqual(req.source, "规则")


if __name__ == "__main__":
    unittest.main(verbosity=2)
