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


def _agent(health_state, last_error=""):
    services = MagicMock()
    services.workflow_store.create_build = AsyncMock()
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
