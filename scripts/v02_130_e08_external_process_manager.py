#!/usr/bin/env python3
"""Generate v0.2.130 E08 external process manager evidence."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.130_e08_external_process_manager"


def _prepare_imports() -> None:
    backend_src = ROOT / "platform" / "backend" / "src"
    for path in (ROOT, backend_src):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))


def sleeper_command() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


def direct_process_scenario() -> dict[str, Any]:
    from agent_platform.worker_runner import ExternalWorkerProcessManager

    manager = ExternalWorkerProcessManager(command=sleeper_command(), stop_timeout_seconds=1)
    try:
        started = manager.start()
        restarted = manager.restart()
        stopped = manager.stop()
    finally:
        manager.stop()
    return {
        "started": started,
        "restarted": restarted,
        "stopped": stopped,
    }


def api_process_scenario(runtime_root: Path) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from agent_platform.api import create_app
    from agent_platform.config import Settings
    from tests.test_runtime import ScriptedProvider

    settings = Settings(
        api_token="workflow-test",
        data_dir=runtime_root / "api",
        workspace_root=runtime_root / "workspaces",
        platform_harness_worker_process_command=sleeper_command(),
        platform_harness_worker_process_stop_timeout_seconds=1,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        initial = client.get("/api/v1/platform/harness/worker-process-manager", headers=headers()).json()
        started = client.post("/api/v1/platform/harness/worker-process-manager/start", headers=headers()).json()
        restarted = client.post("/api/v1/platform/harness/worker-process-manager/restart", headers=headers()).json()
        stopped = client.post("/api/v1/platform/harness/worker-process-manager/stop", headers=headers()).json()
    return {
        "initial": initial,
        "started": started,
        "restarted": restarted,
        "stopped": stopped,
    }


def build_evidence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="v02_130_process_manager_") as runtime_dir:
        runtime_root = Path(runtime_dir)
        direct = direct_process_scenario()
        api = api_process_scenario(runtime_root)
    boundaries = api["stopped"]["boundaries"]
    result = {
        "version": "v0.2.130",
        "source_stage_report": "docs/stage-reports/v0.2.129_e08_remaining_sidecar_architecture_reselection.md",
        "status": "completed",
        "direct_process_manager": direct,
        "api_process_manager": api,
        "invariants": {
            "direct_start_observe_stop": direct["started"]["observed_state"] == "running"
            and direct["stopped"]["observed_state"] == "stopped",
            "direct_restart_changes_pid": direct["started"]["pid"] != direct["restarted"]["pid"]
            and direct["restarted"]["restart_count"] == 1,
            "api_start_observe_stop": api["started"]["observed_state"] == "running"
            and api["stopped"]["observed_state"] == "stopped",
            "api_restart_changes_pid": api["started"]["pid"] != api["restarted"]["pid"]
            and api["restarted"]["restart_count"] == 1,
            "distributed_queue_semantics_preserved": boundaries["distributed_queue_semantics_preserved"],
            "external_kms_provider_integration_claimed": boundaries["external_kms_provider_integration"],
            "e08_full_sidecar_completion_claimed": boundaries["full_sidecar_completion_claimed"],
        },
        "next_boundary": (
            "External process manager now provides local subprocess start/observe/stop/restart. "
            "External KMS provider integration and full sidecar completion remain open."
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
        "# v0.2.130 E08 external process manager",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Direct start/observe/stop: `{result['invariants']['direct_start_observe_stop']}`",
        f"- Direct restart changes pid: `{result['invariants']['direct_restart_changes_pid']}`",
        f"- API start/observe/stop: `{result['invariants']['api_start_observe_stop']}`",
        f"- API restart changes pid: `{result['invariants']['api_restart_changes_pid']}`",
        f"- Distributed queue semantics preserved: `{result['invariants']['distributed_queue_semantics_preserved']}`",
        f"- External KMS provider integration claimed: `{result['invariants']['external_kms_provider_integration_claimed']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Next boundary: {result['next_boundary']}",
        "",
        "## API Surface",
        "",
        "- `GET /api/v1/platform/harness/worker-process-manager`",
        "- `POST /api/v1/platform/harness/worker-process-manager/start`",
        "- `POST /api/v1/platform/harness/worker-process-manager/stop`",
        "- `POST /api/v1/platform/harness/worker-process-manager/restart`",
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
