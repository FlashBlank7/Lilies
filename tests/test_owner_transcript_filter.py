"""业主看的记录里，不能有搭建方写给自己看的话。

回归背景（2026-08-28 发现，2026-08-29 独立复查推翻了第一版修法）：
业主页把搭建方每一轮的原话直接渲染给**付钱那方**看，内容是

    I'll build this word/sentence counter workflow… `variable_assigner` $formula

第一版判据是「这段话是不是中文」，失败得很彻底：真机 76 份 transcript 上，
按语种放行的 164 条里 70 条带着 <tool_call>、variable_assigner、$formula——
线上主力 classic 引擎思考时全程中文，语种判据对它近乎零防护。
换成「有没有机器痕迹」也只挡住一半，剩下的是
「我删除了 aggregator→assigner 的边」这种纯中文的图上手术叙述。

结论：文本里没有可靠信号能把「自言自语」和「对业主说的话」分开，所以不猜——
turn 的正文一律不给。业主该知道的都在 event 与动作行里。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from agent_platform.api import _owner_safe_records, create_app
from agent_platform.config import Settings

ENGLISH_THINKING = ("I'll build this word/sentence counter workflow. Let me start by "
                    "checking the relevant block schemas.")
CHINESE_THINKING = "我删除了 aggregator→assigner 的边，现在需要添加 normalizer→assigner 的边"
MILESTONE = "工作流已发布为正式版 v1，现在可以试运行了"


class OwnerSeesNoBuilderProseTest(unittest.TestCase):
    def test_english_thinking_is_withheld(self):
        out = _owner_safe_records([{"kind": "turn", "text": ENGLISH_THINKING}])
        self.assertEqual(out[0]["text"], "")
        self.assertTrue(out[0]["text_withheld"])

    def test_chinese_thinking_is_withheld_too(self):
        """第一版就栽在这：中文的内部推理被当成「给业主的话」放行了。"""
        out = _owner_safe_records([{"kind": "turn", "text": CHINESE_THINKING}])
        self.assertEqual(out[0]["text"], "")
        self.assertTrue(out[0]["text_withheld"])

    def test_tool_calls_survive_so_the_timeline_keeps_its_action_lines(self):
        out = _owner_safe_records([{"kind": "turn", "text": CHINESE_THINKING,
                                    "tool_calls": [{"tool": "draft_add_node"}]}])
        self.assertEqual(out[0]["tool_calls"], [{"tool": "draft_add_node"}])

    def test_milestone_events_pass_through(self):
        """业主该知道的里程碑都是 event，不受影响——这是敢全挡 turn 的前提。"""
        out = _owner_safe_records([{"kind": "event", "text": MILESTONE}])
        self.assertEqual(out[0]["text"], MILESTONE)
        self.assertNotIn("text_withheld", out[0])

    def test_owner_own_messages_pass_through(self):
        out = _owner_safe_records([{"kind": "owner", "text": "我要改一下"}])
        self.assertEqual(out[0]["text"], "我要改一下")

    def test_the_original_records_are_not_mutated(self):
        # 操作者页面还要看原文
        records = [{"kind": "turn", "text": ENGLISH_THINKING}]
        _owner_safe_records(records)
        self.assertEqual(records[0]["text"], ENGLISH_THINKING)


class OwnerEndpointUsesTheFilterTest(unittest.TestCase):
    """光有判据不够：接口真调用它了吗？

    只测函数的话，把接口里那句 _owner_safe_records(records) 删掉，
    测试照样全绿——测了判据，没测接线。
    """

    def test_builder_prose_never_leaves_the_endpoint(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        app = create_app(Settings(api_token="owner-test", data_dir=root / "d",
                                  workspace_root=root / "w",
                                  scheduler_poll_seconds=3600))
        with TestClient(app) as client:
            services = app.state.services
            services.workflow_store.verify_owner_code = AsyncMock(return_value=True)
            services.workflow_store.list_builds = AsyncMock(return_value=[{"id": "b1"}])
            services.build_transcripts.read = MagicMock(return_value=[
                {"kind": "turn", "text": CHINESE_THINKING,
                 "tool_calls": [{"tool": "draft_add_node"}]},
                {"kind": "event", "text": MILESTONE},
            ])
            services.build_transcripts.summary = MagicMock(return_value={})
            response = client.get("/api/v1/owner/app-1/transcript?code=x")

        self.assertEqual(response.status_code, 200)
        records = response.json()["records"]
        self.assertEqual(records[0]["text"], "")             # 搭建方正文挡住了
        self.assertTrue(records[0]["text_withheld"])
        self.assertEqual(records[0]["tool_calls"][0]["tool"], "draft_add_node")
        self.assertEqual(records[1]["text"], MILESTONE)      # 里程碑还在
