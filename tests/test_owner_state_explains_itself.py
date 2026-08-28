"""搭建停下来时，业主页得说清楚"怎么了、你能做什么"。

回归背景（2026-08-29 真机测量）：两条完全不同的路都会走成 needs_attention——

  · 搭建方主动问业主（ask_owner 工具）→ 有 pending_question，业主该回答
  · 搭建自己崩了（异常兜底）        → 没有问题，业主只是被卡住了

业主页两种都渲染成「这里有个问题等你回复」+「回答上面的问题，搭建会继续」。
真机 75 个构建、61 次停下来，pending_question **一条都没有**——
也就是说这句话每次都在请业主回答一个不存在的问题，
而到底出了什么事，业主一个字都看不到。

同一份错误，管家那边早有翻好的中文（_BUILD_ERROR_WORDS，另有测试）。
业主这边什么也没有。缺的不是能力，是接线。
"""
import unittest

from agent_platform.api import _owner_build_note


class OwnerBuildNoteTest(unittest.TestCase):
    def test_a_crash_does_not_pretend_there_is_a_question(self):
        situation, what_to_do = _owner_build_note(
            "needs_attention", None, "model stream timed out after 600s")
        self.assertNotIn("回答", what_to_do, "崩了却让业主去回答不存在的问题")
        self.assertIn("留", what_to_do, "得告诉业主留言就会接着搭")

    def test_a_crash_says_what_actually_happened(self):
        situation, _ = _owner_build_note(
            "needs_attention", None, "model stream timed out after 600s")
        self.assertIn("卡住", situation)
        self.assertIn("（", situation, "有翻译的报错要把原因说出来")

    def test_a_real_question_asks_the_owner_to_answer(self):
        situation, what_to_do = _owner_build_note(
            "needs_attention", "表里的金额列叫什么名字？", None)
        self.assertIn("等你", situation)
        self.assertIn("回答", what_to_do)

    def test_the_two_paths_do_not_say_the_same_thing(self):
        """这就是 bug 本体：崩溃和提问被说成了同一件事。"""
        crashed = _owner_build_note("needs_attention", None, "boom")
        asked = _owner_build_note("needs_attention", "你要按天还是按周汇总？", None)
        self.assertNotEqual(crashed, asked)

    def test_no_english_or_model_names_reach_the_owner(self):
        import re

        for error in ("model stream timed out after 600s",
                      "tool call budget exceeded: 201 > 200",
                      "OpenAI-compatible API returned 400: bad request",
                      "acceptance still failing after 4 repair cycles"):
            for text in _owner_build_note("needs_attention", None, error):
                self.assertIsNone(re.search(r"[a-z]{3,}\s+[a-z]{3,}", text.lower()),
                                  f"业主看到英文：{text}")

    def test_an_unmapped_error_degrades_without_leaking_it(self):
        situation, what_to_do = _owner_build_note(
            "needs_attention", None, "some brand new failure nobody mapped")
        self.assertNotIn("brand", situation)
        self.assertTrue(situation.strip() and what_to_do.strip())

    def test_building_says_nothing_needs_doing(self):
        situation, what_to_do = _owner_build_note("building", None, None)
        self.assertIn("正在搭", situation)
        self.assertIn("不用", what_to_do)

    def test_published_has_no_call_to_action(self):
        situation, what_to_do = _owner_build_note("published", None, None)
        self.assertIn("试运行", situation)
        self.assertEqual(what_to_do, "")

    def test_cancelled_does_not_nag_the_owner_to_resume(self):
        """业主明确不要了的东西，别再劝他续跑。"""
        _, what_to_do = _owner_build_note("cancelled", None, None)
        self.assertNotIn("接着", what_to_do)


class OwnerStateCarriesTheNoteTest(unittest.TestCase):
    """函数写好了、端点没带上，业主页照样什么都看不到。"""

    def test_the_endpoint_returns_situation_and_what_to_do(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import AsyncMock, MagicMock

        from fastapi.testclient import TestClient

        from agent_platform.api import create_app
        from agent_platform.config import Settings

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        app = create_app(Settings(api_token="t", data_dir=root / "d",
                                  workspace_root=root / "w",
                                  scheduler_poll_seconds=3600))
        with TestClient(app) as client:
            services = app.state.services
            services.workflow_store.verify_owner_code = AsyncMock(return_value=True)
            services.workflow_store.get_application = AsyncMock(
                return_value={"id": "a1", "name": "月报", "active_version": 1})
            services.workflow_store.list_builds = AsyncMock(return_value=[{
                "id": "b1", "status": "needs_attention",
                "team_state": MagicMock(pending_question=None),
                "error": "model stream timed out after 600s",
                "updated_at": "2026-08-29T00:00:00+00:00",
            }])
            response = client.get("/api/v1/owner/a1/state?code=x")

        self.assertEqual(response.status_code, 200)
        build = response.json()["build"]
        self.assertIn("卡住", build["situation"])
        self.assertNotIn("回答", build["what_to_do"])


if __name__ == "__main__":
    unittest.main()
