"""业主看的记录里，不能有搭建方写给自己看的思考。

回归背景（2026-08-28 真机）：业主页把搭建方每一轮的原话直接渲染出去。
transcript 里的实际内容是

    I'll build this word/sentence counter workflow. Let me start by
    checking the relevant block schemas… `variable_assigner` $formula

英文 + 内部节点名 + 平台概念，出现在**付钱那方**的页面上。
工具轮早就翻成「搭了一个环节」这类人话，唯独 text 轮直通。

判据放在接口上而不是页面上：一处判据、能测、对所有消费方生效。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.api import _owner_safe_records, _reads_as_chinese

ENGLISH_THINKING = (
    "I'll build this word/sentence counter workflow. Let me start by "
    "checking the relevant block schemas and available templates."
)
OWNER_FACING = "工作流已发布为正式版 v1，现在可以试运行了"


class ReadsAsChineseTest(unittest.TestCase):
    def test_english_thinking_is_not_for_the_owner(self):
        self.assertFalse(_reads_as_chinese(ENGLISH_THINKING))

    def test_chinese_delivery_note_is_for_the_owner(self):
        self.assertTrue(_reads_as_chinese(OWNER_FACING))

    def test_english_json_with_a_chinese_node_title_is_still_blocked(self):
        # 这就是为什么用比例而不是「含不含中文字」：
        # 搭建方的英文里会夹着中文节点标题，只看含不含就全放行了
        self.assertFalse(_reads_as_chinese(
            '{"node": {"id": "end", "type": "end", "title": "输出", '
            '"config": {"outputs": {"greeting": "string"}}}}'))

    def test_a_short_chinese_line_still_counts(self):
        self.assertTrue(_reads_as_chinese("已经搭好了，你试试"))

    def test_too_short_to_judge_is_withheld(self):
        # 宁可少给一句，也不要把英文原文抬到客户面前
        for text in ("ok", "done", "好", ""):
            self.assertFalse(_reads_as_chinese(text))


class OwnerSafeRecordsTest(unittest.TestCase):
    """接口层：挡掉正文，但保住动作行赖以存在的 tool_calls。"""

    def test_english_turn_keeps_its_tool_calls(self):
        out = _owner_safe_records([{"text": ENGLISH_THINKING,
                                    "tool_calls": [{"tool": "draft_add_node"}]}])
        self.assertEqual(out[0]["text"], "")
        self.assertTrue(out[0]["text_withheld"])
        # 正文挡掉了，但动作行还得有得翻——时间线不能突然断掉
        self.assertEqual(out[0]["tool_calls"], [{"tool": "draft_add_node"}])

    def test_owner_and_event_records_pass_through(self):
        records = [{"kind": "owner", "text": "我要改一下"},
                   {"kind": "event", "text": "已发布"}]
        self.assertEqual(_owner_safe_records(records), records)

    def test_chinese_turn_survives(self):
        out = _owner_safe_records([{"text": OWNER_FACING}])
        self.assertEqual(out[0]["text"], OWNER_FACING)
        self.assertNotIn("text_withheld", out[0])

    def test_the_original_records_are_not_mutated(self):
        # 同一份记录别的地方（操作者页面）还要用原文
        records = [{"text": ENGLISH_THINKING}]
        _owner_safe_records(records)
        self.assertEqual(records[0]["text"], ENGLISH_THINKING)


class OwnerEndpointUsesTheFilterTest(unittest.TestCase):
    """光有函数不够：接口真调用它了吗？

    这条的由来是自查——只测函数的话，把接口里那句
    _owner_safe_records(records) 删掉，测试照样全绿。
    """

    def test_english_thinking_never_leaves_the_endpoint(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        app = create_app(Settings(api_token="owner-test", data_dir=root / "d",
                                  workspace_root=root / "w",
                                  scheduler_poll_seconds=3600))
        with TestClient(app) as client:
            services = app.state.services
            services.workflow_store.verify_owner_code = AsyncMock(return_value=True)
            services.workflow_store.list_builds = AsyncMock(
                return_value=[{"id": "b1"}])
            services.build_transcripts.read = MagicMock(return_value=[
                {"text": ENGLISH_THINKING, "tool_calls": [{"tool": "draft_add_node"}]},
                {"text": OWNER_FACING},
            ])
            services.build_transcripts.summary = MagicMock(return_value={})
            response = client.get("/api/v1/owner/app-1/transcript?code=x")

        self.assertEqual(response.status_code, 200)
        records = response.json()["records"]
        self.assertEqual(records[0]["text"], "")            # 英文思考挡住了
        self.assertTrue(records[0]["text_withheld"])
        self.assertEqual(records[0]["tool_calls"][0]["tool"], "draft_add_node")
        self.assertEqual(records[1]["text"], OWNER_FACING)  # 中文交付说明留着
