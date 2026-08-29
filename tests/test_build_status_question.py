"""构建状态：既要说得出「在问什么」，也要说人话。

回归背景（2026-08-28 真机）：问「那个构建怎么样了」，回答里三处内部词
直接见了用户——「状态：needs_attention」「`model stream timed out after 600s`」
「第 8 版修订」。提示词里禁止机器词汇拦不住，模型手里只有这些词。
"""
import re
import unittest
from unittest.mock import AsyncMock, MagicMock

from agent_platform.assistant_agent import WorkflowConcierge

# 状态码、英文报错、内部计数——一个都不该出现在给模型的载荷里
LEAK = re.compile(r"needs_attention|queued|building|published|revision|"
                  r"stream timed out|rate limit|resume_build|build_status|"
                  r"[a-z]{4,} [a-z]{4,} [a-z]{4,}")


def _concierge(status, pending_question, error=""):
    state = MagicMock()
    state.revision = 8
    state.published_version = None
    state.pending_question = pending_question
    services = MagicMock()
    services.workflow_store.get_build = AsyncMock(
        return_value={"id": "b1", "status": status, "error": error,
                      "team_state": state})
    return WorkflowConcierge(services, MagicMock())


class BuildStatusQuestionTest(unittest.IsolatedAsyncioTestCase):
    async def test_pending_question_is_surfaced(self):
        agent = _concierge("needs_attention", "净字数要不要算标点？")
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertEqual(result["搭建方在问"], "净字数要不要算标点？")
        # 光把问题塞进去还不够：模型得知道拿到答复后怎么送回去
        self.assertIn("等他回答", result["接下来"])

    async def test_long_question_is_truncated_not_dropped(self):
        agent = _concierge("needs_attention", "问" * 5000)
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertTrue(result["搭建方在问"])
        self.assertLessEqual(len(result["搭建方在问"]), 600)

    async def test_running_build_says_it_is_still_working(self):
        agent = _concierge("building", None)
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertNotIn("搭建方在问", result)
        self.assertIn("还在搭", result["情况"])

    async def test_stall_is_explained_in_plain_language(self):
        agent = _concierge("needs_attention", None,
                           "model stream timed out after 600s")
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertIn("卡住", result["情况"])
        # 超时的原因要说出来，但要说人话
        self.assertIn("断了", result["情况"])
        self.assertIn("接着跑", result["接下来"])

    async def test_no_internal_words_reach_the_model(self):
        for status, question, error in (
            ("needs_attention", None, "model stream timed out after 600s"),
            ("needs_attention", "要不要算标点？", ""),
            ("building", None, ""),
            ("published", None, ""),
            ("failed", None, "Connection refused by upstream"),
            ("cancelled", None, ""),
            ("weird_new_status", None, ""),
        ):
            agent = _concierge(status, question, error)
            result = await agent._exec("build_status", {"build_id": "b1"}, {})
            # 下划线键是公开约定的「给模型的指引」，不进业主视野；
            # 搭建方原话是业主要看的正文，两者都排除后再扫
            payload = " ".join(str(value) for key, value in result.items()
                               if key != "搭建方在问" and not key.startswith("_"))
            self.assertIsNone(LEAK.search(payload.lower()),
                              f"{status}/{error} 泄漏了内部词：{payload}")

    async def test_cancelled_build_is_not_offered_for_resume(self):
        """业主明确不要了的东西，别再劝他续跑。"""
        agent = _concierge("cancelled", None, "")
        result = await agent._exec("build_status", {"build_id": "b1"}, {})
        self.assertIn("放弃", result["情况"])
        self.assertNotIn("未知", result["情况"])
        self.assertNotIn("接着跑", result["接下来"])

    async def test_recent_builds_translates_too(self):
        """真机漏的就是这条路：build_status 翻了，兄弟工具没翻。"""
        state = MagicMock()
        state.pending_question = None
        services = MagicMock()
        services.workflow_store.list_recent_builds = AsyncMock(return_value=[
            {"id": "b1", "status": "needs_attention", "requirement": "做个日报",
             "error": "model stream timed out after 600s", "team_state": state}])
        agent = WorkflowConcierge(services, MagicMock())
        result = await agent._exec("recent_builds", {}, {})
        payload = " ".join(str(v) for row in result["最近几个（不是全部）"]
                           for k, v in row.items() if not k.startswith("_"))
        self.assertIsNone(LEAK.search(payload.lower()), payload)
        self.assertIn("卡住", payload)

    async def test_recent_builds_carries_a_time(self):
        """真机上问「最近一次搭建什么时候完成的」，它如实答"工具返回里没带
        具体日期"——答得对，但这个数库里就有（builds.updated_at），
        没理由让业主查不到。"""
        state = MagicMock()
        state.pending_question = None
        services = MagicMock()
        services.workflow_store.list_recent_builds = AsyncMock(return_value=[
            {"id": "b1", "status": "published", "requirement": "做个日报",
             "error": "", "team_state": state,
             "updated_at": "2026-08-28T09:36:12.345678+00:00"}])
        agent = WorkflowConcierge(services, MagicMock())
        row = (await agent._exec("recent_builds", {}, {}))["最近几个（不是全部）"][0]
        when = row["最后动静是什么时候"]
        assert when.startswith("2026-08-28"), when
        assert "T" not in when, f"给人看的时间别留 T：{when}"
        assert len(when) <= 16, f"精确到分就够，别甩一串微秒：{when}"

    async def test_a_build_without_a_time_does_not_crash(self):
        """老记录可能没有这个字段——缺了就给空串，别炸也别编。"""
        state = MagicMock()
        state.pending_question = None
        services = MagicMock()
        services.workflow_store.list_recent_builds = AsyncMock(return_value=[
            {"id": "b1", "status": "published", "requirement": "x",
             "error": "", "team_state": state}])
        agent = WorkflowConcierge(services, MagicMock())
        row = (await agent._exec("recent_builds", {}, {}))["最近几个（不是全部）"][0]
        assert row["最后动静是什么时候"] == ""

    async def test_every_state_tells_the_user_what_to_do(self):
        for status in ("building", "published", "needs_attention", "failed",
                       "cancelled", "weird_new_status"):
            agent = _concierge(status, None, "")
            result = await agent._exec("build_status", {"build_id": "b1"}, {})
            self.assertTrue(result["情况"].strip(), status)
            self.assertTrue(result["接下来"].strip(), status)
