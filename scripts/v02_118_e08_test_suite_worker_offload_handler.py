#!/usr/bin/env python3
"""Generate v0.2.118 E08 test_suite worker offload evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.118_e08_test_suite_worker_offload_handler"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def mutate(client: Any, app_id: str, revision: int, op: str, data: dict[str, Any]) -> int:
    response = client.post(
        f"/api/v1/applications/{app_id}/draft",
        headers=headers(),
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )
    if response.status_code != 200:
        raise RuntimeError(response.text)
    return int(response.json()["revision"])


def create_tested_workflow(client: Any) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "v0.2.118 test suite evidence", "requirement": "Run test suite through worker."},
    )
    response.raise_for_status()
    app_id = response.json()["id"]
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


def wait_for_run(client: Any, run_id: str) -> dict[str, Any]:
    run: dict[str, Any] = {}
    for _ in range(100):
        response = client.get(f"/api/v1/runs/{run_id}", headers=headers())
        response.raise_for_status()
        run = response.json()
        if run["status"] in {"succeeded", "failed"}:
            return run
        time.sleep(0.01)
    return run


def verify_contract() -> dict[str, Any]:
    _prepare_imports()

    from fastapi.testclient import TestClient  # pylint: disable=import-error,import-outside-toplevel

    from agent_platform.api import create_app  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.config import Settings  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.worker_runner import (  # pylint: disable=import-error,import-outside-toplevel
        PlatformHarnessWorkerRunner,
        build_platform_worker_handlers,
        platform_worker_handler_catalog,
    )
    from tests.test_runtime import ScriptedProvider  # pylint: disable=import-error,import-outside-toplevel

    data_dir = ROOT / ".tmp" / "v02_118_e08_test_suite_worker_offload_handler"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    settings = Settings(
        api_token="workflow-test",
        data_dir=data_dir / "data",
        workspace_root=data_dir / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = create_tested_workflow(client)
        services = client.app.state.services
        harness = services.harness
        handlers = build_platform_worker_handlers(services)
        catalog = platform_worker_handler_catalog(handlers)
        entries = {entry["kind"]: entry for entry in catalog["entries"]}

        async def queue_task() -> None:
            await harness.start_task(
                "evidence-test-suite-worker-task",
                kind="test_suite",
                owner_id=app_id,
                resource_id=app_id,
                metadata={},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(
                "evidence-test-suite-worker-task",
                worker_id="producer",
                next_status="queued",
            )

        client.portal.call(queue_task)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="v02-118-worker",
            lease_seconds=60,
            handlers=handlers,
        )

        async def run_worker_once():
            return await runner.run_once(kind="test_suite", limit=5)

        results = client.portal.call(run_worker_once)
        worker_task = client.portal.call(harness.get_task, "evidence-test-suite-worker-task")
        worker_result = worker_task.metadata["worker_runner"]["result"]
        run_id = worker_result["test_run_ids"][0]
        run = wait_for_run(client, run_id)
        run_task = client.portal.call(harness.get_task, run_id)
        heartbeats = {
            row.worker_id: row.model_dump(mode="json")
            for row in client.portal.call(harness.list_worker_heartbeats)
        }

        api_report = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
        api_report.raise_for_status()
        api_tasks = client.get(
            f"/api/v1/platform/harness/tasks?kind=test_suite&owner_id={app_id}",
            headers=headers(),
        )
        api_tasks.raise_for_status()

    remaining_unavailable = [
        entry["kind"]
        for entry in catalog["entries"]
        if entry["status"] == "unavailable"
    ]
    checks = {
        "test_suite_catalog_implemented": entries["test_suite"]["status"] == "implemented"
        and entries["test_suite"]["executable"] is True,
        "worker_completed_queued_test_suite_task": len(results) == 1
        and results[0].status == "succeeded"
        and worker_task.status == "succeeded",
        "worker_returned_existing_test_report_shape": worker_result["passed"] is True
        and worker_result["total"] == 1
        and worker_result["mandatory_failed"] == 0,
        "per_test_workflow_run_parented_to_worker_task": run_task.parent_task_id == "evidence-test-suite-worker-task"
        and run_task.metadata["origin"] == "test_suite",
        "per_test_workflow_run_succeeded": run.get("status") == "succeeded"
        and run.get("outputs") == {"ok": True},
        "api_test_suite_path_preserved": api_report.json()["passed"] is True
        and any(task["metadata"].get("origin") == "test_suite" for task in api_tasks.json()),
        "heartbeat_registry_preserved": heartbeats["v02-118-worker"]["status"] == "idle"
        and heartbeats["v02-118-worker"]["metadata"]["last_task_status"] == "succeeded",
        "remaining_catalog_gaps_still_unavailable": set(remaining_unavailable)
        == {"builder_build", "benchmark", "draft_patch_preview"},
        "full_execution_coverage_not_claimed": catalog["full_execution_coverage"] is False
        and catalog["not_full_sidecar_completion"] is True,
    }
    return {
        "version": "v0.2.118",
        "evidence_id": "e08_test_suite_worker_offload_handler",
        "source_stage_report": "docs/stage-reports/v0.2.117_e08_remaining_sidecar_slice_reselection.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "catalog_summary": {
            key: catalog[key]
            for key in (
                "version",
                "required_count",
                "cataloged_count",
                "implemented_count",
                "unavailable_count",
                "catalog_complete",
                "registered_catalog_complete",
                "full_execution_coverage",
                "not_full_sidecar_completion",
            )
        },
        "test_suite_entry": entries["test_suite"],
        "remaining_unavailable": remaining_unavailable,
        "worker_task": worker_task.model_dump(mode="json"),
        "per_test_run_task": run_task.model_dump(mode="json"),
        "workflow_run": {"id": run.get("id"), "status": run.get("status"), "outputs": run.get("outputs")},
        "api_test_suite_task_count": len(api_tasks.json()),
        "heartbeats": heartbeats,
        "implementation_paths": [
            "platform/backend/src/agent_platform/workflow_runtime.py",
            "platform/backend/src/agent_platform/worker_runner.py",
            "tests/test_v02_118_e08_test_suite_worker_offload_handler.py",
        ],
        "invariants": {
            "api_test_suite_path_preserved": True,
            "process_supervision_implemented": False,
            "distributed_queue_implemented": False,
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
        },
        "next_boundary": (
            "This closes the test_suite worker offload handler only. Full Platform Harness sidecar completion "
            "still needs remaining worker-owned handlers and production worker supervision."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.118 E08 test_suite worker offload handler",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Test suite status: `{result['test_suite_entry']['status']}`",
        f"- Catalog full execution coverage: `{result['catalog_summary']['full_execution_coverage']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Next boundary: {result['next_boundary']}",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
    ]
    for name, value in result["checks"].items():
        lines.append(f"| `{name}` | `{value}` |")
    lines.extend(["", "## Worker Result", ""])
    lines.append(f"- Worker task id: `{result['worker_task']['id']}`")
    lines.append(f"- Worker task status: `{result['worker_task']['status']}`")
    lines.append(f"- Per-test run id: `{result['workflow_run']['id']}`")
    lines.append(f"- Per-test run status: `{result['workflow_run']['status']}`")
    lines.extend(["", "## Remaining Unavailable Worker Kinds", ""])
    for kind in result["remaining_unavailable"]:
        lines.append(f"- `{kind}`")
    lines.extend(["", "## Implementation Paths", ""])
    for path in result["implementation_paths"]:
        lines.append(f"- `{path}`")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = verify_contract()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
