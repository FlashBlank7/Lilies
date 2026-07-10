from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.worker_runner import ExternalWorkerProcessManager
from tests.test_runtime import ScriptedProvider


def sleeper_command() -> list[str]:
    return [sys.executable, "-c", "import time; time.sleep(30)"]


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


def test_v02_130_external_process_manager_start_stop_restart() -> None:
    manager = ExternalWorkerProcessManager(command=sleeper_command(), stop_timeout_seconds=1)
    try:
        started = manager.start()
        assert started["observed_state"] == "running"
        assert started["pid"]
        first_pid = started["pid"]

        restarted = manager.restart()
        assert restarted["observed_state"] == "running"
        assert restarted["pid"]
        assert restarted["pid"] != first_pid
        assert restarted["restart_count"] == 1

        stopped = manager.stop()
        assert stopped["desired_state"] == "stopped"
        assert stopped["observed_state"] == "stopped"
        assert stopped["pid"] is None
        assert stopped["returncode"] is not None
        assert stopped["boundaries"]["distributed_queue_semantics_preserved"] is True
        assert stopped["boundaries"]["external_kms_provider_integration"] is False
        assert stopped["boundaries"]["full_sidecar_completion_claimed"] is False
    finally:
        manager.stop()


def test_v02_130_process_manager_rejects_unconfigured_start() -> None:
    manager = ExternalWorkerProcessManager(command=[])

    try:
        manager.start()
    except ValueError as error:
        assert "command is not configured" in str(error)
    else:
        raise AssertionError("expected unconfigured process manager start to fail")


def test_v02_130_process_manager_api_lifecycle(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_worker_process_command=sleeper_command(),
        platform_harness_worker_process_stop_timeout_seconds=1,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        initial = client.get("/api/v1/platform/harness/worker-process-manager", headers=headers())
        assert initial.status_code == 200, initial.text
        assert initial.json()["configured"] is True
        assert initial.json()["observed_state"] == "stopped"

        started = client.post("/api/v1/platform/harness/worker-process-manager/start", headers=headers())
        assert started.status_code == 200, started.text
        started_body = started.json()
        assert started_body["observed_state"] == "running"
        assert started_body["pid"]

        restarted = client.post("/api/v1/platform/harness/worker-process-manager/restart", headers=headers())
        assert restarted.status_code == 200, restarted.text
        restarted_body = restarted.json()
        assert restarted_body["observed_state"] == "running"
        assert restarted_body["restart_count"] == 1
        assert restarted_body["pid"] != started_body["pid"]

        stopped = client.post("/api/v1/platform/harness/worker-process-manager/stop", headers=headers())
        assert stopped.status_code == 200, stopped.text
        stopped_body = stopped.json()
        assert stopped_body["desired_state"] == "stopped"
        assert stopped_body["observed_state"] == "stopped"
        assert stopped_body["boundaries"]["external_kms_provider_integration"] is False
        assert stopped_body["boundaries"]["full_sidecar_completion_claimed"] is False


def test_v02_130_process_manager_api_start_can_take_operator_command(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        platform_harness_worker_process_stop_timeout_seconds=1,
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        unconfigured = client.post("/api/v1/platform/harness/worker-process-manager/start", headers=headers())
        assert unconfigured.status_code == 422

        started = client.post(
            "/api/v1/platform/harness/worker-process-manager/start",
            headers=headers(),
            json={"command": sleeper_command()},
        )
        assert started.status_code == 200, started.text
        assert started.json()["observed_state"] == "running"
        stopped = client.post("/api/v1/platform/harness/worker-process-manager/stop", headers=headers())
        assert stopped.status_code == 200, stopped.text
