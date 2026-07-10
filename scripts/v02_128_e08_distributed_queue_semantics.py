#!/usr/bin/env python3
"""Generate v0.2.128 E08 distributed queue semantics evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.128_e08_distributed_queue_semantics"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


async def queue_task(harness: Any, task_id: str, *, worker_id: str = "producer") -> None:
    await harness.start_task(
        task_id,
        kind="scheduler_manual_trigger",
        owner_id="owner-a",
        resource_id=f"schedule-{task_id}",
        worker_id=worker_id,
        lease_seconds=60,
    )
    await harness.release_task_lease(task_id, worker_id=worker_id, next_status="queued")


async def direct_queue_scenario(runtime_root: Path) -> dict[str, Any]:
    from agent_platform.platform_harness import PlatformHarness
    from agent_platform.storage import Storage
    from agent_platform.worker_runner import PlatformHarnessWorkerRunner

    storage = Storage(runtime_root / "direct")
    await storage.initialize()
    harness = PlatformHarness(storage=storage, worker_lease_seconds=0.001)
    await queue_task(harness, "v02-128-queue-1")
    await queue_task(harness, "v02-128-queue-2")

    first, second, third = await asyncio.gather(
        harness.claim_next_queued_task(worker_id="worker-a", lease_seconds=60),
        harness.claim_next_queued_task(worker_id="worker-b", lease_seconds=60),
        harness.claim_next_queued_task(worker_id="worker-c", lease_seconds=60),
    )
    assert first is not None
    assert second is not None
    await harness.finish_task(first.id, status="succeeded")
    await harness.finish_task(second.id, status="succeeded")

    await queue_task(harness, "v02-128-requeue-1")
    expired = await harness.claim_next_queued_task(worker_id="worker-expired", lease_seconds=0.001)
    assert expired is not None
    await asyncio.sleep(0.01)
    requeued = await harness.requeue_expired_task_leases()

    await queue_task(harness, "v02-128-runner-1")

    async def handler(record):
        return {"handled_task_id": record.id}

    runner = PlatformHarnessWorkerRunner(
        harness=harness,
        worker_id="runner-worker",
        lease_seconds=60,
        handlers={"scheduler_manual_trigger": handler},
    )
    runner_results = await runner.run_once(limit=5)
    snapshot = await harness.queue_semantics_snapshot()
    return {
        "atomic_claims": [
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
            third.model_dump(mode="json") if third else None,
        ],
        "requeued": [task.model_dump(mode="json") for task in requeued],
        "runner_results": [asdict(result) for result in runner_results],
        "snapshot": snapshot,
    }


def api_queue_scenario(runtime_root: Path) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from agent_platform.api import create_app
    from agent_platform.config import Settings
    from tests.test_runtime import ScriptedProvider

    settings = Settings(
        api_token="workflow-test",
        data_dir=runtime_root / "api",
        workspace_root=runtime_root / "workspaces",
        platform_harness_worker_id="api-worker",
        platform_harness_worker_lease_seconds=0.001,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        async def prepare() -> None:
            harness = client.app.state.services.harness
            await queue_task(harness, "v02-128-api-requeue", worker_id="producer")
            claimed = await harness.claim_next_queued_task(worker_id="api-worker", lease_seconds=0.001)
            assert claimed is not None
            await asyncio.sleep(0.01)

        client.portal.call(prepare)
        requeued = client.post("/api/v1/platform/harness/queue/requeue-expired", headers=headers()).json()
        snapshot = client.get("/api/v1/platform/harness/queue-semantics", headers=headers()).json()
    return {
        "requeued": requeued,
        "snapshot": snapshot,
    }


def build_evidence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="v02_128_queue_semantics_") as runtime_dir:
        runtime_root = Path(runtime_dir)
        direct = asyncio.run(direct_queue_scenario(runtime_root))
        api = api_queue_scenario(runtime_root)
    first, second, third = direct["atomic_claims"]
    snapshot = api["snapshot"]
    boundaries = snapshot["boundaries"]
    result = {
        "version": "v0.2.128",
        "source_stage_report": "docs/stage-reports/v0.2.127_e08_remaining_sidecar_architecture_reselection.md",
        "status": "completed",
        "direct_queue": direct,
        "api_queue": api,
        "invariants": {
            "claim_next_single_owner": first is not None
            and second is not None
            and third is None
            and first["worker_id"] != second["worker_id"],
            "expired_lease_requeue": len(direct["requeued"]) == 1
            and direct["requeued"][0]["status"] == "queued",
            "runner_uses_queue_claim_next": len(direct["runner_results"]) >= 1
            and direct["runner_results"][0]["status"] == "succeeded",
            "api_snapshot_available": snapshot["queue_mode"] == "storage_backed_claim_next_with_requeue",
            "api_requeue_available": len(api["requeued"]) == 1
            and api["requeued"][0]["status"] == "queued",
            "external_process_manager_claimed": boundaries["external_process_manager"],
            "external_kms_provider_integration_claimed": boundaries["external_kms_provider_integration"],
            "e08_full_sidecar_completion_claimed": boundaries["full_sidecar_completion_claimed"],
        },
        "next_boundary": (
            "Distributed queue semantics now have storage-backed claim-next and requeue behavior. "
            "External process management, external KMS provider integration, and full sidecar completion remain open."
        ),
    }
    return result


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
        "# v0.2.128 E08 distributed queue semantics",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Claim-next single owner: `{result['invariants']['claim_next_single_owner']}`",
        f"- Expired lease requeue: `{result['invariants']['expired_lease_requeue']}`",
        f"- Runner uses queue claim-next: `{result['invariants']['runner_uses_queue_claim_next']}`",
        f"- API snapshot available: `{result['invariants']['api_snapshot_available']}`",
        f"- API requeue available: `{result['invariants']['api_requeue_available']}`",
        f"- External process manager claimed: `{result['invariants']['external_process_manager_claimed']}`",
        f"- External KMS provider integration claimed: `{result['invariants']['external_kms_provider_integration_claimed']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Next boundary: {result['next_boundary']}",
        "",
        "## API Surface",
        "",
        "- `GET /api/v1/platform/harness/queue-semantics`",
        "- `POST /api/v1/platform/harness/queue/requeue-expired`",
        "",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    _prepare_imports()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_evidence()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print("completed")


if __name__ == "__main__":
    main()
