#!/usr/bin/env python3
"""Generate v0.2.126 E08 production worker supervision evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.126_e08_production_worker_supervision"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


async def direct_supervision_scenario(output_dir: Path) -> dict[str, Any]:
    from agent_platform.platform_harness import PlatformHarness
    from agent_platform.storage import Storage
    from agent_platform.worker_runner import PlatformHarnessWorkerRunner, PlatformWorkerSupervisor

    storage = Storage(output_dir / "runtime" / "direct")
    await storage.initialize()
    harness = PlatformHarness(storage=storage, worker_lease_seconds=60)
    await harness.start_task(
        "v02-126-supervised-task",
        kind="scheduler_manual_trigger",
        owner_id="owner-a",
        resource_id="schedule-a",
        worker_id="producer",
        lease_seconds=60,
    )
    await harness.release_task_lease("v02-126-supervised-task", worker_id="producer", next_status="queued")

    handled = asyncio.Event()

    async def handler(record):
        handled.set()
        return {"handled_task_id": record.id}

    runner = PlatformHarnessWorkerRunner(
        harness=harness,
        worker_id="v02-126-supervised-worker",
        lease_seconds=60,
        handlers={"scheduler_manual_trigger": handler},
    )
    supervisor = PlatformWorkerSupervisor(runner=runner, poll_seconds=0.01, limit=5)
    started = await supervisor.start()
    await asyncio.wait_for(handled.wait(), timeout=1.0)
    await asyncio.sleep(0.02)
    task = await harness.get_task("v02-126-supervised-task")
    stopped = await supervisor.stop()
    heartbeats = {row.worker_id: row.model_dump(mode="json") for row in await harness.list_worker_heartbeats()}
    return {
        "started": started,
        "stopped": stopped,
        "task": task.model_dump(mode="json"),
        "heartbeats": heartbeats,
    }


def api_supervision_scenario(output_dir: Path) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from agent_platform.api import create_app
    from agent_platform.config import Settings
    from agent_platform.worker_runner import build_platform_worker_handlers, platform_worker_handler_catalog
    from tests.test_runtime import ScriptedProvider

    settings = Settings(
        api_token="workflow-test",
        data_dir=output_dir / "runtime" / "api",
        workspace_root=output_dir / "runtime" / "workspaces",
        platform_harness_worker_id="v02-126-api-supervised-worker",
        platform_harness_worker_supervision_poll_seconds=0.01,
        platform_harness_worker_supervision_limit=2,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        initial = client.get("/api/v1/platform/harness/worker-supervision", headers=headers()).json()
        started = client.post(
            "/api/v1/platform/harness/worker-supervision/start",
            headers=headers(),
            json={"poll_seconds": 0.01, "limit": 2},
        ).json()
        observed = client.get("/api/v1/platform/harness/worker-supervision", headers=headers()).json()
        stopped = client.post("/api/v1/platform/harness/worker-supervision/stop", headers=headers()).json()
        heartbeats = client.get("/api/v1/platform/harness/worker-heartbeats", headers=headers()).json()
        catalog = platform_worker_handler_catalog(build_platform_worker_handlers(client.app.state.services))
    return {
        "initial": initial,
        "started": started,
        "observed": observed,
        "stopped": stopped,
        "heartbeats": heartbeats,
        "catalog": catalog,
    }


def build_evidence(output_dir: Path) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="v02_126_worker_supervision_") as runtime_dir:
        runtime_root = Path(runtime_dir)
        direct = asyncio.run(direct_supervision_scenario(runtime_root))
        api = api_supervision_scenario(runtime_root)
    boundaries = api["stopped"]["boundaries"]
    result = {
        "version": "v0.2.126",
        "source_stage_report": "docs/stage-reports/v0.2.125_e08_remaining_sidecar_architecture_reselection.md",
        "status": "completed",
        "direct_supervision": direct,
        "api_supervision": api,
        "invariants": {
            "supervisor_can_start": direct["started"]["loop_running"] is True
            and api["started"]["loop_running"] is True,
            "supervisor_can_observe": api["observed"]["desired_state"] == "running"
            and api["observed"]["supervision_mode"] == "in_process_worker_loop",
            "supervisor_can_stop": direct["stopped"]["loop_running"] is False
            and api["stopped"]["loop_running"] is False,
            "worker_loop_consumed_task": direct["task"]["status"] == "succeeded"
            and direct["stopped"]["recent_results"][0]["task_id"] == "v02-126-supervised-task",
            "worker_task_kind_execution_coverage_preserved": api["catalog"]["full_execution_coverage"] is True,
            "distributed_queue_semantics_claimed": boundaries["distributed_queue_semantics"],
            "external_process_manager_claimed": boundaries["external_process_manager"],
            "external_kms_provider_integration_claimed": boundaries["external_kms_provider_integration"],
            "e08_full_sidecar_completion_claimed": boundaries["full_sidecar_completion_claimed"],
        },
        "next_boundary": (
            "Production worker supervision is now an in-process supervised loop. Distributed queue semantics, "
            "external process management, external KMS provider integration, and full sidecar completion remain open."
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
        "# v0.2.126 E08 production worker supervision",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Supervisor can start: `{result['invariants']['supervisor_can_start']}`",
        f"- Supervisor can observe: `{result['invariants']['supervisor_can_observe']}`",
        f"- Supervisor can stop: `{result['invariants']['supervisor_can_stop']}`",
        f"- Worker loop consumed task: `{result['invariants']['worker_loop_consumed_task']}`",
        f"- Worker task-kind execution coverage preserved: `{result['invariants']['worker_task_kind_execution_coverage_preserved']}`",
        f"- Distributed queue semantics claimed: `{result['invariants']['distributed_queue_semantics_claimed']}`",
        f"- External process manager claimed: `{result['invariants']['external_process_manager_claimed']}`",
        f"- External KMS provider integration claimed: `{result['invariants']['external_kms_provider_integration_claimed']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Next boundary: {result['next_boundary']}",
        "",
        "## API Surface",
        "",
        "- `GET /api/v1/platform/harness/worker-supervision`",
        "- `POST /api/v1/platform/harness/worker-supervision/start`",
        "- `POST /api/v1/platform/harness/worker-supervision/stop`",
        "",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    _prepare_imports()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_evidence(args.output_dir)
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print("completed")


if __name__ == "__main__":
    main()
