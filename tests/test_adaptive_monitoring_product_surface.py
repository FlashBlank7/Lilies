from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.adaptive_monitoring import adaptive_monitoring_status, refresh_history_path
from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_adaptive_monitoring_api_returns_current_snapshot(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        response = client.get("/api/v1/templates/adaptive-monitoring", headers=headers())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["history_count"] == 0
    assert data["last_refresh"] is None
    assert data["critical_alert_count"] == 0
    assert data["override_options_visible"] is True
    assert data["available_overrides"] == ["adaptive", "deep", "none", "shallow"]
    cases = {(case["family"], case["mode"]): case for case in data["cases"]}
    assert cases[("data_analyzer", "policy_default")]["build_status"] == "published"
    assert cases[("data_analyzer", "policy_default")]["effective_depth"] == "deep"


def test_adaptive_monitoring_refresh_persists_history(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        refresh_response = client.post("/api/v1/templates/adaptive-monitoring/refresh", headers=headers())
        get_response = client.get("/api/v1/templates/adaptive-monitoring", headers=headers())

    assert refresh_response.status_code == 200
    refreshed = refresh_response.json()
    assert refreshed["history_count"] == 1
    assert refreshed["last_refresh"]["status"] == "healthy"
    assert refreshed["last_refresh"]["trigger"] == "manual"
    assert refreshed["last_refresh"]["critical_alert_count"] == 0
    assert refresh_history_path(settings.data_dir).exists()

    assert get_response.status_code == 200
    current = get_response.json()
    assert current["history_count"] == 1
    assert current["last_refresh"]["refreshed_at"] == refreshed["last_refresh"]["refreshed_at"]


def test_adaptive_monitoring_status_handles_missing_evidence(tmp_path: Path) -> None:
    missing = tmp_path / "missing-monitor.json"

    status = adaptive_monitoring_status(missing)

    assert status["status"] == "missing_evidence"
    assert status["cases"] == []
    assert status["override_options_visible"] is False


def test_adaptive_monitoring_schedule_defaults_to_disabled(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        response = client.get("/api/v1/templates/adaptive-monitoring/schedule", headers=headers())

    assert response.status_code == 200
    schedule = response.json()
    assert schedule["enabled"] is False
    assert schedule["interval_seconds"] == 0
    assert schedule["running"] is False
    assert schedule["last_refresh"] is None


def test_adaptive_monitoring_schedule_run_once_records_trigger(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        response = client.post("/api/v1/templates/adaptive-monitoring/schedule/run-once", headers=headers())
        status_response = client.get("/api/v1/templates/adaptive-monitoring", headers=headers())

    assert response.status_code == 200
    schedule = response.json()
    assert schedule["last_refresh"]["trigger"] == "manual_schedule_run"
    assert schedule["last_refresh"]["status"] == "healthy"

    assert status_response.status_code == 200
    status = status_response.json()
    assert status["history_count"] == 1
    assert status["last_refresh"]["trigger"] == "manual_schedule_run"


def test_adaptive_monitoring_schedule_starts_background_task_when_enabled(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        adaptive_monitoring_refresh_interval_seconds=3600,
    )
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        response = client.get("/api/v1/templates/adaptive-monitoring/schedule", headers=headers())

    assert response.status_code == 200
    schedule = response.json()
    assert schedule["enabled"] is True
    assert schedule["interval_seconds"] == 3600
    assert schedule["running"] is True
