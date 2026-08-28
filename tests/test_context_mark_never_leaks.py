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
from agent_platform.assistant_agent import (WorkflowConcierge,
                                            _without_context_marks,
                                            _without_tool_names)
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


class ToolNamesNeverReachTheUserTest(unittest.TestCase):
    """工具名也靠剪，不靠嘱咐。

    提示词里早写着「回答里不出现工具名」，可它还是照说。真机原话：
        「我只能通过 tidy_workflows 把工作流收起来」
        「用 build_status 跟进即可」
    这些是 snake_case 的内部标识符，中文回答里没有正当理由出现。
    """

    def test_tool_name_becomes_a_phrase_not_a_hole(self):
        # 换成词组而不是删掉，免得把句子弄断
        out = _without_tool_names("我只能通过 tidy_workflows 把工作流收起来")
        self.assertNotIn("tidy_workflows", out)
        self.assertIn("收拾列表", out)

    def test_every_tool_name_is_covered(self):
        from agent_platform.assistant_agent import TOOLS

        for tool in TOOLS:
            out = _without_tool_names(f"用 {tool.name} 就行")
            self.assertNotIn(tool.name, out, f"{tool.name} 没有对应的人话")

    def test_ordinary_text_is_untouched(self):
        for text in ("「文本行数与净字数统计」跑通了", "正常回答", ""):
            self.assertEqual(_without_tool_names(text), text)

    def test_it_runs_on_the_reply_path(self):
        # 接线：出口那道清洗要真的调用它
        out = _without_context_marks('<上下文 a="1" />用 build_status 跟进')
        self.assertNotIn("build_status", out)
        self.assertNotIn("上下文", out)
