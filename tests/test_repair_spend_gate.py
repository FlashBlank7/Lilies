"""自动花钱的那道闸不许被顺手拆掉。

repair_workflow 是全系统唯一会自动开构建（= 自动花钱）的路径：
模型没说要修什么时，必须去体检取原因，且只认「确实坏了且说得出原因」。

这条测试的由来是一次自伤：给 instruction 加「并上业主原话」时，
空指示会被业主的上一句话填满，于是径直闯过体检门。
不是假想的风险——已经写出来过一次。
"""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_platform.assistant_agent import WorkflowConcierge


def _agent(health_state, last_error="", builds=None):
    services = MagicMock()
    services.workflow_store.create_build = AsyncMock()
    # 默认"手上没有在跑的构建"。这一项是 2026-08-29 加并发闸时补的：
    # 不给的话 list_builds 是个 MagicMock，await 不了。
    services.workflow_store.list_builds = AsyncMock(return_value=list(builds or []))
    agent = WorkflowConcierge(services, MagicMock())
    agent._owner_words = "这个日报不太对，你看看吧"     # 像指示但不是指示
    agent._resolve_app = AsyncMock(return_value={"id": "a1", "name": "日报"})
    report = {"items": [{"application_id": "a1", "state": health_state,
                         "last_error": last_error, "reason": f"近7天全败：{last_error}"}]}
    return agent, services, report


class RepairSpendGateTest(unittest.IsolatedAsyncioTestCase):
    async def _call(self, agent, report, args):
        with patch("agent_platform.overview.build_health",
                   AsyncMock(return_value=report)):
            return await agent._exec("repair_workflow", args, {})

    async def test_healthy_workflow_is_refused_even_with_owner_chatter(self):
        agent, services, report = _agent("ok")
        result = await self._call(agent, report, {"name_or_id": "日报"})
        self.assertIn("error", result)
        services.workflow_store.create_build.assert_not_awaited()

    async def test_broken_without_a_reason_is_still_refused(self):
        agent, services, report = _agent("broken", last_error="")
        result = await self._call(agent, report, {"name_or_id": "日报"})
        self.assertIn("error", result)
        services.workflow_store.create_build.assert_not_awaited()

    async def test_broken_with_a_reason_may_spend(self):
        agent, services, report = _agent("broken", last_error="缺少必填输入「text」")
        result = await self._call(agent, report, {"name_or_id": "日报"})
        self.assertTrue(result.get("repairing"))
        services.workflow_store.create_build.assert_awaited_once()

    async def test_explicit_instruction_keeps_the_owner_words(self):
        agent, services, report = _agent("ok")
        result = await self._call(
            agent, report, {"name_or_id": "日报", "instruction": "把日期格式改成年月日"})
        self.assertTrue(result.get("repairing"))
        self.assertIn("把日期格式改成年月日", result["instruction"])
        self.assertIn("这个日报不太对", result["instruction"])   # 原话带上了


class OneRepairAtATimeTest(unittest.IsolatedAsyncioTestCase):
    """已经在修了就别再开一轮——钱和草稿都只有一份。

    2026-08-29 读代码发现：这条是全系统唯一自动花钱的路径，
    而它**没有**并发闸。业主连说两句「修一下」就是两个构建
    同时改同一份草稿：钱花两份，后完成的那个把前一个的成果直接盖掉，
    无声无息。

    业主页那个「一键返修」早就挡住了（409「正在搭建中，等这轮结束再返修」），
    管家这条没挡——同一个闸只装了一个出口。
    """

    async def _call(self, agent, report, args):
        with patch("agent_platform.overview.build_health",
                   AsyncMock(return_value=report)):
            return await agent._exec("repair_workflow", args, {})

    async def test_a_build_already_running_blocks_a_second_one(self):
        for status in ("queued", "building"):
            agent, services, report = _agent(
                "broken", last_error="缺少必填输入「text」",
                builds=[{"id": "b-1", "status": status}])
            result = await self._call(agent, report, {"name_or_id": "日报"})
            self.assertIn("error", result, status)
            self.assertIn("正在搭建中", result["error"])
            self.assertEqual(result.get("build_id"), "b-1")
            services.workflow_store.create_build.assert_not_awaited()

    async def test_it_blocks_even_when_an_instruction_was_given(self):
        """带指示那条路也要挡——它绕开了体检门，别再绕开这道。"""
        agent, services, report = _agent(
            "ok", builds=[{"id": "b-1", "status": "building"}])
        result = await self._call(
            agent, report, {"name_or_id": "日报", "instruction": "把日期改成年月日"})
        self.assertIn("error", result)
        services.workflow_store.create_build.assert_not_awaited()

    async def test_a_finished_build_does_not_block(self):
        """搭完的、卡住的、放弃的都不算"在跑"——挡过头就没法修了。"""
        for status in ("published", "needs_attention", "cancelled", "failed"):
            agent, services, report = _agent(
                "broken", last_error="缺少必填输入「text」",
                builds=[{"id": "b-old", "status": status}])
            result = await self._call(agent, report, {"name_or_id": "日报"})
            self.assertTrue(result.get("repairing"), f"{status} 不该挡住返修")
            services.workflow_store.create_build.assert_awaited_once()

    async def test_it_looks_past_the_newest_build(self):
        """最近一条已经结束、但更早那条还挂在 building——照样要挡。

        业主页那边只看 builds[0]；这里逐条看，因为漏判的代价是花钱。
        """
        agent, services, report = _agent(
            "broken", last_error="缺少必填输入「text」",
            builds=[{"id": "b-new", "status": "needs_attention"},
                    {"id": "b-old", "status": "building"}])
        result = await self._call(agent, report, {"name_or_id": "日报"})
        self.assertIn("error", result)
        services.workflow_store.create_build.assert_not_awaited()

