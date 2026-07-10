from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.worker_runner import (
    PLATFORM_WORKER_TASK_KINDS,
    PlatformHarnessWorkerRunner,
    build_platform_worker_handlers,
    platform_worker_handler_catalog,
)
from tests.test_runtime import ScriptedProvider
from tests.test_workflow import headers, mutate


class FakeScheduler:
    async def trigger_now(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {"run_id": "fake-run", "status": "queued"}

    async def execute_claimed_schedule_fire(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {
            "application_id": "fake-app",
            "version": 1,
            "node_id": "schedule",
            "local_date": "2026-06-24",
            "run_id": "fake-run",
        }


class FakeServices:
    scheduler = FakeScheduler()


def _create_scheduled_application(client: TestClient, *, name: str = "Worker schedule") -> str:
    app_id = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": name, "requirement": "Run every day at 08:00 Tokyo time."},
    ).json()["id"]
    revision = 0
    for node in [
        {
            "id": "schedule",
            "type": "schedule_trigger",
            "title": "08:00 JST",
            "config": {
                "timezone": "Asia/Tokyo",
                "hour": 8,
                "minute": 0,
                "inputs": {"topic": "idols"},
            },
        },
        {
            "id": "end",
            "type": "end",
            "title": "End",
            "config": {
                "outputs": {
                    "topic": {"$ref": {"node_id": "schedule", "path": ["topic"]}},
                },
            },
        },
    ]:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    revision = mutate(
        client,
        app_id,
        revision,
        "add_edge",
        {
            "edge": {
                "id": "scheduled-end",
                "source": "schedule",
                "target": "end",
                "source_port": "output",
                "target_port": "input",
            },
        },
    )
    revision = mutate(
        client,
        app_id,
        revision,
        "add_test",
        {
            "test": {
                "name": "Scheduled inputs",
                "requirement": "Schedule defaults reach the result.",
                "inputs": {},
                "assertions": [{"path": ["topic"], "operator": "equals", "expected": "idols"}],
            },
        },
    )
    assert client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers()).json()["passed"]
    assert client.post(f"/api/v1/applications/{app_id}/versions", headers=headers()).status_code == 200
    return app_id


def _wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    run: dict[str, Any] = {}
    for _ in range(100):
        run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
        if run["status"] == "succeeded":
            break
        time.sleep(0.01)
    return run


def test_v02_114_catalog_marks_scheduler_trigger_implemented() -> None:
    handlers = build_platform_worker_handlers(FakeServices())
    catalog = platform_worker_handler_catalog(handlers)
    entries = {entry["kind"]: entry for entry in catalog["entries"]}

    assert catalog["version"] == "v0.2.114"
    assert catalog["catalog_complete"] is True
    assert catalog["registered_catalog_complete"] is True
    assert catalog["full_execution_coverage"] is False
    assert catalog["not_full_sidecar_completion"] is True
    assert entries["scheduler_trigger"]["status"] == "implemented"
    assert entries["scheduler_trigger"]["implementation"] == "scheduler_trigger_handler"
    assert entries["scheduler_trigger"]["executable"] is True
    assert entries["scheduler_manual_trigger"]["status"] == "implemented"

    remaining_unavailable = set(PLATFORM_WORKER_TASK_KINDS) - {
        "scheduler_trigger",
        "scheduler_manual_trigger",
    }
    for kind in remaining_unavailable:
        assert entries[kind]["status"] == "unavailable"
        assert entries[kind]["executable"] is False


def test_v02_114_scheduler_trigger_worker_handler_runs_claimed_schedule_fire(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = _create_scheduled_application(client)
        harness = client.app.state.services.harness
        workflow_store = client.app.state.services.workflow_store
        assert client.portal.call(workflow_store.claim_schedule_fire, app_id, 1, "schedule", "2026-06-24")

        async def queue_task() -> None:
            await harness.start_task(
                "worker-scheduler-trigger-1",
                kind="scheduler_trigger",
                owner_id=app_id,
                resource_id="worker-scheduler-trigger-1",
                metadata={
                    "version": 1,
                    "node_id": "schedule",
                    "local_date": "2026-06-24",
                    "timezone": "Asia/Tokyo",
                    "triggered_at": "2026-06-23T23:00:00+00:00",
                },
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(
                "worker-scheduler-trigger-1",
                worker_id="producer",
                next_status="queued",
            )

        client.portal.call(queue_task)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(client.app.state.services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [
            ("worker-scheduler-trigger-1", "succeeded")
        ]

        task = client.portal.call(harness.get_task, "worker-scheduler-trigger-1")
        result = task.metadata["worker_runner"]["result"]
        run = _wait_for_run(client, result["run_id"])
        assert run["outputs"] == {"topic": "idols"}
        assert task.usage_counts["scheduler_fire"] == 1
        assert result["application_id"] == app_id
        assert result["node_id"] == "schedule"
        assert result["local_date"] == "2026-06-24"

        heartbeats = {row.worker_id: row for row in client.portal.call(harness.list_worker_heartbeats)}
        assert heartbeats["worker-a"].status == "idle"
        assert heartbeats["worker-a"].metadata["last_task_id"] == "worker-scheduler-trigger-1"
        assert heartbeats["worker-a"].metadata["last_task_status"] == "succeeded"


def test_v02_114_scheduler_tick_can_queue_automatic_trigger_for_worker(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
        scheduler_worker_offload_enabled=True,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = _create_scheduled_application(client, name="Offloaded schedule")
        scheduler = client.app.state.services.scheduler
        harness = client.app.state.services.harness
        workflow_store = client.app.state.services.workflow_store

        started = client.portal.call(
            scheduler.tick,
            datetime(2026, 6, 23, 23, 0, tzinfo=timezone.utc),
        )
        assert started == [
            {
                "application_id": app_id,
                "version": 1,
                "node_id": "schedule",
                "local_date": "2026-06-24",
                "task_id": f"scheduler:{app_id}:1:schedule:2026-06-24",
                "queued": True,
            }
        ]
        fires = client.portal.call(workflow_store.list_schedule_fires, app_id)
        assert fires[0]["run_id"] is None
        async def list_queued_scheduler_tasks():
            return await harness.list_tasks(
                kind="scheduler_trigger",
                status="queued",
                owner_id=app_id,
            )

        queued = client.portal.call(list_queued_scheduler_tasks)
        assert [task.id for task in queued] == [f"scheduler:{app_id}:1:schedule:2026-06-24"]

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(client.app.state.services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(kind="scheduler_trigger", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [
            (f"scheduler:{app_id}:1:schedule:2026-06-24", "succeeded")
        ]
        completed_fires = client.portal.call(workflow_store.list_schedule_fires, app_id)
        assert completed_fires[0]["run_id"]
        run = _wait_for_run(client, completed_fires[0]["run_id"])
        assert run["outputs"] == {"topic": "idols"}
