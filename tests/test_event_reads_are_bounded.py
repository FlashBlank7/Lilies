"""读事件必须有上限，而且上限要下推到 SQL。

回归背景（2026-08-29 独立复查）：`GET /v1/streams/{id}` 完全不限量。
真机上 platform_harness 流 435 MB、单个构建流 18 万条事件——
一发请求就能把整个 API 冻十几秒、内存冲到几 GB。
另一处 `/runs/{id}/events/list` 虽然输出取了尾窗，但**先全量读进内存再切片**，
省了带宽没省内存和时间。

关键是「下推到 SQL」而不是「读完再切」：后者在 18 万条的流上照样要全量物化。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_platform.config import Settings
from agent_platform.storage import Storage


class BoundedEventReadTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        settings = Settings(api_token="t", data_dir=Path(self._tmp.name) / "d",
                            workspace_root=Path(self._tmp.name) / "w")
        settings.prepare()
        self.storage = Storage(settings.data_dir)
        await self.storage.initialize()
        for index in range(50):
            await self.storage.append_event("s1", "tick", {"n": index})

    async def test_no_limit_returns_everything(self):
        self.assertEqual(len(await self.storage.list_events("s1")), 50)

    async def test_limit_bounds_the_result(self):
        events = await self.storage.list_events("s1", limit=10)
        self.assertEqual(len(events), 10)
        self.assertEqual(events[0].data["n"], 0)      # 默认取最前面

    async def test_tail_takes_the_end_in_order(self):
        events = await self.storage.list_events("s1", limit=10, tail=True)
        self.assertEqual(len(events), 10)
        self.assertEqual(events[0].data["n"], 40)     # 顺序仍是正序
        self.assertEqual(events[-1].data["n"], 49)

    async def test_limit_is_pushed_into_sql_not_sliced_after(self):
        """下推与否的区别：切片版会把全部行读出来。

        直接查 SQL 层的实现，确认带 limit 时执行的语句里有 LIMIT——
        不然「限量」只是省了带宽，内存和时间照样爆。
        """
        seen = []
        original = self.storage._connect

        class _Spy:
            def __init__(self, conn): self._conn = conn
            def execute(self, sql, *args):
                seen.append(" ".join(sql.split()))
                return self._conn.execute(sql, *args)
            def __enter__(self): return self
            def __exit__(self, *a): return self._conn.__exit__(*a)

        self.storage._connect = lambda: _Spy(original())
        try:
            await self.storage.list_events("s1", limit=5)
        finally:
            self.storage._connect = original
        self.assertTrue(any("LIMIT" in sql for sql in seen), seen)

    async def test_after_and_limit_combine(self):
        events = await self.storage.list_events("s1", after=20, limit=5)
        self.assertEqual(len(events), 5)
        self.assertTrue(all(e.data["n"] >= 20 for e in events))

    async def test_cold_backfill_does_not_defeat_the_limit(self):
        """归档冷读会把全量补回来——限量必须挡住它。

        真机上就是这么栽的：tail=10 返回了 50 条。冷读看到「首条 seq 不是 1」
        就以为前面被归档了，于是把冷文件里的全部补在前面。
        可 tail 的前段缺口是**故意**留的。
        """
        cold = [type("R", (), {"id": n, "data": {"n": -n}})() for n in range(1, 100)]
        original = self.storage._read_cold_events
        self.storage._read_cold_events = lambda *a, **k: cold
        try:
            events = await self.storage.list_events("s1", limit=10, tail=True)
            head = await self.storage.list_events("s1", limit=10)
        finally:
            self.storage._read_cold_events = original
        self.assertEqual(len(events), 10, "tail 被冷读撑破了")
        self.assertEqual(len(head), 10, "head 被冷读撑破了")
