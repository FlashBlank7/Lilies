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
from tests.test_workflow import headers


def _reference_workflow() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {
                "id": "permission",
                "type": "permission_gate",
                "title": "Permission",
                "config": {
                    "input": {"$ref": {"node_id": "start", "path": ["output"]}},
                    "settings": {"auto_approve": True},
                },
            },
            {
                "id": "end",
                "type": "end",
                "title": "End",
                "config": {"outputs": {"ok": {"$ref": {"node_id": "permission", "path": ["output"]}}}},
            },
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "permission", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "permission", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }


def _missing_harness_workflow() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
            {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
        ],
        "edges": [
            {"id": "a", "source": "start", "target": "end", "source_port": "output", "target_port": "input"},
        ],
    }


def _passing_case(name: str = "complete harness") -> dict:
    reference = _reference_workflow()
    return {
        "name": name,
        "reference": reference,
        "candidate": reference,
        "required_harness_nodes": ["permission_gate"],
    }


def _failing_suite() -> dict:
    reference = _reference_workflow()
    return {
        "name": "worker benchmark suite",
        "minimum_score": 0.8,
        "minimum_pass_rate": 1.0,
        "cases": [
            _passing_case(),
            {
                "name": "missing harness",
                "reference": reference,
                "candidate": _missing_harness_workflow(),
                "required_harness_nodes": ["permission_gate"],
            },
        ],
    }


def test_v02_122_catalog_marks_benchmark_implemented() -> None:
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
    assert entries["benchmark"]["status"] == "implemented"
    assert entries["benchmark"]["implementation"] == "benchmark_handler"
    assert entries["benchmark"]["executable"] is True
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


def test_v02_122_benchmark_worker_case_succeeds(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        harness = client.app.state.services.harness

        async def queue_task() -> None:
            await harness.start_task(
                "worker-benchmark-case-1",
                kind="benchmark",
                owner_id="builder-benchmark",
                resource_id="complete harness",
                metadata={"case_payload": _passing_case()},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease("worker-benchmark-case-1", worker_id="producer", next_status="queued")

        client.portal.call(queue_task)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(client.app.state.services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(kind="benchmark", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [("worker-benchmark-case-1", "succeeded")]

        worker_task = client.portal.call(harness.get_task, "worker-benchmark-case-1")
        result = worker_task.metadata["worker_runner"]["result"]
        assert worker_task.status == "succeeded"
        assert result["mode"] == "case"
        assert result["passed"] is True
        assert result["report"]["passed"] is True
        assert result["missing"] == {"node_types": [], "tool_nodes": [], "harness_nodes": []}


def test_v02_122_benchmark_worker_suite_failure_preserves_report(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        harness = client.app.state.services.harness

        async def queue_task() -> None:
            await harness.start_task(
                "worker-benchmark-suite-1",
                kind="benchmark",
                owner_id="builder-benchmark-suite",
                resource_id="worker benchmark suite",
                metadata={"suite_payload": _failing_suite()},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease("worker-benchmark-suite-1", worker_id="producer", next_status="queued")

        client.portal.call(queue_task)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="worker-a",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(client.app.state.services),
        )

        async def run_worker_once_for_test():
            return await runner.run_once(kind="benchmark", limit=5)

        results = client.portal.call(run_worker_once_for_test)
        assert [(item.task_id, item.status) for item in results] == [("worker-benchmark-suite-1", "failed")]
        assert "worker benchmark suite failed" in results[0].error

        worker_task = client.portal.call(harness.get_task, "worker-benchmark-suite-1")
        result = worker_task.metadata["worker_runner"]["result"]
        assert worker_task.status == "failed"
        assert worker_task.usage_counts["node_execution"] == 2
        assert result["mode"] == "suite"
        assert result["passed"] is False
        assert result["failed_cases"] == ["missing harness"]
        assert result["report"]["pass_rate"] == 0.5


def test_v02_122_existing_api_benchmark_path_and_history_still_work(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/builder-benchmark/evaluate",
            headers=headers(),
            json=_passing_case("api complete harness"),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["report"]["passed"] is True

        history = client.get(
            "/api/v1/builder-benchmark/history?owner_id=builder-benchmark",
            headers=headers(),
        )
        assert history.status_code == 200, history.text
        records = history.json()
        assert any(item["id"] == body["task_id"] for item in records)
        task = next(item for item in records if item["id"] == body["task_id"])
        assert task["owner_id"] == "builder-benchmark"
        assert task["resource_id"] == "api complete harness"
        assert task["status"] == "succeeded"
