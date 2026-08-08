"""Terminal harness tasks restart in place — resume-after-failure is first-class."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_platform.config import Settings
from agent_platform.platform_harness import PlatformHarness
from agent_platform.storage import Storage


async def _harness(tmp_path: Path) -> PlatformHarness:
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    settings.prepare()
    storage = Storage(settings.data_dir)
    await storage.initialize()
    return PlatformHarness(storage=storage)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["failed", "cancelled", "succeeded", "paused"])
async def test_terminal_tasks_restart_and_accept_usage(
    tmp_path: Path, terminal: str
) -> None:
    harness = await _harness(tmp_path)
    await harness.start_task(
        "build-1", kind="builder_build", owner_id="app-1", resource_id="build-1"
    )
    await harness.finish_task("build-1", status=terminal, error="boom")

    record = await harness.start_task(
        "build-1", kind="builder_build", owner_id="app-1", resource_id="build-1"
    )
    assert record.status == "running"
    assert record.error == ""
    assert record.finished_at is None
    # The old wall: "platform task is not running ... status=failed"
    usage = await harness.record_usage("build-1", "model_call")
    assert usage is not None


def test_worker_yields_when_build_already_running_elsewhere() -> None:
    """双执行者互斥：worker 撞"别处在跑"必须交还执行权，不许把任务打成 failed。

    ERP 盲测两次死亡的真凶：租约到期 requeue → worker 再认领 → 撞 API 进程
    正跑的构建 → finish(failed) → 正跑实例 record_usage 全线崩。
    """

    import asyncio

    from agent_platform.worker_runner import builder_build_handler

    class StubBuilder:
        class workflow_store:  # noqa: N801 - 只为满足属性访问
            @staticmethod
            async def get_build(build_id: str):
                raise AssertionError("不应该走到失败收集分支")

        @staticmethod
        async def run_claimed_build(build_id: str):
            raise RuntimeError("build is already running")

    class Task:
        id = "b-1"
        resource_id = "b-1"
        metadata: dict = {}

    handler = builder_build_handler(StubBuilder())
    result = asyncio.run(handler(Task()))
    assert result["status"] == "skipped_already_running"
