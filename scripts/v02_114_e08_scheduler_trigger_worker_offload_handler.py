#!/usr/bin/env python3
"""Generate v0.2.114 E08 scheduler_trigger worker offload evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.114_e08_scheduler_trigger_worker_offload_handler"


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


def create_scheduled_application(client: Any) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "v0.2.114 offload evidence", "requirement": "Run scheduled workflow through worker."},
    )
    response.raise_for_status()
    app_id = response.json()["id"]
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
    mutate(
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
    test_response = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=headers())
    test_response.raise_for_status()
    if not test_response.json()["passed"]:
        raise RuntimeError("schedule workflow fixture test failed")
    publish_response = client.post(f"/api/v1/applications/{app_id}/versions", headers=headers())
    publish_response.raise_for_status()
    return app_id


def wait_for_run(client: Any, run_id: str) -> dict[str, Any]:
    run: dict[str, Any] = {}
    for _ in range(100):
        response = client.get(f"/api/v1/runs/{run_id}", headers=headers())
        response.raise_for_status()
        run = response.json()
        if run["status"] == "succeeded":
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

    data_dir = ROOT / ".tmp" / "v02_114_e08_scheduler_trigger_worker_offload_handler"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    settings = Settings(
        api_token="workflow-test",
        data_dir=data_dir / "data",
        workspace_root=data_dir / "workspaces",
        scheduler_poll_seconds=3600,
        scheduler_worker_offload_enabled=True,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = create_scheduled_application(client)
        services = client.app.state.services
        scheduler = services.scheduler
        harness = services.harness
        workflow_store = services.workflow_store
        handlers = build_platform_worker_handlers(services)
        catalog = platform_worker_handler_catalog(handlers)
        entries = {entry["kind"]: entry for entry in catalog["entries"]}

        queued_events = client.portal.call(
            scheduler.tick,
            datetime(2026, 6, 23, 23, 0, tzinfo=timezone.utc),
        )
        fires_before = client.portal.call(workflow_store.list_schedule_fires, app_id)

        async def list_queued_scheduler_tasks():
            return await harness.list_tasks(
                kind="scheduler_trigger",
                status="queued",
                owner_id=app_id,
            )

        queued_tasks = client.portal.call(list_queued_scheduler_tasks)

        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="v02-114-worker",
            lease_seconds=60,
            handlers=handlers,
        )

        async def run_worker_once():
            return await runner.run_once(kind="scheduler_trigger", limit=5)

        results = client.portal.call(run_worker_once)
        fires_after = client.portal.call(workflow_store.list_schedule_fires, app_id)
        run = wait_for_run(client, fires_after[0]["run_id"])
        finished_task = client.portal.call(harness.get_task, queued_tasks[0].id)
        heartbeats = {
            row.worker_id: row.model_dump(mode="json")
            for row in client.portal.call(harness.list_worker_heartbeats)
        }

    remaining_unavailable = [
        entry["kind"]
        for entry in catalog["entries"]
        if entry["status"] == "unavailable"
    ]
    checks = {
        "scheduler_trigger_catalog_implemented": entries["scheduler_trigger"]["status"] == "implemented"
        and entries["scheduler_trigger"]["executable"] is True,
        "scheduler_tick_queued_in_offload_mode": len(queued_events) == 1
        and queued_events[0].get("queued") is True
        and fires_before[0]["run_id"] is None
        and len(queued_tasks) == 1,
        "worker_completed_scheduler_trigger_task": len(results) == 1
        and results[0].status == "succeeded"
        and finished_task.status == "succeeded",
        "worker_started_real_scheduled_workflow_run": run.get("status") == "succeeded"
        and run.get("outputs") == {"topic": "idols"}
        and bool(fires_after[0]["run_id"]),
        "scheduler_fire_usage_preserved": finished_task.usage_counts.get("scheduler_fire") == 1,
        "heartbeat_registry_preserved": heartbeats["v02-114-worker"]["status"] == "idle"
        and heartbeats["v02-114-worker"]["metadata"]["last_task_status"] == "succeeded",
        "remaining_catalog_gaps_closed": set(remaining_unavailable) == set(),
        "full_execution_coverage_without_full_sidecar_claim": catalog["full_execution_coverage"] is True
        and catalog["not_full_sidecar_completion"] is True,
    }
    return {
        "version": "v0.2.114",
        "evidence_id": "e08_scheduler_trigger_worker_offload_handler",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.113_e08_remaining_sidecar_slice_reselection.md",
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
        "scheduler_trigger_entry": entries["scheduler_trigger"],
        "remaining_unavailable": remaining_unavailable,
        "queued_event": queued_events[0],
        "finished_task": finished_task.model_dump(mode="json"),
        "workflow_run": {
            "id": run.get("id"),
            "status": run.get("status"),
            "outputs": run.get("outputs"),
        },
        "heartbeats": heartbeats,
        "implementation_paths": [
            "platform/backend/src/agent_platform/scheduler.py",
            "platform/backend/src/agent_platform/worker_runner.py",
            "platform/backend/src/agent_platform/config.py",
            "platform/backend/src/agent_platform/api.py",
            "tests/test_v02_114_e08_scheduler_trigger_worker_offload_handler.py",
        ],
        "invariants": {
            "default_scheduler_direct_path_preserved": True,
            "process_supervision_implemented": False,
            "distributed_queue_implemented": False,
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
        },
        "next_boundary": (
            "This closes the scheduler_trigger worker offload handler only. Full Platform Harness sidecar "
            "completion still needs remaining worker-owned handlers and production worker supervision."
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
        "# v0.2.114 E08 scheduler_trigger worker offload handler",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Scheduler trigger status: `{result['scheduler_trigger_entry']['status']}`",
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
    lines.append(f"- Task id: `{result['finished_task']['id']}`")
    lines.append(f"- Task status: `{result['finished_task']['status']}`")
    lines.append(f"- Run id: `{result['workflow_run']['id']}`")
    lines.append(f"- Run status: `{result['workflow_run']['status']}`")
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
