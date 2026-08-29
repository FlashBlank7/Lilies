"""给模型看的那份总览，字段名不能让人（机）读错。

回归背景（2026-08-29 真跑 REPL 撞到）：问"昨天有没有失败的运行"，
它先答"没有"（用了 health_report——那是看"现在健不健康"的，
一个工作流昨天 5 败 26 成，在体检里仍然正常）。
把"这份数据不回答什么"贴到体检结果里之后，它改去查总览了。

接着又读错一步：总览的失败清单里 `count` 是**这个原因在窗口内一共出现过
几次**，`at` 是最近一次的时刻。两个挨在一起，它答成"当天失败 13 次"——
当天其实是 5 次，13 是近 7 天的合计。实测复现过两次。

所以把**给模型的那一份**换成不会读错的名字。接口本身一个字没动：
客户端契约（guanjia doctor --contract）和前端读的都还是原字段。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_platform.assistant_agent import WorkflowConcierge

# 这份夹具要**长得像真载荷**。2026-08-29 它缺 date_utc（真 build_overview
# 一定带），于是加"日期口径"那段时七条用例集体 KeyError——
# 夹具与真值分家的老毛病，这次是夹具比真的少了字段。
RAW = {
    "date_utc": "2026-08-29",
    "runs_today": {"total": 3, "succeeded": 2, "failed": 1, "running": 0},
    "published_workflows": 3, "builds_active": 0, "week": [],
    "week_failures": [], "recent_failures_total": 1, "schedules": [],
    "recent_failures": [
        {"run_id": "abc12345", "workflow": "文本行数与净字数统计",
         "at": "2026-08-28T10:03:36", "count": 13,
         "error": "缺少必填输入「text」"},
    ],
}


class OverviewForTheModelTest(unittest.IsolatedAsyncioTestCase):
    async def _exec(self):
        agent = WorkflowConcierge.__new__(WorkflowConcierge)
        agent.services = SimpleNamespace()
        with patch("agent_platform.overview.build_overview",
                   AsyncMock(return_value=dict(RAW))):
            return await agent._exec("platform_overview", {}, {})

    async def test_the_count_field_says_what_it_counts(self):
        row = (await self._exec())["recent_failures"][0]
        self.assertEqual(row["这个原因一共出现过几次"], 13)
        self.assertNotIn("count", row, "旧名字还在，模型还是会读错")

    async def test_the_time_field_says_it_is_the_latest_one(self):
        row = (await self._exec())["recent_failures"][0]
        self.assertEqual(row["最近一次失败在"], "2026-08-28T10:03:36")
        self.assertNotIn("at", row)

    async def test_the_payload_spells_out_how_to_read_it(self):
        result = await self._exec()
        how = result["失败清单怎么读"]
        self.assertIn("整个窗口", how)
        self.assertIn("不是", how, "没点破「那天的次数」这个误读")

    async def test_the_workflow_and_reason_survive(self):
        row = (await self._exec())["recent_failures"][0]
        self.assertEqual(row["工作流"], "文本行数与净字数统计")
        self.assertIn("text", row["原因"])

    async def test_run_id_is_kept_for_follow_up(self):
        """要能顺着查下去——run_id 不能在改名时弄丢。"""
        self.assertEqual((await self._exec())["recent_failures"][0]["run_id"], "abc12345")

    async def test_the_rest_of_the_overview_is_untouched(self):
        result = await self._exec()
        self.assertEqual(result["runs_today"], RAW["runs_today"])
        self.assertEqual(result["published_workflows"], 3)

    async def test_an_empty_failure_list_is_fine(self):
        agent = WorkflowConcierge.__new__(WorkflowConcierge)
        agent.services = SimpleNamespace()
        with patch("agent_platform.overview.build_overview",
                   AsyncMock(return_value={**RAW, "recent_failures": []})):
            result = await agent._exec("platform_overview", {}, {})
        self.assertEqual(result["recent_failures"], [])


class ApiContractIsUnchangedTest(unittest.TestCase):
    """接口本身一个字不能动——客户端契约和前端都读原字段。

    初稿是 inspect.getsource 查源码里有没有 '"count"' 这个串，红了：
    那个键是 _dedupe_failures 产出的，build_overview 里根本看不到。
    grep 源码的断言就是这样——既容易误报，也证明不了行为。
    改成真调那个函数看它吐出什么键。
    """

    def test_the_failure_rows_keep_the_documented_keys(self):
        from agent_platform.overview import _dedupe_failures

        rows = _dedupe_failures([
            {"id": "abc12345", "name": "文本行数与净字数统计",
             "at": "2026-08-28T10:03:36", "error": "缺少必填输入「text」"},
        ])
        self.assertEqual(set(rows[0]), {"run_id", "workflow", "at", "count", "error"},
                         "接口字段变了，guanjia 和前端会读不到")


class FixtureMatchesTheRealPayloadTest(unittest.IsolatedAsyncioTestCase):
    """夹具的字段要跟真 build_overview 对得上。

    2026-08-29：这份夹具缺 date_utc（真载荷一定带），加「日期口径」那段时
    七条用例集体 KeyError。夹具比真的少字段，测试就会在"真载荷有、
    夹具没有"的那些路径上装聋——直到某天一起塌。

    这条盯着它别再走样：真跑一次 build_overview，比字段集合。
    """

    async def test_no_key_of_the_real_payload_is_missing_from_the_fixture(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace

        from agent_platform.overview import build_overview
        from agent_platform.storage import Storage
        from agent_platform.workflow_storage import WorkflowStorage

        with TemporaryDirectory() as tmp:
            storage = Storage(Path(tmp) / "d")
            store = WorkflowStorage(storage)
            await storage.initialize()
            await store.initialize()
            real = await build_overview(
                SimpleNamespace(workflow_store=SimpleNamespace(storage=storage)))
        missing = sorted(set(real) - set(RAW))
        self.assertEqual(missing, [], f"夹具少了真载荷里的字段：{missing}")
