from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.platform_harness import PlatformHarness
from agent_platform.storage import Storage
from agent_platform.worker_runner import (
    PLATFORM_WORKER_TASK_KINDS,
    PlatformHarnessWorkerRunner,
    build_platform_worker_handlers,
    platform_worker_handler_catalog,
)
from tests.test_runtime import ScriptedProvider


def run(coro):
    return asyncio.run(coro)


class FakeScheduler:
    async def trigger_now(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"run_id": "fake-run", "status": "queued"}


class FakeWorkflowRuntime:
    async def create_run(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"run_id": "fake-workflow-run", "status": "queued", "version": 1, "draft_revision": None}

    async def run_test_suite(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"passed": True, "summary": {"total": 1, "failed": 0, "mandatory_failed": 0}, "tests": []}


class FakeServices:
    scheduler = FakeScheduler()
    workflow_runtime = FakeWorkflowRuntime()


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


def test_v02_110_catalog_covers_all_platform_task_kinds() -> None:
    handlers = build_platform_worker_handlers(FakeServices())
    catalog = platform_worker_handler_catalog(handlers)

    assert set(handlers) == set(PLATFORM_WORKER_TASK_KINDS)
    assert catalog["required_count"] == len(PLATFORM_WORKER_TASK_KINDS)
    assert catalog["catalog_complete"] is True
    assert catalog["registered_catalog_complete"] is True
    assert catalog["full_execution_coverage"] is False
    assert catalog["deterministic_gap_failure"] is True
    assert catalog["missing_required_kinds"] == []
    assert catalog["unregistered_required_kinds"] == []

    entries = {entry["kind"]: entry for entry in catalog["entries"]}
    assert entries["workflow_run"]["status"] == "implemented"
    assert entries["workflow_run"]["executable"] is True
    assert entries["test_suite"]["status"] == "implemented"
    assert entries["test_suite"]["executable"] is True
    assert entries["scheduler_trigger"]["status"] == "implemented"
    assert entries["scheduler_trigger"]["executable"] is True
    assert entries["scheduler_manual_trigger"]["status"] == "implemented"
    assert entries["scheduler_manual_trigger"]["executable"] is True
    for kind in set(PLATFORM_WORKER_TASK_KINDS) - {
        "workflow_run",
        "test_suite",
        "scheduler_trigger",
        "scheduler_manual_trigger",
    }:
        assert entries[kind]["status"] == "unavailable"
        assert entries[kind]["handler_registered"] is True
        assert entries[kind]["executable"] is False
        assert entries[kind]["operator_action"]


def test_v02_110_unavailable_catalog_handler_fails_deterministically(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await harness.start_task(
            "handler-catalog-benchmark-1",
            kind="benchmark",
            owner_id="owner-a",
            resource_id="case-a",
            worker_id="producer",
            lease_seconds=60,
        )
        await harness.release_task_lease(
            "handler-catalog-benchmark-1",
            worker_id="producer",
            next_status="queued",
        )

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(FakeServices()),
        )
        results = await runner.run_once(limit=5)

        assert [(item.task_id, item.status) for item in results] == [
            ("handler-catalog-benchmark-1", "failed")
        ]
        assert "worker handler unavailable: benchmark" in results[0].error
        finished = await harness.get_task("handler-catalog-benchmark-1")
        assert finished.status == "failed"
        assert finished.worker_id == "worker-a"
        assert "worker handler unavailable: benchmark" in finished.error
        assert finished.metadata["worker_runner"]["status"] == "failed"

    run(scenario())


def test_v02_110_worker_handler_catalog_api_exposes_coverage(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        response = client.get("/api/v1/platform/harness/worker-handler-catalog", headers=headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == "v0.2.118"
    assert body["catalog_complete"] is True
    assert body["registered_catalog_complete"] is True
    assert body["full_execution_coverage"] is False
    assert body["not_full_sidecar_completion"] is True
    assert body["missing_required_kinds"] == []
    assert {entry["kind"] for entry in body["entries"]} == set(PLATFORM_WORKER_TASK_KINDS)
