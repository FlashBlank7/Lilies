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


@pytest.mark.asyncio
async def test_revived_task_budget_counts_per_segment(tmp_path: Path) -> None:
    """预算按工作段计：返修 N 段的长寿命任务每段有完整预算，审计账保留全量。

    ERP 盲测处决案：build 跨 7 段累计 204 次工具调用，任务级预算按
    生命周期封顶——第 201 次记账当场处决，后续调用全撞
    "platform task is not running"。
    """

    harness = await _harness(tmp_path)
    harness.max_tool_calls_per_task = 5
    await harness.start_task("b-1", kind="builder_build", owner_id="app", resource_id="b-1")
    for _ in range(5):
        await harness.record_usage("b-1", "tool_call")
    await harness.finish_task("b-1", status="failed", error="boom")

    # 复活 = 新工作段：基线重置，新段又有 5 次预算
    await harness.start_task("b-1", kind="builder_build", owner_id="app", resource_id="b-1")
    for _ in range(5):
        await harness.record_usage("b-1", "tool_call")
    task = await harness.get_task("b-1")
    assert task.status == "running", task.error
    # 审计账保留全生命周期
    assert task.usage_counts["tool_call"] == 10
