from __future__ import annotations

import time
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


def _create_simple_workflow(client: TestClient) -> str:
    app_id = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Worker workflow run", "requirement": "Run a simple workflow through worker."},
    ).json()["id"]
    revision = 0
    for node in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
    ]:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    mutate(
        client,
        app_id,
        revision,
        "add_edge",
        {
            "edge": {
                "id": "start-end",
                "source": "start",
                "target": "end",
                "source_port": "output",
                "target_port": "input",
            },
        },
    )
    return app_id


def _wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    run: dict[str, Any] = {}
    for _ in range(100):
        run = client.get(f"/api/v1/runs/{run_id}", headers=headers()).json()
        if run["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)
    return run


def test_v02_116_catalog_marks_workflow_run_implemented() -> None:
    settings = Settings(api_token="workflow-test")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        handlers = build_platform_worker_handlers(client.app.state.services)
        catalog = platform_worker_handler_catalog(handlers)
    entries = {entry["kind"]: entry for entry in catalog["entries"]}

    assert catalog["version"] == "v0.2.116"
    assert catalog["catalog_complete"] is True
    assert catalog["registered_catalog_complete"] is True
    assert catalog["full_execution_coverage"] is False
    assert entries["workflow_run"]["status"] == "implemented"
    assert entries["workflow_run"]["implementation"] == "workflow_run_handler"
    assert entries["workflow_run"]["executable"] is True
    remaining_unavailable = set(PLATFORM_WORKER_TASK_KINDS) - {
        "workflow_run",
        "scheduler_trigger",
        "scheduler_manual_trigger",
    }
    for kind in remaining_unavailable:
        assert entries[kind]["status"] == "unavailable"


def test_v02_116_workflow_run_worker_handler_creates_real_run(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = _create_simple_workflow(client)
        harness = client.app.state.services.harness

        async def queue_task() -> None:
            await harness.start_task(
                "worker-workflow-run-1",
                kind="workflow_run",
                owner_id=app_id,
                resource_id="worker-workflow-run-1",
                metadata={"inputs": {}, "use_draft": True, "workspace_path": "."},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease("worker-workflow-run-1", worker_id="producer", next_status="queued")

        client.portal.call(queue_task)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(client.app.state.services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(kind="workflow_run", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [("worker-workflow-run-1", "succeeded")]

        worker_task = client.portal.call(harness.get_task, "worker-workflow-run-1")
        run_id = worker_task.metadata["worker_runner"]["result"]["run_id"]
        run = _wait_for_run(client, run_id)
        assert run["status"] == "succeeded", run
        assert run["outputs"] == {"ok": True}

        run_task = client.portal.call(harness.get_task, run_id)
        assert run_task.kind == "workflow_run"
        assert run_task.parent_task_id == "worker-workflow-run-1"
        assert run_task.metadata["origin"] == "worker"
        assert isinstance(worker_task.metadata["worker_runner"]["result"]["draft_revision"], int)

        heartbeats = {row.worker_id: row for row in client.portal.call(harness.list_worker_heartbeats)}
        assert heartbeats["worker-a"].status == "idle"
        assert heartbeats["worker-a"].metadata["last_task_id"] == "worker-workflow-run-1"


def test_v02_116_existing_api_workflow_run_path_still_uses_api_origin(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = _create_simple_workflow(client)
        created = client.post(
            f"/api/v1/applications/{app_id}/runs",
            headers=headers(),
            json={"inputs": {}, "use_draft": True},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        run = _wait_for_run(client, run_id)
        assert run["status"] == "succeeded", run
        task = client.get(f"/api/v1/platform/harness/tasks/{run_id}", headers=headers()).json()
        assert task["metadata"]["origin"] == "api"
        assert task["parent_task_id"] is None
