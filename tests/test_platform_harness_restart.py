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
