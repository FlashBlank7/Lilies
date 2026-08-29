"""体检说"都正常"之前，得看过调度器还在不在。

回归背景（2026-08-29）：管家的 health_report 只看工作流本身，
最后还给模型一句「problems 为空表示所有已发布工作流都正常」。
可调度器刚死、还没到任何定时点的时候，"有定时却没跑起来"仍然是 0——
报告全绿，而所有定时任务其实都不会再开火了。

客户端 doctor 同一天修过一模一样的形状：没查调度器却宣布「一切正常」。
"""
import unittest

from agent_platform.assistant_agent import _scheduler_words


class SchedulerWordsTest(unittest.TestCase):
    def test_a_dead_scheduler_says_what_it_means(self):
        words = _scheduler_words({"alive": False, "seconds_since_tick": 900})
        self.assertIn("停了", words)
        self.assertIn("不会再开火", words)
        self.assertIn("900", words)

    def test_a_live_scheduler_says_when_it_last_ticked(self):
        self.assertIn("3 秒前", _scheduler_words({"alive": True, "seconds_since_tick": 3}))

    def test_no_scheduler_at_all_is_not_silently_ok(self):
        words = _scheduler_words(None)
        self.assertIn("不会自动跑", words)

    def test_no_internal_words_reach_the_model(self):
        """alive / seconds_since_tick 这些词递给模型，它会原样念出来。"""
        for health in ({"alive": False, "seconds_since_tick": 10},
                       {"alive": True, "seconds_since_tick": 1},
                       {"alive": True}, None):
            words = _scheduler_words(health)
            self.assertNotIn("alive", words)
            self.assertNotIn("seconds_since_tick", words)

    def test_a_missing_tick_time_does_not_crash(self):
        self.assertTrue(_scheduler_words({"alive": True}).strip())
        self.assertTrue(_scheduler_words({"alive": False}).strip())


class HealthReportCarriesItTest(unittest.IsolatedAsyncioTestCase):
    """并进去了没有——接线才算数。"""

    async def test_the_tool_result_includes_the_scheduler(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from agent_platform.assistant_agent import WorkflowConcierge

        agent = WorkflowConcierge.__new__(WorkflowConcierge)
        agent.services = SimpleNamespace(
            scheduler=SimpleNamespace(
                health=lambda: {"alive": False, "seconds_since_tick": 600}))

        with patch("agent_platform.overview.build_health",
                   AsyncMock(return_value={"items": [], "counts": {"ok": 3}})):
            result = await agent._exec("health_report", {}, {})

        self.assertIn("定时调度", result)
        self.assertIn("停了", result["定时调度"])
        self.assertIn("定时", result["note"], "note 还在说「都正常」，会把用户带偏")


if __name__ == "__main__":
    unittest.main()


class TruncationIsDeclaredTest(unittest.IsolatedAsyncioTestCase):
    """只列前 10 个就要说出来。

    同一个病 2026-08-29 一天里在三个工具上各中一次：
    recent_runs（翻 10 条数出 2 次，实际 5 次）、
    list_workflows（比最近 5 条就断言"跑得最多"）、
    recent_builds（数了看到的 25 个，实际 75 个）。
    规律是：**给一页数据、不说这是一页，模型就会把它当全部。**
    """

    async def _report(self, problem_count: int) -> dict:
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from agent_platform.assistant_agent import WorkflowConcierge

        agent = WorkflowConcierge.__new__(WorkflowConcierge)
        agent.services = SimpleNamespace(scheduler=SimpleNamespace(
            health=lambda: {"alive": True, "seconds_since_tick": 1}))
        items = [{"workflow": f"坏的{i}", "state": "broken", "reason": "全失败",
                  "application_id": f"a{i}", "runs": 3, "succeeded": 0}
                 for i in range(problem_count)]
        with patch("agent_platform.overview.build_health",
                   AsyncMock(return_value={"items": items,
                                           "counts": {"ok": 0, "broken": problem_count}})):
            return await agent._exec("health_report", {}, {})

    async def test_a_long_list_says_how_many_were_left_out(self):
        result = await self._report(25)
        self.assertEqual(len(result["problems"]), 10)
        key = "problems 只列了前 10 个"
        self.assertIn(key, result)
        self.assertIn("25", result[key], "没说实际有多少个")

    async def test_a_short_list_says_nothing_extra(self):
        """没截断就别加噪音——加了会让人以为还有更多。"""
        result = await self._report(3)
        self.assertEqual(len(result["problems"]), 3)
        self.assertNotIn("problems 只列了前 10 个", result)

    async def test_exactly_ten_is_not_flagged(self):
        result = await self._report(10)
        self.assertNotIn("problems 只列了前 10 个", result)


class AliveIsNotTheSameAsAllFiringTest(unittest.TestCase):
    """「调度器活着」和「所有定时都开火了」是两回事。

    2026-08-29 补上 per-application 保护之后，一个坏工作流不再拖垮全体——
    但它自己每轮都被跳过，而调度器照样心跳、照样报活着。
    光说"调度器正常"等于替那个定时任务瞒着：它在无声地不跑。
    """

    def test_a_skipped_workflow_shows_up_even_though_the_loop_is_alive(self):
        words = _scheduler_words({
            "alive": True, "seconds_since_tick": 3,
            "last_error": "这一轮跳过了 「服务器GPU日报」：KeyError: 版本查不到"})
        self.assertIn("没能开火", words)
        self.assertIn("服务器GPU日报", words)

    def test_a_clean_scheduler_does_not_cry_wolf(self):
        words = _scheduler_words({"alive": True, "seconds_since_tick": 3, "last_error": ""})
        self.assertNotIn("没能开火", words)
        self.assertIn("正常", words)

    def test_a_dead_scheduler_still_leads_with_being_dead(self):
        """停了就是停了——别被"某某跳过"抢了头条，那是更小的问题。"""
        words = _scheduler_words({
            "alive": False, "seconds_since_tick": 900, "last_error": "这一轮跳过了 「甲」"})
        self.assertIn("停了", words)
