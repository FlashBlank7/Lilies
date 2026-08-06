from __future__ import annotations

import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.worker_runner import (
    PlatformHarnessWorkerRunner,
    build_platform_worker_handlers,
    platform_worker_handler_catalog,
)
from tests.test_workflow import IncrementalBuilderProvider, TimeoutBuilderProvider, headers


def _create_application(client: TestClient, *, name: str = "Worker build") -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": name, "requirement": "Build a tested greeting workflow."},
    )
    response.raise_for_status()
    return response.json()["id"]


def test_v02_124_catalog_marks_builder_build_implemented(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, IncrementalBuilderProvider())
    with TestClient(app) as client:
        handlers = build_platform_worker_handlers(client.app.state.services)
        catalog = platform_worker_handler_catalog(handlers)
    entries = {entry["kind"]: entry for entry in catalog["entries"]}

    assert catalog["version"] == "v0.2.124"
    assert catalog["catalog_complete"] is True
    assert catalog["registered_catalog_complete"] is True
    assert catalog["full_execution_coverage"] is True
    assert catalog["not_full_sidecar_completion"] is True
    assert catalog["unavailable_count"] == 0
    assert entries["builder_build"]["status"] == "implemented"
    assert entries["builder_build"]["implementation"] == "builder_build_handler"
    assert entries["builder_build"]["executable"] is True


def test_v02_124_builder_build_worker_publishes_existing_build(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, IncrementalBuilderProvider())
    with TestClient(app) as client:
        app_id = _create_application(client)
        services = client.app.state.services
        harness = services.harness
        build_id = str(uuid4())

        async def queue_build() -> None:
            await services.workflow_store.create_build(
                build_id,
                app_id,
                "Build a tested greeting workflow.",
                True,
                12,
                2,
            )
            await harness.start_task(
                build_id,
                kind="builder_build",
                owner_id=app_id,
                resource_id=build_id,
                metadata={"build_id": build_id, "auto_publish": True},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(build_id, worker_id="producer", next_status="queued")

        client.portal.call(queue_build)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(kind="builder_build", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [(build_id, "succeeded")]

        build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
        assert build["status"] == "published", build
        assert build["team_state"]["published_version"] == 1
        worker_task = client.portal.call(harness.get_task, build_id)
        result = worker_task.metadata["worker_runner"]["result"]
        assert worker_task.status == "succeeded"
        assert result["build_id"] == build_id
        assert result["status"] == "published"
        assert worker_task.usage_counts["model_call"] >= 1
        events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        assert any(event["type"] == "build.completed" for event in events)


def test_v02_124_builder_build_worker_failure_preserves_build_error(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, TimeoutBuilderProvider())
    with TestClient(app) as client:
        app_id = _create_application(client, name="Worker build failure")
        services = client.app.state.services
        harness = services.harness
        build_id = str(uuid4())

        async def queue_build() -> None:
            await services.workflow_store.create_build(
                build_id,
                app_id,
                "Build a workflow that times out.",
                False,
                5,
                1,
            )
            await harness.start_task(
                build_id,
                kind="builder_build",
                owner_id=app_id,
                resource_id=build_id,
                metadata={"build_id": build_id, "auto_publish": False},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(build_id, worker_id="producer", next_status="queued")

        client.portal.call(queue_build)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(kind="builder_build", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [(build_id, "failed")]
        assert "worker builder_build failed" in results[0].error

        build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
        assert build["status"] == "needs_attention"
        assert "timed out" in build["error"]
        worker_task = client.portal.call(harness.get_task, build_id)
        result = worker_task.metadata["worker_runner"]["result"]
        assert worker_task.status == "failed"
        assert result["build_id"] == build_id
        assert result["status"] == "needs_attention"
        assert "timed out" in result["error"]


def test_v02_124_existing_api_build_path_still_publishes(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, IncrementalBuilderProvider())
    with TestClient(app) as client:
        app_id = _create_application(client, name="API build")
        response = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={
                "requirement": "Build a tested greeting workflow.",
                "auto_publish": True,
                "planning_mode": "disabled",
            },
        )
        assert response.status_code == 202, response.text
        build_id = response.json()["build_id"]
        build = {}
        for _ in range(300):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"published", "needs_attention"}:
                break
            time.sleep(0.01)

        assert build["status"] == "published", build
        task = client.portal.call(client.app.state.services.harness.get_task, build_id)
        assert task.status == "succeeded"
