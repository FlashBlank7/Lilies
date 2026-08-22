from pathlib import Path

import pytest

from agent_platform.models import AgentSpec
from agent_platform.storage import Storage


@pytest.mark.asyncio
async def test_agent_version_publish_and_event_replay(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    await storage.initialize()
    spec = AgentSpec(
        name="tester",
        description="Runs tests",
        system_prompt="Run tests, inspect failures, fix root causes, and verify the result.",
        tools=["Read", "Bash"],
    )
    version = await storage.save_agent_version(spec)
    await storage.publish_agent(spec.id, version)
    restored, restored_version, status = await storage.get_agent(spec.id)
    assert restored == spec
    assert restored_version == 1
    assert status == "published"

    first = await storage.append_event("stream", "one", {"value": 1})
    second = await storage.append_event("stream", "two", {"value": 2})
    assert first.id == 1
    assert second.id == 2
    replay = await storage.list_events("stream", after=1)
    assert [event.type for event in replay] == ["two"]
    assert (tmp_path / "events" / "stream.jsonl").exists()


@pytest.mark.asyncio
async def test_generation_record_roundtrip(tmp_path: Path) -> None:
    storage = Storage(tmp_path)
    await storage.initialize()
    await storage.create_generation("g1", "a sufficiently long requirement", "demo")
    await storage.update_generation("g1", status="generating")
    row = await storage.get_generation("g1")
    assert row["status"] == "generating"
    assert row["workspace_path"] == "demo"



def test_append_event_seq_allocation_is_atomic_under_thread_contention(tmp_path):
    """事件 seq 分配必须原子：取消/超时留下的 to_thread 孤儿线程与下一次写入并发时，
    "先读 MAX 再写"会撞出 UNIQUE 冲突（bagpipe 首次异构构建的死因）。多线程直接打
    同步写入路径（绕过 asyncio 锁）复现该窗口。"""
    import asyncio
    import threading

    storage = Storage(tmp_path)
    asyncio.run(storage.initialize())
    errors: list[Exception] = []

    def worker(n: int) -> None:
        for i in range(150):
            try:
                storage._append_event_sync("contended", "tick", {"w": n, "i": i})
            except Exception as error:  # noqa: BLE001 - 收集后统一断言
                errors.append(error)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, f"seq collisions: {errors[:3]}"
    seqs = [event.id for event in asyncio.run(storage.list_events("contended", 0))]
    assert len(seqs) == 600 and len(set(seqs)) == 600 and seqs == sorted(seqs)
