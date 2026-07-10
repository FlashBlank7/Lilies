from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.complexity_router import runtime_activation_rollout_metrics
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def fake_build(
    build_id: str,
    *,
    status: str,
    active: bool,
    configured_mode: str,
    requirement_class: str,
    effective_class: str,
    planning_mode_source: str,
    effective_planning_mode: str,
    reuse_depth: str | None,
    conservative_unknown: bool = False,
) -> dict[str, object]:
    return {
        "id": build_id,
        "application_id": f"app-{build_id}",
        "status": status,
        "team_state": {
            "planning_mode": effective_planning_mode,
            "complexity_router": {
                "active": active,
                "planning_mode_source": planning_mode_source,
                "effective_planning_mode": effective_planning_mode,
                "classification": {
                    "configured_default_mode": configured_mode,
                    "requirement_class": requirement_class,
                    "effective_class": effective_class,
                    "conservative_unknown": conservative_unknown,
                },
            },
            "runtime_builder_policy": {"reuse_depth": reuse_depth} if reuse_depth else None,
        },
    }


def create_test_application(client: TestClient, requirement: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Router metrics", "requirement": requirement},
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


def test_v02_91_rollout_metrics_distinguish_activation_categories() -> None:
    metrics = runtime_activation_rollout_metrics([
        fake_build(
            "active-simple",
            status="ready",
            active=True,
            configured_mode="limited_default",
            requirement_class="simple",
            effective_class="simple",
            planning_mode_source="complexity_router",
            effective_planning_mode="disabled",
            reuse_depth="shallow",
        ),
        fake_build(
            "disabled-default",
            status="queued",
            active=False,
            configured_mode="disabled",
            requirement_class="simple",
            effective_class="simple",
            planning_mode_source="request_default",
            effective_planning_mode="auto",
            reuse_depth=None,
        ),
        fake_build(
            "unknown",
            status="queued",
            active=False,
            configured_mode="limited_default",
            requirement_class="unknown",
            effective_class="complex",
            planning_mode_source="request_default",
            effective_planning_mode="auto",
            reuse_depth=None,
            conservative_unknown=True,
        ),
        fake_build(
            "request-override",
            status="needs_attention",
            active=True,
            configured_mode="limited_default",
            requirement_class="medium",
            effective_class="medium",
            planning_mode_source="request_override",
            effective_planning_mode="disabled",
            reuse_depth="adaptive",
        ),
    ])

    assert metrics["total_builds"] == 4
    assert metrics["rollback_value"] == "disabled"
    assert metrics["decision_categories"] == {
        "active": 2,
        "bypassed": 2,
        "disabled_default": 1,
        "conservative_unknown": 1,
        "request_override": 1,
    }
    assert metrics["classification_distribution"] == {"simple": 2, "complex": 1, "medium": 1}
    assert metrics["effective_planning_mode_distribution"] == {"disabled": 2, "auto": 2}
    assert metrics["runtime_reuse_depth_distribution"] == {"shallow": 1, "none": 2, "adaptive": 1}
    assert metrics["build_outcome_distribution"] == {"ready": 1, "queued": 2, "needs_attention": 1}


def test_v02_91_runtime_activation_metrics_api_reads_persisted_build_state(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "work",
        complexity_router_default_mode="limited_default",
        complexity_router_limited_default_enabled=True,
    )
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        simple_app_id = create_test_application(client, "Fix a typo in a settings label.")
        create_build(client, simple_app_id, "Fix a typo in a settings label.")

        medium_override_app_id = create_test_application(client, "Add an API endpoint with tests.")
        create_build(
            client,
            medium_override_app_id,
            "Add an API endpoint with tests.",
            planning_mode="disabled",
        )

        unknown_app_id = create_test_application(client, "")
        create_build(client, unknown_app_id, "          ")

        response = client.get(
            "/api/v1/platform/complexity-router/runtime-activation-metrics",
            headers=headers(),
        )

    assert response.status_code == 200, response.text
    metrics = response.json()
    assert metrics["total_builds"] == 3
    assert metrics["decision_categories"]["active"] == 2
    assert metrics["decision_categories"]["bypassed"] == 1
    assert metrics["decision_categories"]["conservative_unknown"] == 1
    assert metrics["decision_categories"]["request_override"] == 1
    assert metrics["classification_distribution"]["simple"] == 1
    assert metrics["classification_distribution"]["medium"] == 1
    assert metrics["classification_distribution"]["complex"] == 1
    assert metrics["effective_planning_mode_distribution"]["disabled"] == 2
    assert metrics["runtime_reuse_depth_distribution"]["shallow"] == 1
    assert metrics["runtime_reuse_depth_distribution"]["adaptive"] == 1
    assert metrics["runtime_reuse_depth_distribution"]["none"] == 1
    assert {record["active"] for record in metrics["records"]} == {True, False}
