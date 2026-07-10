from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.complexity_router import classify_requirement, limited_default_enablement_plan_status
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def create_test_application(client: TestClient, requirement: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Guarded default", "requirement": requirement},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def create_build(
    client: TestClient,
    application_id: str,
    requirement: str,
    *,
    planning_mode: str = "auto",
) -> dict[str, object]:
    response = client.post(
        f"/api/v1/applications/{application_id}/builds",
        headers=headers(),
        json={
            "requirement": requirement,
            "auto_publish": False,
            "max_turns": 5,
            "planning_mode": planning_mode,
        },
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_v02_93_default_settings_activate_guarded_limited_default_classification() -> None:
    status = limited_default_enablement_plan_status(
        default_mode=Settings().complexity_router_default_mode,
        limited_default_enabled=Settings().complexity_router_limited_default_enabled,
    )
    classification = classify_requirement(
        "Fix a typo in a settings label",
        default_mode=Settings().complexity_router_default_mode,
        limited_default_enabled=Settings().complexity_router_limited_default_enabled,
    )

    assert status["default_enabled"] is True
    assert status["rollback_value"] == "disabled"
    assert classification["default_router_enabled"] is True
    assert classification["default_builder_policy"]["reuse_depth"] == "shallow"


def test_v02_93_default_build_uses_runtime_builder_policy(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "work")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        app_id = create_test_application(client, "Fix a typo in a settings label.")
        created = create_build(client, app_id, "Fix a typo in a settings label.")
        build = client.get(f"/api/v1/builds/{created['build_id']}", headers=headers()).json()

    router = created["complexity_router"]
    assert router["active"] is True
    assert router["effective_planning_mode"] == "disabled"
    assert build["team_state"]["runtime_builder_policy"]["reuse_depth"] == "shallow"


def test_v02_93_unknown_and_disabled_rollback_remain_safe(tmp_path: Path) -> None:
    default_settings = Settings(api_token="test-token", data_dir=tmp_path / "data1", workspace_root=tmp_path / "work1")
    default_app = create_app(default_settings, ScriptedProvider())

    with TestClient(default_app) as client:
        app_id = create_test_application(client, "")
        unknown = create_build(client, app_id, "          ")

    assert unknown["complexity_router"]["active"] is False
    assert unknown["complexity_router"]["classification"]["effective_class"] == "complex"
    assert unknown["complexity_router"]["classification"]["conservative_unknown"] is True

    rollback_settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data2",
        workspace_root=tmp_path / "work2",
        complexity_router_default_mode="disabled",
        complexity_router_limited_default_enabled=False,
    )
    rollback_app = create_app(rollback_settings, ScriptedProvider())

    with TestClient(rollback_app) as client:
        app_id = create_test_application(client, "Fix a typo in a settings label.")
        rolled_back = create_build(client, app_id, "Fix a typo in a settings label.")

    assert rolled_back["complexity_router"]["active"] is False
    assert rolled_back["complexity_router"]["effective_planning_mode"] == "auto"


def test_v02_93_request_override_visibility_is_preserved(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "work")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        app_id = create_test_application(client, "Add an API endpoint with tests.")
        created = create_build(
            client,
            app_id,
            "Add an API endpoint with tests.",
            planning_mode="disabled",
        )

    router = created["complexity_router"]
    assert router["active"] is True
    assert router["planning_mode_source"] == "request_override"
    assert router["effective_planning_mode"] == "disabled"
