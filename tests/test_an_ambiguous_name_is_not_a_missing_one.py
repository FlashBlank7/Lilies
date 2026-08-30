"""「对得上好几个」被说成「一个都没有」。

`_named_app` 的注释分开了两件事：
  · 没说是哪个工作流（模型漏传参数）
  · 说了，但没有这个（业主记错名字）
漏了第三种：**说了，但对得上好几个**。它走的是第二条路，于是

    业主：跑一下日报
    管家：没有叫「日报」的工作流

而业主明明在列表里见过「日报基准-一」和「日报基准-二」。
这句话会让他以为工作流被删了——而真相只是名字不够具体。

客户端那边一直是对的（`guanjia run 日报` 会把候选列出来，
真机验过），平台这侧漏了。同一个判据没铺满出口。

顺带钉住匹配口径本身（精确 → 子串 → 宽松），
以前它藏在 _resolve_app 里，没有一条测试直接说过它是什么。
"""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock

from agent_platform.assistant_agent import WorkflowConcierge

APPS = [
    {"id": "a1", "name": "日报基准-一"},
    {"id": "a2", "name": "日报基准-二"},
    {"id": "a3", "name": "词频统计"},
    {"id": "a4", "name": "GPU Daily Report"},
]


def _agent(apps=APPS, archived=()):
    services = MagicMock()
    services.workflow_store.list_applications = AsyncMock(return_value=list(apps))
    services.workflow_store.list_archived = AsyncMock(return_value=list(archived))
    return WorkflowConcierge(services, MagicMock())


class AmbiguousNameIsItsOwnAnswer(unittest.IsolatedAsyncioTestCase):
    async def test_it_does_not_claim_the_workflow_is_missing(self):
        _, error = await _agent()._named_app({"name_or_id": "日报"})
        self.assertNotIn("没有叫", error["error"], error)

    async def test_it_lists_the_candidates(self):
        """只说"有好几个"还是让业主猜——把名字摆出来才接得上下一句。"""
        _, error = await _agent()._named_app({"name_or_id": "日报"})
        self.assertIn("日报基准-一", error["error"])
        self.assertIn("日报基准-二", error["error"])

    async def test_it_tells_the_model_what_to_do(self):
        _, error = await _agent()._named_app({"name_or_id": "日报"})
        self.assertIn("问清", error["error"])

    async def test_many_candidates_are_capped_and_counted(self):
        """截了要说——列一屏名字没用，但得让人知道一共多少个。"""
        apps = [{"id": f"a{i}", "name": f"日报-{i}"} for i in range(9)]
        _, error = await _agent(apps)._named_app({"name_or_id": "日报"})
        self.assertIn("共 9 个", error["error"])


class TheOtherTwoAnswersAreUnchanged(unittest.IsolatedAsyncioTestCase):
    """反向那一批：新加的分支不能把原来两句话吃掉。"""

    async def test_no_name_at_all(self):
        _, error = await _agent()._named_app({})
        self.assertIn("没说是哪个工作流", error["error"])

    async def test_a_name_that_matches_nothing(self):
        _, error = await _agent()._named_app({"name_or_id": "根本没有的"})
        self.assertIn("没有叫", error["error"])

    async def test_an_unambiguous_name_still_resolves(self):
        app, error = await _agent()._named_app({"name_or_id": "词频"})
        self.assertIsNone(error)
        self.assertEqual(app["name"], "词频统计")


class TheMatchingOrderIsPinned(unittest.IsolatedAsyncioTestCase):
    """匹配口径以前藏在 _resolve_app 里，没有一条测试直接说过它是什么。"""

    async def test_an_exact_name_beats_substring_rivals(self):
        """全名精确命中时不算歧义——否则叫「日报」的那个永远选不中。"""
        apps = APPS + [{"id": "a9", "name": "日报"}]
        app, error = await _agent(apps)._named_app({"name_or_id": "日报"})
        self.assertIsNone(error, error)
        self.assertEqual(app["id"], "a9")

    async def test_an_id_also_resolves(self):
        app, _ = await _agent()._named_app({"name_or_id": "a3"})
        self.assertEqual(app["name"], "词频统计")

    async def test_case_and_spaces_are_forgiven_when_nothing_else_matches(self):
        app, error = await _agent()._named_app({"name_or_id": "gpudailyreport"})
        self.assertIsNone(error, error)
        self.assertEqual(app["id"], "a4")

    async def test_archived_are_only_searched_when_asked(self):
        """「拿回 X」要找得到已收起来的；平时不找，免得凭空多出歧义。"""
        archived = [{"id": "z1", "name": "日报-收起来的"}]
        agent = _agent(archived=archived)
        app, _ = await agent._named_app({"name_or_id": "日报-收起来的"},
                                        include_archived=True)
        self.assertEqual(app["id"], "z1")
        _, error = await agent._named_app({"name_or_id": "日报-收起来的"})
        self.assertIn("没有叫", error["error"])
