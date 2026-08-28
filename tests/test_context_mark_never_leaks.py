"""内部上下文标记绝不能出现在回答里——靠剪，不靠嘱咐。

回归背景（2026-08-28，含糊/改主意那段对话）：问「它能发邮件吗」，
回答第一行原样是

    <上下文 上一轮做了="explain_workflow" />

这个标记是塞进历史给模型解析指代用的。它原本是方括号形式，
因为被抄进回答才改成 XML 式；结果证明换写法只是抄得少一点。
提示词里那句「绝不能出现在回答里」是约束，不是保证。
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

import agent_platform.assistant_agent as module
from agent_platform.assistant_agent import WorkflowConcierge, _without_context_marks
from agent_platform.models import ContentBlock


class StripContextMarkTest(unittest.TestCase):
    def test_marker_at_the_start_is_removed(self):
        self.assertEqual(
            _without_context_marks('<上下文 上一轮做了="explain_workflow" />\n\n它不能发邮件'),
            "它不能发邮件")

    def test_marker_in_the_middle_is_removed(self):
        self.assertEqual(_without_context_marks('前面 <上下文 x="1" /> 后面'),
                         "前面 后面")

    def test_several_markers(self):
        self.assertEqual(
            _without_context_marks('<上下文 a="1" /><上下文 b="2" />正文'), "正文")

    def test_ordinary_text_is_untouched(self):
        for text in ("正常回答", "", "带 < 号的正常文本", "a > b"):
            self.assertEqual(_without_context_marks(text), text.strip())

    def test_real_angle_bracket_content_survives(self):
        # 不能顺手把用户正文里的尖括号内容也吃掉
        self.assertEqual(_without_context_marks("用 <b> 标签加粗"), "用 <b> 标签加粗")


class ReplyNeverEmitsTheMarkTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_reply_that_copied_the_marker_is_cleaned(self):
        services = MagicMock()
        services.storage.append_event = AsyncMock()
        agent = WorkflowConcierge(services, MagicMock())

        leaked = MagicMock(blocks=[ContentBlock(
            type="text", text='<上下文 上一轮做了="explain_workflow" />\n它不能发邮件')])
        original = module.collect_model_stream
        module.collect_model_stream = AsyncMock(return_value=leaked)
        try:
            _, text = await agent.reply([{"role": "user", "text": "它能发邮件吗"}],
                                        {"name": "u"})
        finally:
            module.collect_model_stream = original
        self.assertEqual(text, "它不能发邮件")
        self.assertNotIn("上下文", text)
