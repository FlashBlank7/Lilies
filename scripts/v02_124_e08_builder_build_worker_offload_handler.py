#!/usr/bin/env python3
"""Generate v0.2.124 E08 builder_build worker offload evidence."""

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
OUTPUT_NAME = "evidence_v0.2.124_e08_builder_build_worker_offload_handler"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def create_application(client: Any, *, name: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": name, "requirement": "Build a tested greeting workflow."},
    )
    response.raise_for_status()
    return response.json()["id"]


def wait_for_build(client: Any, build_id: str, terminal: set[str]) -> dict[str, Any]:
    build: dict[str, Any] = {}
    for _ in range(300):
        response = client.get(f"/api/v1/builds/{build_id}", headers=headers())
        response.raise_for_status()
        build = response.json()
        if build["status"] in terminal:
            return build
        time.sleep(0.01)
    return build


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
    from tests.test_workflow import (  # pylint: disable=import-error,import-outside-toplevel
        IncrementalBuilderProvider,
        TimeoutBuilderProvider,
    )

    success_data_dir = ROOT / ".tmp" / "v02_124_e08_builder_build_worker_offload_handler_success"
    if success_data_dir.exists():
        shutil.rmtree(success_data_dir)
    success_settings = Settings(
        api_token="workflow-test",
        data_dir=success_data_dir / "data",
        workspace_root=success_data_dir / "workspaces",
    )
    success_app = create_app(success_settings, IncrementalBuilderProvider())
    with TestClient(success_app) as client:
        app_id = create_application(client, name="v0.2.124 worker build success")
        services = client.app.state.services
        harness = services.harness
        handlers = build_platform_worker_handlers(services)
        catalog = platform_worker_handler_catalog(handlers)
        entries = {entry["kind"]: entry for entry in catalog["entries"]}
        worker_build_id = str(uuid4())

        async def queue_worker_build() -> None:
            await services.workflow_store.create_build(
                worker_build_id,
                app_id,
                "Build a tested greeting workflow.",
                True,
                12,
                2,
            )
            await harness.start_task(
                worker_build_id,
                kind="builder_build",
                owner_id=app_id,
                resource_id=worker_build_id,
                metadata={"build_id": worker_build_id, "auto_publish": True},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(worker_build_id, worker_id="producer", next_status="queued")

        client.portal.call(queue_worker_build)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="v02-124-worker-success",
            lease_seconds=60,
            handlers=handlers,
        )

        async def run_success_worker_once():
            return await runner.run_once(kind="builder_build", limit=5)

        success_results = client.portal.call(run_success_worker_once)
        success_build = client.get(f"/api/v1/builds/{worker_build_id}", headers=headers()).json()
        success_task = client.portal.call(harness.get_task, worker_build_id)
        success_events = client.get(f"/v1/streams/{worker_build_id}", headers=headers()).json()

        success_heartbeats = {
            row.worker_id: row.model_dump(mode="json")
            for row in client.portal.call(harness.list_worker_heartbeats)
        }

    api_data_dir = ROOT / ".tmp" / "v02_124_e08_builder_build_worker_offload_handler_api"
    if api_data_dir.exists():
        shutil.rmtree(api_data_dir)
    api_settings = Settings(
        api_token="workflow-test",
        data_dir=api_data_dir / "data",
        workspace_root=api_data_dir / "workspaces",
    )
    api_app = create_app(api_settings, IncrementalBuilderProvider())
    with TestClient(api_app) as client:
        api_app_id = create_application(client, name="v0.2.124 api build preserved")
        api_created = client.post(
            f"/api/v1/applications/{api_app_id}/builds",
            headers=headers(),
            json={
                "requirement": "Build a tested greeting workflow.",
                "auto_publish": True,
                "planning_mode": "disabled",
            },
        )
        api_created.raise_for_status()
        api_build_id = api_created.json()["build_id"]
        api_build = wait_for_build(client, api_build_id, {"published", "needs_attention"})
        api_task = client.portal.call(client.app.state.services.harness.get_task, api_build_id)

    failure_data_dir = ROOT / ".tmp" / "v02_124_e08_builder_build_worker_offload_handler_failure"
    if failure_data_dir.exists():
        shutil.rmtree(failure_data_dir)
    failure_settings = Settings(
        api_token="workflow-test",
        data_dir=failure_data_dir / "data",
        workspace_root=failure_data_dir / "workspaces",
    )
    failure_app = create_app(failure_settings, TimeoutBuilderProvider())
    with TestClient(failure_app) as client:
        app_id = create_application(client, name="v0.2.124 worker build failure")
        services = client.app.state.services
        harness = services.harness
        worker_build_id_failed = str(uuid4())

        async def queue_failed_worker_build() -> None:
            await services.workflow_store.create_build(
                worker_build_id_failed,
                app_id,
                "Build a workflow that times out.",
                False,
                5,
                1,
            )
            await harness.start_task(
                worker_build_id_failed,
                kind="builder_build",
                owner_id=app_id,
                resource_id=worker_build_id_failed,
                metadata={"build_id": worker_build_id_failed, "auto_publish": False},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(worker_build_id_failed, worker_id="producer", next_status="queued")

        client.portal.call(queue_failed_worker_build)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="v02-124-worker-failure",
            lease_seconds=60,
            handlers=build_platform_worker_handlers(services),
        )

        async def run_failed_worker_once():
            return await runner.run_once(kind="builder_build", limit=5)

        failure_results = client.portal.call(run_failed_worker_once)
        failure_build = client.get(f"/api/v1/builds/{worker_build_id_failed}", headers=headers()).json()
        failure_task = client.portal.call(harness.get_task, worker_build_id_failed)

    remaining_unavailable = [
        entry["kind"]
        for entry in catalog["entries"]
        if entry["status"] == "unavailable"
    ]
    success_result = success_task.metadata["worker_runner"]["result"]
    failure_result = failure_task.metadata["worker_runner"]["result"]
    checks = {
        "builder_build_catalog_implemented": entries["builder_build"]["status"] == "implemented"
        and entries["builder_build"]["executable"] is True
        and entries["builder_build"]["implementation"] == "builder_build_handler",
        "all_required_worker_kinds_executable": catalog["full_execution_coverage"] is True
        and remaining_unavailable == [],
        "worker_completed_builder_build": len(success_results) == 1
        and success_results[0].status == "succeeded"
        and success_task.status == "succeeded"
        and success_build["status"] == "published"
        and success_result["status"] == "published",
        "worker_build_recorded_usage_and_events": success_task.usage_counts.get("model_call", 0) >= 1
        and any(event["type"] == "build.completed" for event in success_events),
        "worker_failed_builder_build_with_metadata": len(failure_results) == 1
        and failure_results[0].status == "failed"
        and failure_task.status == "failed"
        and failure_build["status"] == "needs_attention"
        and "timed out" in failure_result["error"],
        "api_build_path_preserved": api_build["status"] == "published" and api_task.status == "succeeded",
        "heartbeat_registry_preserved": success_heartbeats["v02-124-worker-success"]["status"] == "idle"
        and success_heartbeats["v02-124-worker-success"]["metadata"]["last_task_status"] == "succeeded",
        "not_full_sidecar_completion_preserved": catalog["not_full_sidecar_completion"] is True,
    }
    return {
        "version": "v0.2.124",
        "evidence_id": "e08_builder_build_worker_offload_handler",
        "source_stage_report": "docs/stage-reports/v0.2.123_e08_remaining_sidecar_slice_reselection.md",
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
        "builder_build_entry": entries["builder_build"],
        "remaining_unavailable": remaining_unavailable,
        "success_worker_task": success_task.model_dump(mode="json"),
        "failure_worker_task": failure_task.model_dump(mode="json"),
        "success_build": {
            "id": worker_build_id,
            "status": success_build["status"],
            "published_version": success_build["team_state"]["published_version"],
        },
        "failure_build": {
            "id": worker_build_id_failed,
            "status": failure_build["status"],
            "error": failure_build["error"],
        },
        "api_build": {
            "id": api_build_id,
            "status": api_build["status"],
            "task_status": api_task.status,
        },
        "implementation_paths": [
            "platform/backend/src/agent_platform/builder.py",
            "platform/backend/src/agent_platform/worker_runner.py",
            "tests/test_v02_124_e08_builder_build_worker_offload_handler.py",
            "scripts/v02_124_e08_builder_build_worker_offload_handler.py",
        ],
        "invariants": {
            "api_build_path_preserved": True,
            "all_required_worker_kinds_executable": True,
            "process_supervision_implemented": False,
            "distributed_queue_implemented": False,
            "external_kms_provider_integrated": False,
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
            "stage_report_is_next_task_source": True,
        },
        "next_boundary": (
            "This closes required worker task-kind execution coverage only. Full Platform Harness sidecar "
            "completion still needs production worker supervision, distributed queue semantics, and external KMS "
            "provider integration."
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
        "# v0.2.124 E08 builder_build worker offload handler",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Builder build status: `{result['builder_build_entry']['status']}`",
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
    lines.append(f"- Success worker task id: `{result['success_worker_task']['id']}`")
    lines.append(f"- Success worker task status: `{result['success_worker_task']['status']}`")
    lines.append(f"- Failure worker task id: `{result['failure_worker_task']['id']}`")
    lines.append(f"- Failure worker task status: `{result['failure_worker_task']['status']}`")
    lines.append(f"- API build id: `{result['api_build']['id']}`")
    lines.append(f"- API build status: `{result['api_build']['status']}`")
    lines.extend(["", "## Remaining Unavailable Worker Kinds", ""])
    if result["remaining_unavailable"]:
        for kind in result["remaining_unavailable"]:
            lines.append(f"- `{kind}`")
    else:
        lines.append("- none")
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
