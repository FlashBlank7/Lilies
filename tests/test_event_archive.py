"""事件冷归档：DB 只留近期，JSONL 冷文件是权威全量，读取端无感回退。

三条铁律：
1. 归档后 list_events 返回的事件一条不少（冷热合并）；
2. 每个 stream 保留最大 seq 哨兵——新事件序号永不回退撞号；
3. 未过期与活跃 stream 不受影响。
"""

from __future__ import annotations

import asyncio
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
