"""删了行不等于腾了磁盘——这件事要报出来。

SQLite 的 DELETE 只把页标成空闲，文件一个字节都不会变小
（本仓 auto_vacuum=0，代码里也没有任何 VACUUM）。
事件归档一次清掉几十万行之后，日志写着"移除 N 行"而 `ls -lh` 纹丝不动，
运维只会以为归档没起作用。

真机 2026-08-29：库 978 MB，其中 845 MB 是
platform_harness.usage.recorded——那是**一个已经修好的 bug** 的存量
（当时每条事件都把该任务至今的全部用量明细抄一份，平方级增长；
一个任务 1006 条事件写了 70 MB）。修完不再长，但存量还在，
而且要等它过了 7 天保留期才轮得到清。

只报数、不动手：VACUUM 要重写整库、拿排他锁、临时占两倍磁盘，
那是人挑时间做的事（scripts/compact_db.py）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_platform.storage import Storage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    made = Storage(tmp_path / "d")
    asyncio.run(made.initialize())
    return made


def _fill(storage: Storage, rows: int) -> None:
    async def write() -> None:
        for index in range(rows):
            await storage.append_event(f"s{index % 5}", "noise",
                                       {"blob": "x" * 2_000})
    asyncio.run(write())


class TestItReportsBothNumbers:
    def test_a_fresh_database_has_a_size_and_little_free_space(self, storage):
        space = asyncio.run(storage.database_space())
        assert space["bytes"] > 0
        assert space["reclaimable_bytes"] <= space["bytes"]

    def test_deleting_rows_shows_up_as_reclaimable_not_smaller(self, storage):
        """这一条就是整件事：删完之后**库没变小**，空闲的那部分要报出来。"""
        _fill(storage, 400)
        before = asyncio.run(storage.database_space())
        with storage._connect() as conn:
            conn.execute("DELETE FROM events WHERE event_type='noise'")
        after = asyncio.run(storage.database_space())

        assert after["bytes"] >= before["bytes"] * 0.9, "文件不该因为 DELETE 变小"
        assert after["reclaimable_bytes"] > before["reclaimable_bytes"], (
            f"删了几百行却没多出空闲页：{before} → {after}")

    def test_reclaimable_is_reported_in_bytes_not_pages(self, storage):
        """报字节，不报页数——页数对运维没有意义，还容易被当成行数。"""
        _fill(storage, 400)
        with storage._connect() as conn:
            conn.execute("DELETE FROM events WHERE event_type='noise'")
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        space = asyncio.run(storage.database_space())
        assert space["reclaimable_bytes"] == page_size * free
        assert space["reclaimable_bytes"] > free, "看着像页数就不对了"
