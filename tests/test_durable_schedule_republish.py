"""重新发布不能让当天的定时再开一炮——durable 那条路。

回归背景（2026-08-29 独立复查）：非 durable 那条路昨天修过一次
（claim_schedule_fire 的去重键去掉了版本号），但 durable 这条**漏了**：
幂等键是 `schedule:{app}:{version}:{node}:{date}`，带着版本号。
业主早上改一次工作流、下午再改一次，同一天那个定时就跑三趟。

线上此刻零暴露（durable_jobs 表是空的，唯一那个线上定时没开 durable），
所以这是"开了就中"的雷，不是正在流血的伤口。
一个 bug 修一半比不修更糟：两条路看起来都防住了，实际只防住一条。
"""
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, MagicMock

from agent_platform.config import Settings
from agent_platform.blocks import ScheduleTriggerConfig
from agent_platform.scheduler import WorkflowScheduler


class DurableScheduleRepublishTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        settings = Settings(api_token="t", data_dir=root / "d", workspace_root=root / "w")
        settings.prepare()
        self.enqueued: list[dict] = []

        async def _enqueue(**kwargs):
            self.enqueued.append(kwargs)
            return MagicMock(id=kwargs["job_id"], status="pending")

        self.scheduler = WorkflowScheduler.__new__(WorkflowScheduler)
        self.scheduler.durable_jobs = MagicMock(enqueue=AsyncMock(side_effect=_enqueue))
        self.scheduler.storage = MagicMock(append_event=AsyncMock())
        self.config = ScheduleTriggerConfig(hour=9, minute=0, timezone="Asia/Shanghai")

    async def _enqueue_at_version(self, version: int):
        return await self.scheduler.enqueue_durable_schedule(
            "app-1",
            version=version,
            node_id="sched",
            local_date="2026-08-29",
            triggered_at=datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc),
            config=self.config,
        )

    async def test_republishing_keeps_the_same_idempotency_key(self):
        """业主当天改了两次工作流——版本 1、2、3，还是同一炮。"""
        for version in (1, 2, 3):
            await self._enqueue_at_version(version)
        keys = {call["idempotency_key"] for call in self.enqueued}
        self.assertEqual(len(keys), 1, f"重新发布换出了新键，当天会多开炮：{keys}")

    async def test_job_id_is_also_stable_across_versions(self):
        """键稳定但 job_id 不稳定的话，去重照样落空——它才是真正的主键。"""
        for version in (1, 2, 3):
            await self._enqueue_at_version(version)
        self.assertEqual(len({call["job_id"] for call in self.enqueued}), 1)

    async def test_the_key_does_not_mention_the_version(self):
        await self._enqueue_at_version(7)
        key = self.enqueued[0]["idempotency_key"]
        self.assertNotIn("7", key, f"版本号漏进了幂等键：{key}")
        self.assertIn("app-1", key)
        self.assertIn("2026-08-29", key)

    async def test_a_different_day_is_a_different_job(self):
        """去重不能宽到把明天那炮也吃掉。"""
        await self._enqueue_at_version(1)
        await self.scheduler.enqueue_durable_schedule(
            "app-1", version=1, node_id="sched", local_date="2026-08-30",
            triggered_at=datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc),
            config=self.config)
        self.assertEqual(len({c["idempotency_key"] for c in self.enqueued}), 2)

    async def test_a_different_node_is_a_different_job(self):
        """一个工作流挂两个定时（早报、晚报），两炮都要开。"""
        await self._enqueue_at_version(1)
        await self.scheduler.enqueue_durable_schedule(
            "app-1", version=1, node_id="evening", local_date="2026-08-29",
            triggered_at=datetime(2026, 8, 29, 10, 0, tzinfo=timezone.utc),
            config=self.config)
        self.assertEqual(len({c["idempotency_key"] for c in self.enqueued}), 2)

    async def test_version_still_reaches_the_job_record(self):
        """键里不带版本，不等于版本可以丢——跑的时候要知道跑的是哪一版。"""
        await self._enqueue_at_version(5)
        self.assertEqual(self.enqueued[0]["version"], 5)


# 这里原本还有一条 BothSchedulePathsAgreeTest：inspect.getsource 两条路的源码，
# grep "version" 有没有出现在键里。删掉了——它断言的是**源码长什么样**，
# 比要保证的东西弱，改个写法就能骗过去。
# 两条路现在都有行为覆盖：durable 看上面，非 durable 看
# tests/test_republish_does_not_refire.py（8 条）。够了。
