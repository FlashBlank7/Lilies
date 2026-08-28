"""一个工具参数写错，不该把整轮对话打成 500。

回归背景（2026-08-28 真机）：走招牌路径造工作流，第二句
「刚才那个建好了吗」让模型用一个不存在的构建号调了 build_status，
get_build 抛 KeyError 一路冒到 HTTP 层——整个请求 500，业主这一轮全没了。

工具报错是数据不是崩溃：模型收到「找不到这个构建」能自己去查或如实转告，
收到 500 则什么都做不了。
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from agent_platform.assistant_agent import WorkflowConcierge
from agent_platform.models import ChatMessage, ContentBlock


def _agent(get_build_raises=True):
    services = MagicMock()
    services.workflow_store.get_build = AsyncMock(
        side_effect=KeyError("record not found") if get_build_raises else None)
    return WorkflowConcierge(services, MagicMock())


class BadBuildIdTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_status_with_unknown_id_returns_an_error(self):
        result = await _agent()._exec("build_status", {"build_id": "nope"}, {})
        self.assertIn("error", result)
        self.assertIn("recent_builds", result["error"])   # 告诉模型怎么找回来

    async def test_resume_build_with_unknown_id_returns_an_error(self):
        result = await _agent()._exec("resume_build", {"build_id": "nope"}, {})
        self.assertIn("error", result)

    async def test_abandon_build_with_unknown_id_returns_an_error(self):
        result = await _agent()._exec("abandon_build", {"build_id": "nope"}, {})
        self.assertIn("error", result)

    async def test_missing_build_id_is_not_treated_as_a_lookup(self):
        agent = _agent()
        result = await agent._exec("build_status", {}, {})
        self.assertIn("error", result)
        agent.services.workflow_store.get_build.assert_not_awaited()


class ToolCrashIsContainedTest(unittest.IsolatedAsyncioTestCase):
    async def test_any_tool_blowing_up_becomes_a_tool_error(self):
        """兜底：十几个工具、参数全由模型填，总会有意想不到的炸法。"""
        import agent_platform.assistant_agent as module

        agent = _agent()
        agent._exec = AsyncMock(side_effect=RuntimeError("想不到的炸法"))
        agent.services.storage.append_event = AsyncMock()
        agent.services.provider.stream = MagicMock(return_value=None)

        # 必须是真的 ContentBlock：这些块会被塞回 ChatMessage 做校验
        first = MagicMock(blocks=[ContentBlock(
            type="tool_use", id="t1", name="list_workflows", input={})])
        second = MagicMock(blocks=[ContentBlock(
            type="text", text="刚才那一步没成，我如实说一下")])
        responses = [first, second]
        original = module.collect_model_stream
        module.collect_model_stream = AsyncMock(side_effect=lambda *a, **k: responses.pop(0))
        try:
            actions, text = await agent.reply(
                [{"role": "user", "text": "列一下"}], {"name": "u"})
        finally:
            module.collect_model_stream = original

        # 关键：没有异常冒到调用方（真机上它变成了 HTTP 500）
        self.assertIn("如实说", text)
        self.assertEqual(len(actions), 1)
        self.assertTrue(actions[0]["summary"])
