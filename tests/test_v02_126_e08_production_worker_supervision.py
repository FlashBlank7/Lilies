from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.platform_harness import PlatformHarness
from agent_platform.storage import Storage
from agent_platform.worker_runner import PlatformHarnessWorkerRunner, PlatformWorkerSupervisor
from tests.test_runtime import ScriptedProvider


def run(coro):
    return asyncio.run(coro)


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


def test_v02_126_supervisor_starts_runs_and_stops_worker_loop(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await harness.start_task(
            "supervised-task-1",
            kind="scheduler_manual_trigger",
            owner_id="owner-a",
            resource_id="schedule-a",
            worker_id="producer",
            lease_seconds=60,
        )
        await harness.release_task_lease("supervised-task-1", worker_id="producer", next_status="queued")

        handled = asyncio.Event()

        async def handler(record):
            handled.set()
            return {"handled_task_id": record.id}

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="supervised-worker-a",
            lease_seconds=60,
            handlers={"scheduler_manual_trigger": handler},
        )
        supervisor = PlatformWorkerSupervisor(runner=runner, poll_seconds=0.01, limit=5)

        started = await supervisor.start()
        assert started["desired_state"] == "running"
        assert started["loop_running"] is True
        assert started["supervision_mode"] == "in_process_worker_loop"

        await asyncio.wait_for(handled.wait(), timeout=1.0)
        await asyncio.sleep(0.02)
        task = await harness.get_task("supervised-task-1")
        assert task.status == "succeeded"

        stopped = await supervisor.stop()
        assert stopped["desired_state"] == "stopped"
        assert stopped["loop_running"] is False
        assert stopped["run_count"] >= 1
        assert stopped["recent_results"][0]["task_id"] == "supervised-task-1"
        assert stopped["recent_results"][0]["status"] == "succeeded"
        assert stopped["boundaries"]["distributed_queue_semantics"] is False
        assert stopped["boundaries"]["external_process_manager"] is False
        assert stopped["boundaries"]["external_kms_provider_integration"] is False
        assert stopped["boundaries"]["full_sidecar_completion_claimed"] is False

    run(scenario())


def test_v02_126_worker_supervision_api_start_observe_stop(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_worker_id="api-supervised-worker",
        platform_harness_worker_supervision_poll_seconds=0.01,
        platform_harness_worker_supervision_limit=2,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        initial = client.get("/api/v1/platform/harness/worker-supervision", headers=headers())
        assert initial.status_code == 200, initial.text
        assert initial.json()["desired_state"] == "stopped"
        assert initial.json()["supports_start"] is True
        assert initial.json()["supports_stop"] is True

        started = client.post(
            "/api/v1/platform/harness/worker-supervision/start",
            headers=headers(),
            json={"poll_seconds": 0.01, "limit": 2},
        )
        assert started.status_code == 200, started.text
        started_body = started.json()
        assert started_body["desired_state"] == "running"
        assert started_body["loop_running"] is True
        assert started_body["worker_id"] == "api-supervised-worker"
        assert started_body["boundaries"]["distributed_queue_semantics"] is False
        assert started_body["boundaries"]["full_sidecar_completion_claimed"] is False

        observed = client.get("/api/v1/platform/harness/worker-supervision", headers=headers())
        assert observed.status_code == 200, observed.text
        assert observed.json()["desired_state"] == "running"
        assert observed.json()["supervision_mode"] == "in_process_worker_loop"

        stopped = client.post("/api/v1/platform/harness/worker-supervision/stop", headers=headers())
        assert stopped.status_code == 200, stopped.text
        stopped_body = stopped.json()
        assert stopped_body["desired_state"] == "stopped"
        assert stopped_body["loop_running"] is False
        assert stopped_body["observed_state"] == "idle"

        heartbeats = client.get("/api/v1/platform/harness/worker-heartbeats", headers=headers())
        assert heartbeats.status_code == 200, heartbeats.text
        rows = {row["worker_id"]: row for row in heartbeats.json()}
        assert rows["api-supervised-worker"]["metadata"]["phase"] == "supervisor_stopped"
