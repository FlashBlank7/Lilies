from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.platform_harness import PlatformHarness
from agent_platform.storage import Storage
from agent_platform.worker_runner import PlatformHarnessWorkerRunner
from tests.test_runtime import ScriptedProvider


def run(coro):
    return asyncio.run(coro)


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


def test_v02_112_worker_heartbeats_persist_and_classify_liveness(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        heartbeat = await harness.record_worker_heartbeat(
            worker_id="worker-a",
            status="running",
            active_task_id="task-a",
            stale_after_seconds=60,
            metadata={"phase": "unit"},
        )
        assert heartbeat.liveness == "active"

        restarted = PlatformHarness(storage=storage, worker_lease_seconds=60)
        rows = await restarted.list_worker_heartbeats()
        assert len(rows) == 1
        assert rows[0].worker_id == "worker-a"
        assert rows[0].status == "running"
        assert rows[0].active_task_id == "task-a"
        assert rows[0].metadata["phase"] == "unit"
        assert rows[0].liveness == "active"

        await restarted.record_worker_heartbeat(
            worker_id="worker-stale",
            status="idle",
            stale_after_seconds=0.001,
            metadata={"phase": "stale-check"},
        )
        await asyncio.sleep(0.01)
        stale_rows = {row.worker_id: row for row in await restarted.list_worker_heartbeats()}
        assert stale_rows["worker-stale"].liveness == "stale"

    run(scenario())


def test_v02_112_worker_runner_updates_heartbeat_registry(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await harness.start_task(
            "heartbeat-runner-1",
            kind="scheduler_manual_trigger",
            owner_id="owner-a",
            resource_id="schedule-a",
            worker_id="producer",
            lease_seconds=60,
        )
        await harness.release_task_lease("heartbeat-runner-1", worker_id="producer", next_status="queued")

        async def handler(record):
            running_rows = {row.worker_id: row for row in await harness.list_worker_heartbeats()}
            assert running_rows["worker-a"].status == "running"
            assert running_rows["worker-a"].active_task_id == record.id
            return {"handled": True}

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers={"scheduler_manual_trigger": handler},
        )
        results = await runner.run_once(limit=5)

        assert [(item.task_id, item.status) for item in results] == [("heartbeat-runner-1", "succeeded")]
        rows = {row.worker_id: row for row in await harness.list_worker_heartbeats()}
        assert rows["worker-a"].status == "idle"
        assert rows["worker-a"].active_task_id == ""
        assert rows["worker-a"].metadata["last_task_id"] == "heartbeat-runner-1"
        assert rows["worker-a"].metadata["last_task_status"] == "succeeded"
        assert rows["worker-a"].liveness == "active"

    run(scenario())


def test_v02_112_worker_heartbeat_api_exposes_liveness(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        async def record() -> None:
            await client.app.state.services.harness.record_worker_heartbeat(
                worker_id="api-worker",
                status="idle",
                stale_after_seconds=60,
                metadata={"phase": "api"},
            )

        client.portal.call(record)
        response = client.get("/api/v1/platform/harness/worker-heartbeats", headers=headers())

    assert response.status_code == 200, response.text
    body = response.json()
    assert body[0]["worker_id"] == "api-worker"
    assert body[0]["status"] == "idle"
    assert body[0]["liveness"] == "active"
    assert body[0]["metadata"]["phase"] == "api"
