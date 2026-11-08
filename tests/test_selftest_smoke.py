"""自检脚本的回归测试：跑离线子集，保证核心不变量不被后续改动破坏。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from travel_planner import selftest


class SelfTestSmoke(unittest.TestCase):
    def test_offline_subset_has_no_failures(self):
        call = selftest.make_inproc()
        failures, results = [], []

        fresh = [case for case in selftest.FRESH_CASES
                 if case[0] in {"成都两天，预算1200，喜欢人文",
                                "预算3000，五天，哈尔滨",
                                "桃3天，预算2000，去风景好的地方",
                                "3天西安，预算1800，喜欢人文"}]
        saved = selftest.FRESH_CASES
        try:
            selftest.FRESH_CASES = fresh
            selftest.check_fresh(call, "", "offline", failures, results)
            selftest.check_edges(call, "", "offline", failures, results)
            start, steps = selftest.REFINE_CHAINS[0]
            selftest.REFINE_CHAINS = [(start, steps[:2])]
            selftest.check_refine(call, "", "offline", failures, results)
        finally:
            selftest.FRESH_CASES = saved
            selftest.REFINE_CHAINS = selftest.REFINE_CHAINS
        self.assertEqual(failures, [], "\n".join(failures))
        self.assertGreaterEqual(len(results), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
