from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.complexity_router import classify_requirement, limited_default_enablement_plan_status
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_v02_89_default_settings_keep_limited_default_contract_disabled() -> None:
    status = limited_default_enablement_plan_status()
    classification = classify_requirement("Fix a typo in a settings label")

    assert status["configured_default_mode"] == "disabled"
    assert status["limited_default_active"] is False
    assert status["default_enabled"] is False
    assert status["rollback_value"] == "disabled"
    assert classification["default_router_enabled"] is False
    assert classification["default_builder_policy"] is None


def test_v02_89_explicit_limited_default_surfaces_default_builder_policy() -> None:
    status = limited_default_enablement_plan_status(
        default_mode="limited_default",
        limited_default_enabled=True,
        min_confidence=0.55,
    )
    classification = classify_requirement(
        "Fix a typo in a settings label",
        default_mode="limited_default",
        limited_default_enabled=True,
        min_confidence=0.55,
    )
    unknown = classify_requirement(
        "",
        default_mode="limited_default",
        limited_default_enabled=True,
        min_confidence=0.55,
    )

    assert status["limited_default_active"] is True
    assert status["default_enabled"] is True
    assert classification["limited_default_eligible"] is True
    assert classification["default_router_enabled"] is True
    assert classification["default_builder_policy"]["reuse_depth"] == "shallow"
    assert unknown["effective_class"] == "complex"
    assert unknown["limited_default_eligible"] is False
    assert unknown["default_router_enabled"] is False


def test_v02_89_api_surfaces_disabled_and_explicit_limited_default_modes(tmp_path: Path) -> None:
    default_settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data1",
        workspace_root=tmp_path / "work1",
        complexity_router_default_mode="disabled",
        complexity_router_limited_default_enabled=False,
    )
    default_app = create_app(default_settings, ScriptedProvider())

    with TestClient(default_app) as client:
        default_plan = client.get("/api/v1/platform/complexity-router/default-enableable-plan", headers=headers())
        default_classification = client.post(
            "/api/v1/platform/complexity-router/classify-requirement",
            headers=headers(),
            json={"requirement": "Fix a typo in a settings label"},
        )

    assert default_plan.status_code == 200
    assert default_plan.json()["default_enabled"] is False
    assert default_classification.status_code == 200
    assert default_classification.json()["default_router_enabled"] is False

    enabled_settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data2",
        workspace_root=tmp_path / "work2",
        complexity_router_default_mode="limited_default",
        complexity_router_limited_default_enabled=True,
    )
    enabled_app = create_app(enabled_settings, ScriptedProvider())

    with TestClient(enabled_app) as client:
        enabled_plan = client.get("/api/v1/platform/complexity-router/default-enableable-plan", headers=headers())
        enabled_classification = client.post(
            "/api/v1/platform/complexity-router/classify-requirement",
            headers=headers(),
            json={"requirement": "Add an API endpoint with tests for the reporting workflow"},
        )

    assert enabled_plan.status_code == 200
    assert enabled_plan.json()["configured_default_mode"] == "limited_default"
    assert enabled_plan.json()["default_enabled"] is True
    assert enabled_classification.status_code == 200
    assert enabled_classification.json()["default_router_enabled"] is True
    assert enabled_classification.json()["default_builder_policy"]["reuse_depth"] == "adaptive"
