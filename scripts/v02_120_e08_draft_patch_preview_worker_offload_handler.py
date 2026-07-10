#!/usr/bin/env python3
"""Generate v0.2.120 E08 draft_patch_preview worker offload evidence."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.120_e08_draft_patch_preview_worker_offload_handler"


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


def create_preview_workflow(client: Any) -> tuple[str, dict[str, Any]]:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={
            "name": "v0.2.120 draft patch preview evidence",
            "requirement": "Preview a draft patch through the worker catalog.",
        },
    )
    response.raise_for_status()
    app_id = response.json()["id"]
    revision = 0
    for node in [
        {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
        {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"ok": True}}},
    ]:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers())
    draft.raise_for_status()
    return app_id, draft.json()


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

    data_dir = ROOT / ".tmp" / "v02_120_e08_draft_patch_preview_worker_offload_handler"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    settings = Settings(
        api_token="workflow-test",
        data_dir=data_dir / "data",
        workspace_root=data_dir / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id, before = create_preview_workflow(client)
        services = client.app.state.services
        harness = services.harness
        handlers = build_platform_worker_handlers(services)
        catalog = platform_worker_handler_catalog(handlers)
        entries = {entry["kind"]: entry for entry in catalog["entries"]}

        async def queue_supported_task() -> None:
            await harness.start_task(
                "evidence-draft-preview-worker-task",
                kind="draft_patch_preview",
                owner_id=app_id,
                resource_id=app_id,
                metadata={"instruction": "rename node end to Final Answer"},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(
                "evidence-draft-preview-worker-task",
                worker_id="producer",
                next_status="queued",
            )

        client.portal.call(queue_supported_task)
        runner = PlatformHarnessWorkerRunner(
            harness=harness,
            worker_id="v02-120-worker",
            lease_seconds=60,
            handlers=handlers,
        )

        async def run_supported_worker_once():
            return await runner.run_once(kind="draft_patch_preview", limit=5)

        supported_results = client.portal.call(run_supported_worker_once)
        supported_task = client.portal.call(
            harness.get_task,
            "evidence-draft-preview-worker-task",
        )
        supported_result = supported_task.metadata["worker_runner"]["result"]
        after_supported = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers())
        after_supported.raise_for_status()

        async def queue_unsupported_task() -> None:
            await harness.start_task(
                "evidence-draft-preview-unsupported-task",
                kind="draft_patch_preview",
                owner_id=app_id,
                resource_id=app_id,
                metadata={"instruction": "please invent a new workflow"},
                worker_id="producer",
                lease_seconds=60,
            )
            await harness.release_task_lease(
                "evidence-draft-preview-unsupported-task",
                worker_id="producer",
                next_status="queued",
            )

        client.portal.call(queue_unsupported_task)

        async def run_unsupported_worker_once():
            return await runner.run_once(kind="draft_patch_preview", limit=5)

        unsupported_results = client.portal.call(run_unsupported_worker_once)
        unsupported_task = client.portal.call(
            harness.get_task,
            "evidence-draft-preview-unsupported-task",
        )
        after_unsupported = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers())
        after_unsupported.raise_for_status()

        api_preview = client.post(
            f"/api/v1/applications/{app_id}/draft/preview-patch",
            headers=headers(),
            json={"instruction": "rename node end to Final Answer"},
        )
        api_preview.raise_for_status()
        after_api_preview = client.get(f"/api/v1/applications/{app_id}/draft", headers=headers())
        after_api_preview.raise_for_status()
        api_tasks = client.get(
            f"/api/v1/platform/harness/tasks?kind=draft_patch_preview&owner_id={app_id}",
            headers=headers(),
        )
        api_tasks.raise_for_status()
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
        "draft_patch_preview_catalog_implemented": entries["draft_patch_preview"]["status"] == "implemented"
        and entries["draft_patch_preview"]["executable"] is True
        and entries["draft_patch_preview"]["implementation"] == "draft_patch_preview_handler",
        "worker_completed_supported_preview_task": len(supported_results) == 1
        and supported_results[0].status == "succeeded"
        and supported_task.status == "succeeded",
        "worker_preview_returned_expected_operation": supported_result["supported"] is True
        and supported_result["intent"] == "rename_node"
        and supported_result["operations"][0]["op"] == "update_node"
        and supported_result["operations"][0]["expected_revision"] == before["revision"],
        "worker_preview_non_destructive": after_supported.json()["revision"] == before["revision"]
        and after_supported.json()["content_hash"] == before["content_hash"],
        "unsupported_preview_fails_deterministically": len(unsupported_results) == 1
        and unsupported_results[0].status == "failed"
        and unsupported_task.status == "failed"
        and "worker draft_patch_preview unsupported" in (unsupported_task.error or ""),
        "unsupported_preview_non_destructive": after_unsupported.json()["revision"] == before["revision"]
        and after_unsupported.json()["content_hash"] == before["content_hash"],
        "api_preview_path_preserved": api_preview.json()["supported"] is True
        and after_api_preview.json()["revision"] == before["revision"]
        and after_api_preview.json()["content_hash"] == before["content_hash"],
        "heartbeat_registry_preserved": heartbeats["v02-120-worker"]["status"] == "idle"
        and heartbeats["v02-120-worker"]["metadata"]["last_task_status"] == "failed",
        "remaining_catalog_gaps_still_unavailable": set(remaining_unavailable)
        == {"builder_build", "benchmark"},
        "full_execution_coverage_not_claimed": catalog["full_execution_coverage"] is False
        and catalog["not_full_sidecar_completion"] is True,
    }
    return {
        "version": "v0.2.120",
        "evidence_id": "e08_draft_patch_preview_worker_offload_handler",
        "source_stage_report": "docs/stage-reports/v0.2.119_e08_remaining_sidecar_slice_reselection.md",
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
        "draft_patch_preview_entry": entries["draft_patch_preview"],
        "remaining_unavailable": remaining_unavailable,
        "supported_worker_task": supported_task.model_dump(mode="json"),
        "unsupported_worker_task": unsupported_task.model_dump(mode="json"),
        "supported_worker_result": supported_result,
        "api_preview_task_id": api_preview.json()["task_id"],
        "api_preview_intent": api_preview.json()["intent"],
        "api_preview_task_count": len(api_tasks.json()),
        "heartbeats": heartbeats,
        "implementation_paths": [
            "platform/backend/src/agent_platform/worker_runner.py",
            "tests/test_v02_120_e08_draft_patch_preview_worker_offload_handler.py",
            "scripts/v02_120_e08_draft_patch_preview_worker_offload_handler.py",
        ],
        "invariants": {
            "worker_preview_does_not_mutate_draft": True,
            "api_preview_path_preserved": True,
            "process_supervision_implemented": False,
            "distributed_queue_implemented": False,
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
            "stage_report_is_next_task_source": True,
        },
        "next_boundary": (
            "This closes the draft_patch_preview worker offload handler only. Full Platform Harness sidecar "
            "completion still needs builder_build, benchmark, production worker supervision, and distributed "
            "queue semantics."
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
        "# v0.2.120 E08 draft_patch_preview worker offload handler",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Draft patch preview status: `{result['draft_patch_preview_entry']['status']}`",
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
    lines.append(f"- Supported worker task id: `{result['supported_worker_task']['id']}`")
    lines.append(f"- Supported worker task status: `{result['supported_worker_task']['status']}`")
    lines.append(f"- Supported preview intent: `{result['supported_worker_result']['intent']}`")
    lines.append(f"- Unsupported worker task id: `{result['unsupported_worker_task']['id']}`")
    lines.append(f"- Unsupported worker task status: `{result['unsupported_worker_task']['status']}`")
    lines.append(f"- API preview task id: `{result['api_preview_task_id']}`")
    lines.append(f"- API preview intent: `{result['api_preview_intent']}`")
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
