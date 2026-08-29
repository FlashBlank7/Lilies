"""事件冷归档：DB 只留近期，JSONL 冷文件是权威全量，读取端无感回退。

三条铁律：
1. 归档后 list_events 返回的事件一条不少（冷热合并）；
2. 每个 stream 保留最大 seq 哨兵——新事件序号永不回退撞号；
3. 未过期与活跃 stream 不受影响。
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from agent_platform.storage import Storage


def _make_storage(tmp_path: Path) -> Storage:
    storage = Storage(tmp_path / "data")
    asyncio.run(storage.initialize()) if hasattr(storage, "initialize") else None
    return storage


def _age_events(storage: Storage, stream_id: str, days: int) -> None:
    with storage._connect() as conn:
        conn.execute(
            "UPDATE events SET created_at = datetime('now', ?) || '+00:00'"
            " WHERE stream_id=?",
            (f"-{days} days", stream_id),
        )


def test_archive_keeps_full_history_readable(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        if hasattr(storage, "initialize"):
            await storage.initialize()
        for index in range(10):
            await storage.append_event("run-old", "node.completed", {"i": index})
        await storage.append_event("run-new", "workflow.started", {})

        _age_events(storage, "run-old", days=30)

        result = await storage.archive_events_before(keep_days=7)
        # run-old 留 1 条哨兵，run-new 不动
        assert result["removed"] == 9
        assert result["remaining"] == 2

        # 铁律 1：读取无感——10 条一条不少，顺序正确
        events = await storage.list_events("run-old", 0)
        assert [e.id for e in events] == list(range(1, 11))
        assert events[0].data == {"i": 0}

        # after 定位在冷区中段也正确
        tail = await storage.list_events("run-old", 4)
        assert [e.id for e in tail] == [5, 6, 7, 8, 9, 10]

        # 铁律 2：新事件 seq 延续不回退
        appended = await storage.append_event("run-old", "workflow.completed", {})
        assert appended.id == 11

        # 铁律 3：未过期 stream 完整保留在 DB（不触发冷读也全量）
        fresh = await storage.list_events("run-new", 0)
        assert len(fresh) == 1

    asyncio.run(scenario())


def test_cold_file_compression_roundtrip(tmp_path: Path) -> None:
    """冷文件 gzip 压缩后读取无感；stream 复活（.gz + 新 .jsonl 并存）合并去重。"""

    import os

    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        if hasattr(storage, "initialize"):
            await storage.initialize()
        for index in range(5):
            await storage.append_event("run-z", "node.completed", {"i": index})

        # 老化文件 mtime → 压缩
        cold = storage.events_dir / "run-z.jsonl"
        old = 10_000_000
        os.utime(cold, (old, old))
        result = await storage.compress_cold_event_files(older_than_days=14)
        assert result["compressed"] == 1
        assert not cold.exists()
        assert (storage.events_dir / "run-z.jsonl.gz").exists()

        # 模拟真实归档语义：保留每 stream 最大 seq 哨兵行
        with storage._connect() as conn:
            conn.execute("DELETE FROM events WHERE stream_id='run-z' AND seq < 5")
        events = await storage.list_events("run-z", 0)
        assert [e.id for e in events] == [1, 2, 3, 4, 5]

        # 复活：追加新事件（新 .jsonl，seq 接续哨兵），读取合并 .gz + .jsonl
        appended = await storage.append_event("run-z", "workflow.completed", {})
        assert appended.id == 6
        with storage._connect() as conn:
            conn.execute("DELETE FROM events WHERE stream_id='run-z' AND seq < 6")
        merged = await storage.list_events("run-z", 0)
        assert [e.id for e in merged] == [1, 2, 3, 4, 5, 6]

    asyncio.run(scenario())


class UnreadableRetentionMeansDoNotDeleteTest(unittest.IsolatedAsyncioTestCase):
    """保留天数写歪了（0 或负数），一条都不删。

    变异验证（2026-08-29）：`max(0, keep_days)` 那个夹子没有任何测试。
    而它本身也不救命——负数被夹成 0，**而 0 恰恰是最狠的那个值**：
    cutoff 变成"现在"，除了每个 stream 的哨兵行，业务事件（审计线索）
    全部删掉，日志上只写一句"归档 N 行"。

    产物清理那一侧早就定了规矩：「看不懂的配置一律当成别删。
    这是删数据的地方该有的默认方向。」两处本该一个脾气，
    而事件这边一直是反的——同一个仓里两条删数据的路，
    对同一个 0 给出相反的解释，这本身就是个信号。

    第一版我加的是 ge=0（加载时报错），被产物那边的测试当场顶回来了：
    那条测试明写着"负数也不删"是一条安全属性。报错会把手误变成
    "服务起不来"；什么都不删则既安全又能继续服务。听它的。
    """

    async def _archive(self, keep_days: int):
        from pathlib import Path as _Path
        from tempfile import TemporaryDirectory

        from agent_platform.storage import Storage

        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage = Storage(_Path(tmp.name) / "d")
        await storage.initialize()
        for i in range(5):
            await storage.append_event("s1", "tick", {"n": i})
        with storage._connect() as conn:
            conn.execute("UPDATE events SET created_at='2020-01-01T00:00:00+00:00'")
        return storage, await storage.archive_events_before(keep_days=keep_days)

    async def test_zero_deletes_nothing(self):
        storage, result = await self._archive(0)
        self.assertEqual(result["removed"], 0, "keep_days=0 却动手删了")
        self.assertEqual(result["remaining"], 5)

    async def test_a_negative_window_deletes_nothing(self):
        _, result = await self._archive(-1)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["remaining"], 5)

    async def test_a_sane_window_still_archives(self):
        """别把闸关死：正常配置照样要删。

        没有这一条的话，"一律不删"也能让上面两条全绿。
        """
        _, result = await self._archive(7)
        self.assertGreater(result["removed"], 0)
        self.assertGreaterEqual(result["remaining"], 1, "哨兵行要留着")

    async def test_the_sentinel_row_survives_even_then(self):
        """每个 stream 留最大 seq 那行：全删会让新事件序号回退、和冷文件撞号。"""
        _, result = await self._archive(7)
        self.assertEqual(result["remaining"], 1)
