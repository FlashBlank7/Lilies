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


def _create_tested_workflow(client: TestClient) -> str:
    app_id = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Worker test suite", "requirement": "Run tests through worker."},
    ).json()["id"]
    revision = 0
    for node in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
    ]:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    revision = mutate(
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
    mutate(
        client,
        app_id,
        revision,
        "add_test",
        {
            "test": {
                "name": "Ok",
                "requirement": "Workflow returns ok.",
                "inputs": {},
                "assertions": [{"path": ["ok"], "operator": "equals", "expected": True}],
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


def test_v02_118_catalog_marks_test_suite_implemented() -> None:
    settings = Settings(api_token="workflow-test")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        handlers = build_platform_worker_handlers(client.app.state.services)
        catalog = platform_worker_handler_catalog(handlers)
    entries = {entry["kind"]: entry for entry in catalog["entries"]}

    assert catalog["version"] == "v0.2.124"
    assert catalog["catalog_complete"] is True
    assert catalog["registered_catalog_complete"] is True
    assert catalog["full_execution_coverage"] is True
    assert entries["test_suite"]["status"] == "implemented"
    assert entries["test_suite"]["implementation"] == "test_suite_handler"
    assert entries["test_suite"]["executable"] is True
    remaining_unavailable = set(PLATFORM_WORKER_TASK_KINDS) - {
        "workflow_run",
        "builder_build",
        "test_suite",
        "scheduler_trigger",
        "scheduler_manual_trigger",
        "draft_patch_preview",
        "benchmark",
    }
    assert remaining_unavailable == set()
    for kind in remaining_unavailable:
        assert entries[kind]["status"] == "unavailable"


def test_v02_118_test_suite_worker_handler_runs_existing_test_suite(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = _create_tested_workflow(client)
        harness = client.app.state.services.harness

        async def queue_task() -> None:
            await harness.start_task(
                "worker-test-suite-1",
                kind="test_suite",
                owner_id=app_id,
                resource_id=app_id,
                metadata={},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease("worker-test-suite-1", worker_id="producer", next_status="queued")

        client.portal.call(queue_task)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(client.app.state.services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(kind="test_suite", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [("worker-test-suite-1", "succeeded")]

        worker_task = client.portal.call(harness.get_task, "worker-test-suite-1")
        result = worker_task.metadata["worker_runner"]["result"]
        assert result["passed"] is True
        assert result["total"] == 1
        assert result["mandatory_failed"] == 0
        assert len(result["test_run_ids"]) == 1

        run_id = result["test_run_ids"][0]
        run = _wait_for_run(client, run_id)
        assert run["status"] == "succeeded", run
        assert run["outputs"] == {"ok": True}
        run_task = client.portal.call(harness.get_task, run_id)
        assert run_task.parent_task_id == "worker-test-suite-1"
        assert run_task.metadata["origin"] == "test_suite"

        heartbeats = {row.worker_id: row for row in client.portal.call(harness.list_worker_heartbeats)}
        assert heartbeats["worker-a"].status == "idle"
        assert heartbeats["worker-a"].metadata["last_task_id"] == "worker-test-suite-1"


def test_v02_118_existing_api_test_suite_path_still_manages_task(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = _create_tested_workflow(client)
        report = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        assert report.status_code == 200, report.text
        assert report.json()["passed"] is True
        tasks = client.get(
            f"/api/v1/platform/harness/tasks?kind=test_suite&owner_id={app_id}",
            headers=headers(),
        ).json()
        assert len(tasks) == 1
        assert tasks[0]["status"] == "succeeded"
        assert tasks[0]["metadata"]["origin"] == "test_suite"
