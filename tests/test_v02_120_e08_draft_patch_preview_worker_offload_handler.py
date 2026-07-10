from __future__ import annotations

from pathlib import Path

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


def _create_preview_workflow(client: TestClient) -> tuple[str, dict]:
    app_id = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Worker preview", "requirement": "Preview a draft patch through worker."},
    ).json()["id"]
    revision = 0
    for node in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
    ]:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    before = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
    return app_id, before


def test_v02_120_catalog_marks_draft_patch_preview_implemented() -> None:
    settings = Settings(api_token="workflow-test")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        handlers = build_platform_worker_handlers(client.app.state.services)
        catalog = platform_worker_handler_catalog(handlers)
    entries = {entry["kind"]: entry for entry in catalog["entries"]}

    assert catalog["version"] == "v0.2.122"
    assert catalog["catalog_complete"] is True
    assert catalog["registered_catalog_complete"] is True
    assert catalog["full_execution_coverage"] is False
    assert entries["draft_patch_preview"]["status"] == "implemented"
    assert entries["draft_patch_preview"]["implementation"] == "draft_patch_preview_handler"
    assert entries["draft_patch_preview"]["executable"] is True
    remaining_unavailable = set(PLATFORM_WORKER_TASK_KINDS) - {
        "workflow_run",
        "test_suite",
        "scheduler_trigger",
        "scheduler_manual_trigger",
        "draft_patch_preview",
        "benchmark",
    }
    assert remaining_unavailable == {"builder_build"}
    for kind in remaining_unavailable:
        assert entries[kind]["status"] == "unavailable"


def test_v02_120_draft_patch_preview_worker_handler_is_non_destructive(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id, before = _create_preview_workflow(client)
        harness = client.app.state.services.harness

        async def queue_task() -> None:
            await harness.start_task(
                "worker-preview-1",
                kind="draft_patch_preview",
                owner_id=app_id,
                resource_id=app_id,
                metadata={"instruction": "rename node end to Final Answer"},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease("worker-preview-1", worker_id="producer", next_status="queued")

        client.portal.call(queue_task)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(client.app.state.services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(kind="draft_patch_preview", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [("worker-preview-1", "succeeded")]

        worker_task = client.portal.call(harness.get_task, "worker-preview-1")
        result = worker_task.metadata["worker_runner"]["result"]
        assert result["supported"] is True
        assert result["intent"] == "rename_node"
        assert result["operations"][0]["op"] == "update_node"
        assert result["operations"][0]["expected_revision"] == before["revision"]

        after = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert after["revision"] == before["revision"]
        assert after["content_hash"] == before["content_hash"]

        heartbeats = {row.worker_id: row for row in client.portal.call(harness.list_worker_heartbeats)}
        assert heartbeats["worker-a"].status == "idle"
        assert heartbeats["worker-a"].metadata["last_task_id"] == "worker-preview-1"


def test_v02_120_draft_patch_preview_worker_handler_fails_unsupported(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id, _before = _create_preview_workflow(client)
        harness = client.app.state.services.harness

        async def queue_task() -> None:
            await harness.start_task(
                "worker-preview-unsupported-1",
                kind="draft_patch_preview",
                owner_id=app_id,
                resource_id=app_id,
                metadata={"instruction": "please invent a new workflow"},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(
                "worker-preview-unsupported-1",
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
            return await runner.run_once(kind="draft_patch_preview", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [
            ("worker-preview-unsupported-1", "failed")
        ]
        assert "worker draft_patch_preview unsupported" in results[0].error

        worker_task = client.portal.call(harness.get_task, "worker-preview-unsupported-1")
        assert worker_task.status == "failed"
        assert "worker draft_patch_preview unsupported" in worker_task.error


def test_v02_120_existing_api_preview_path_still_non_destructive(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id, before = _create_preview_workflow(client)
        response = client.post(
            f"/api/v1/applications/{app_id}/draft/preview-patch",
            headers=headers(),
            json={"instruction": "rename node end to Final Answer"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["supported"] is True
        after = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers()).json()
        assert after["revision"] == before["revision"]
        assert after["content_hash"] == before["content_hash"]
