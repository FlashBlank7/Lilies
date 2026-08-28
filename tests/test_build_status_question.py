"""搭建方停下来问业主时，build_status 必须把问题带出来。

回归背景：build_status 只返回状态词，遇到 needs_attention 时模型
说得出「需要你注意」却说不出在问什么——用户等构建、构建等用户。
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from agent_platform.assistant_agent import WorkflowConcierge


def _concierge(status, pending_question):
    state = MagicMock()
    state.revision = 3
    state.published_version = None
    state.pending_question = pending_question
    services = MagicMock()
    services.workflow_store.get_build = AsyncMock(
        return_value={"id": "b1", "status": status, "error": "", "team_state": state})
    return WorkflowConcierge(services, MagicMock())


class BuildStatusQuestionTest(unittest.IsolatedAsyncioTestCase):
    async def test_pending_question_is_surfaced(self):
        agent = _concierge("needs_attention", "净字数要不要算标点？")
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertEqual(result["pending_question"], "净字数要不要算标点？")
        # 光把问题塞进去还不够：模型得知道拿到答复后怎么送回去
        self.assertIn("resume_build", result["note"])

    async def test_long_question_is_truncated_not_dropped(self):
        agent = _concierge("needs_attention", "问" * 5000)
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertTrue(result["pending_question"])
        self.assertLessEqual(len(result["pending_question"]), 600)

    async def test_running_build_says_it_is_still_working(self):
        agent = _concierge("building", None)
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertNotIn("pending_question", result)
        self.assertIn("revision", result["note"])

    async def test_finished_build_gets_no_misleading_note(self):
        agent = _concierge("published", None)
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertNotIn("note", result)
