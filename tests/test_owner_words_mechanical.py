"""「业主原话」由代码从对话里取，不经模型的手。

回归背景（2026-08-28 真机）：业主说「三行「第一行」「第二行」「第三行」，
行数 3、净字数 5」，管家调监理时改写成了「「abc」「de」「f」…」。
监理照改写忠实出卷，验收失败；管家再拿「业主原话」核对用例，
两边同源当然对得上，于是判定「卷子没问题，是工作流坏了」。
只要原话也由模型填，这个核对就是自己跟自己对，永远发现不了问题。
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from agent_platform.assistant_agent import WorkflowConcierge


class OwnerWordsMechanicalTest(unittest.IsolatedAsyncioTestCase):
    def _agent(self):
        services = MagicMock()
        services.provider.stream = MagicMock(side_effect=RuntimeError("不跑模型"))
        return WorkflowConcierge(services, MagicMock())

    async def _capture(self, history):
        agent = self._agent()
        with self.assertRaises(RuntimeError):
            await agent.reply(history, {})
        return agent._owner_words

    async def test_last_user_message_is_captured_verbatim(self):
        said = "验一下统计。样例：三行「第一行」「第二行」「第三行」，行数 3、净字数 5"
        words = await self._capture([
            {"role": "user", "text": "平台怎么样"},
            {"role": "assistant", "text": "一切正常"},
            {"role": "user", "text": said},
        ])
        self.assertEqual(words, said)

    async def test_assistant_paraphrase_is_never_picked_up(self):
        words = await self._capture([
            {"role": "user", "text": "样例：「第一行」「第二行」「第三行」"},
            {"role": "assistant", "text": "好的，我按「abc」「de」「f」去验"},
        ])
        self.assertIn("第一行", words)
        self.assertNotIn("abc", words)

    async def test_blank_turns_are_skipped(self):
        words = await self._capture([
            {"role": "user", "text": "样例：三行文本"},
            {"role": "user", "text": "   "},
        ])
        self.assertEqual(words, "样例：三行文本")

    async def test_no_user_turn_leaves_it_empty(self):
        self.assertEqual(await self._capture([{"role": "assistant", "text": "在的"}]), "")

    async def test_very_long_message_is_bounded(self):
        words = await self._capture([{"role": "user", "text": "长" * 9_000}])
        self.assertLessEqual(len(words), 4_000)
