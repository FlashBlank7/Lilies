#!/usr/bin/env python3
"""Generate v0.2.112 E08 distributed heartbeat registry evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.112_e08_distributed_heartbeat_registry"


def verify_contract() -> dict[str, Any]:
    backend_src = ROOT / "platform" / "backend" / "src"
    if str(backend_src) not in sys.path:
        sys.path.insert(0, str(backend_src))

    from agent_platform.platform_harness import PlatformHarness  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.storage import Storage  # pylint: disable=import-error,import-outside-toplevel
    from agent_platform.worker_runner import PlatformHarnessWorkerRunner  # pylint: disable=import-error,import-outside-toplevel

    async def scenario() -> dict[str, Any]:
        data_dir = ROOT / ".tmp" / "v02_112_e08_distributed_heartbeat_registry"
        if data_dir.exists():
            shutil.rmtree(data_dir)
        storage = Storage(data_dir)
        await storage.initialize()
        harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
        await harness.record_worker_heartbeat(
            worker_id="worker-active",
            status="idle",
            stale_after_seconds=60,
            metadata={"phase": "active"},
        )
        restarted = PlatformHarness(storage=storage, worker_lease_seconds=60)
        persisted = {row.worker_id: row for row in await restarted.list_worker_heartbeats()}

        await restarted.record_worker_heartbeat(
            worker_id="worker-stale",
            status="idle",
            stale_after_seconds=0.001,
            metadata={"phase": "stale"},
        )
        await asyncio.sleep(0.01)
        liveness_rows = {row.worker_id: row for row in await restarted.list_worker_heartbeats()}

        await restarted.start_task(
            "heartbeat-evidence-task",
            kind="scheduler_manual_trigger",
            owner_id="owner-a",
            resource_id="schedule-a",
            worker_id="producer",
            lease_seconds=60,
        )
        await restarted.release_task_lease("heartbeat-evidence-task", worker_id="producer", next_status="queued")
        observed_running: dict[str, Any] = {}

        async def handler(record):
            rows = {row.worker_id: row for row in await restarted.list_worker_heartbeats()}
            observed_running["status"] = rows["worker-runner"].status
            observed_running["active_task_id"] = rows["worker-runner"].active_task_id
            return {"handled": True, "task_id": record.id}

        runner = PlatformHarnessWorkerRunner(
            harness=restarted,
            worker_id="worker-runner",
            lease_seconds=60,
            handlers={"scheduler_manual_trigger": handler},
        )
        results = await runner.run_once(limit=5)
        after_runner = {row.worker_id: row for row in await restarted.list_worker_heartbeats()}

        checks = {
            "heartbeat_persisted_across_harness_instances": "worker-active" in persisted
            and persisted["worker-active"].metadata["phase"] == "active",
            "active_liveness_exposed": liveness_rows["worker-active"].liveness == "active",
            "stale_liveness_exposed": liveness_rows["worker-stale"].liveness == "stale",
            "runner_sets_running_active_task": observed_running == {
                "status": "running",
                "active_task_id": "heartbeat-evidence-task",
            },
            "runner_returns_to_idle_with_last_task_metadata": after_runner["worker-runner"].status == "idle"
            and after_runner["worker-runner"].active_task_id == ""
            and after_runner["worker-runner"].metadata["last_task_id"] == "heartbeat-evidence-task"
            and after_runner["worker-runner"].metadata["last_task_status"] == "succeeded",
            "worker_runner_task_succeeded": len(results) == 1 and results[0].status == "succeeded",
        }
        return {
            "checks": checks,
            "heartbeats": [row.model_dump(mode="json") for row in await restarted.list_worker_heartbeats()],
            "runner_result": asdict(results[0]) if results else {},
        }

    details = asyncio.run(scenario())
    return {
        "version": "v0.2.112",
        "evidence_id": "e08_distributed_heartbeat_registry",
        "source_stage_report": "docs/stage-reports/v0.2.111_e08_remaining_sidecar_slice_reselection.md",
        "status": "completed" if all(details["checks"].values()) else "needs_attention",
        "checks": details["checks"],
        "heartbeats": details["heartbeats"],
        "runner_result": details["runner_result"],
        "completed_slices_preserved": [
            "docs/workingon-archives/v0.2.110/evidence_v0.2.110_e08_complete_handler_catalog_summary.md",
            "docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md",
            "docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md",
            "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
            "docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md",
            "docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md",
        ],
        "implementation_paths": [
            "platform/backend/src/agent_platform/storage.py",
            "platform/backend/src/agent_platform/platform_harness.py",
            "platform/backend/src/agent_platform/worker_runner.py",
            "platform/backend/src/agent_platform/api.py",
            "tests/test_v02_112_e08_distributed_heartbeat_registry.py",
        ],
        "invariants": {
            "distributed_queue_implemented": False,
            "process_supervision_implemented": False,
            "external_alerting_implemented": False,
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
        },
        "next_boundary": (
            "This closes durable worker heartbeat/liveness registry only; distributed queue semantics, "
            "process supervision, external alerting, real worker-offload handlers, and external KMS remain open."
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
        "# v0.2.112 E08 distributed heartbeat registry",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Distributed queue implemented: `{result['invariants']['distributed_queue_implemented']}`",
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
    lines.extend(["", "## Heartbeats", "", "| Worker | Status | Liveness | Active task |", "| --- | --- | --- | --- |"])
    for row in result["heartbeats"]:
        lines.append(f"| `{row['worker_id']}` | `{row['status']}` | `{row['liveness']}` | `{row['active_task_id']}` |")
    lines.extend(["", "## Completed Slices Preserved", ""])
    for path in result["completed_slices_preserved"]:
        lines.append(f"- `{path}`")
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
