from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.complexity_router import (
    DefaultSafetyInputs,
    classify_requirement,
    complexity_router_default_safety_gate,
    current_default_safety_inputs,
    operator_override_plan_status,
    requirement_classification_contract_status,
    rollout_metrics_prerequisites_status,
    validate_operator_override,
)
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_current_complexity_router_default_safety_gate_reports_guarded_default_enabled() -> None:
    inputs = current_default_safety_inputs()
    status = complexity_router_default_safety_gate(inputs)

    assert status["default_enabled"] is True
    assert status["allowed_to_enable_default"] is True
    assert status["router_ready_for_default"] is True
    assert "source_evidence" not in status["missing_prerequisites"]
    assert "requirement_classification_contract" not in status["missing_prerequisites"]
    assert "operator_override_plan" not in status["missing_prerequisites"]
    assert "rollout_metrics_prerequisites" not in status["missing_prerequisites"]
    assert status["allowed_to_enable_default"] is True
    assert "requirement_classification_contract" in status["supporting_guardrails"]


def test_complexity_router_default_safety_gate_requires_all_prerequisites() -> None:
    incomplete = DefaultSafetyInputs(
        source_evidence_present=True,
        requirement_classification_contract=True,
        operator_override_plan=False,
        rollout_metrics_prerequisites=True,
    )
    complete = DefaultSafetyInputs(
        source_evidence_present=True,
        requirement_classification_contract=True,
        operator_override_plan=True,
        rollout_metrics_prerequisites=True,
    )

    assert complexity_router_default_safety_gate(incomplete)["allowed_to_enable_default"] is False
    allowed = complexity_router_default_safety_gate(complete)
    assert allowed["allowed_to_enable_default"] is True
    assert allowed["router_ready_for_default"] is True
    assert allowed["default_enabled"] is True


def test_complexity_router_default_safety_api_reports_current_gate(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        response = client.get("/api/v1/platform/complexity-router/default-safety", headers=headers())

    assert response.status_code == 200
    body = response.json()
    assert body["gate_id"] == "default_safety_gate"
    assert body["allowed_to_enable_default"] is True
    assert body["default_enabled"] is True
    assert body["router_ready_for_default"] is True
    assert "requirement_classification_contract" not in body["missing_prerequisites"]
    assert "operator_override_plan" not in body["missing_prerequisites"]
    assert "rollout_metrics_prerequisites" not in body["missing_prerequisites"]


def test_requirement_classification_contract_status_is_satisfied_without_enabling_default() -> None:
    status = requirement_classification_contract_status()

    assert status["satisfied"] is True
    assert status["default_router_enabled"] is False
    assert set(status["classes"]) == {"simple", "medium", "complex", "unknown"}
    assert status["conservative_unknown_handling"]["effective_class"] == "complex"


def test_requirement_classification_contract_classifies_core_cases() -> None:
    simple = classify_requirement("Fix a typo in the settings label")
    medium = classify_requirement("Add an API endpoint with tests for the reporting workflow")
    complex_case = classify_requirement("Design a platform guardrail rollout for a model-sensitive agent router")
    unknown = classify_requirement("")

    assert simple["requirement_class"] == "simple"
    assert simple["effective_class"] == "simple"
    assert medium["requirement_class"] == "medium"
    assert medium["effective_class"] == "medium"
    assert complex_case["requirement_class"] == "complex"
    assert complex_case["effective_class"] == "complex"
    assert unknown["requirement_class"] == "unknown"
    assert unknown["effective_class"] == "complex"
    assert unknown["conservative_unknown"] is True


def test_requirement_classification_api_surfaces_contract_and_classification(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        contract_response = client.get(
            "/api/v1/platform/complexity-router/requirement-classification",
            headers=headers(),
        )
        classify_response = client.post(
            "/api/v1/platform/complexity-router/classify-requirement",
            headers=headers(),
            json={"requirement": "Build a multi-module platform API integration with tests"},
        )

    assert contract_response.status_code == 200
    contract = contract_response.json()
    assert contract["satisfied"] is True
    assert contract["default_router_enabled"] is False

    assert classify_response.status_code == 200
    classification = classify_response.json()
    assert classification["requirement_class"] == "complex"
    assert classification["default_router_enabled"] is True
    assert classification["default_builder_policy"]["model_tier"] == "strong"


def test_operator_override_plan_status_and_validation() -> None:
    status = operator_override_plan_status()

    assert status["satisfied"] is True
    assert status["default_router_enabled"] is False
    assert set(status["allowed_modes"]) == {"disabled", "force_simple", "force_medium", "force_complex"}
    assert "force_complex" in status["operator_visible_reason_required_for"]

    assert validate_operator_override("disabled")["valid"] is True
    force_complex = validate_operator_override("force_complex", "High-risk live integration")
    assert force_complex["valid"] is True
    assert force_complex["target_class"] == "complex"
    assert force_complex["operator_visible_reason"] == "High-risk live integration"

    missing_reason = validate_operator_override("force_medium", "")
    assert missing_reason["valid"] is False
    assert missing_reason["error"] == "operator_visible_reason_required"

    unsupported = validate_operator_override("force_magic", "testing")
    assert unsupported["valid"] is False
    assert unsupported["error"] == "unsupported_override_mode"


def test_operator_override_api_surfaces_plan_and_validation(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        plan_response = client.get(
            "/api/v1/platform/complexity-router/operator-override-plan",
            headers=headers(),
        )
        validation_response = client.post(
            "/api/v1/platform/complexity-router/validate-operator-override",
            headers=headers(),
            json={"mode": "force_simple", "reason": "Low-risk text-only edit"},
        )

    assert plan_response.status_code == 200
    plan = plan_response.json()
    assert plan["satisfied"] is True
    assert plan["default_router_enabled"] is False

    assert validation_response.status_code == 200
    validation = validation_response.json()
    assert validation["valid"] is True
    assert validation["target_class"] == "simple"
    assert validation["default_router_enabled"] is False


def test_rollout_metrics_prerequisites_status_satisfies_gate_without_enabling_default() -> None:
    status = rollout_metrics_prerequisites_status()

    assert status["satisfied"] is True
    assert status["default_router_enabled"] is False
    assert status["status"] == "ready_empty_state"
    assert status["sample_count"] == 0
    metric_ids = {item["id"] for item in status["required_metrics"]}
    assert metric_ids == {
        "classification_distribution",
        "override_rate",
        "override_reason_coverage",
        "fallback_unknown_rate",
        "success_rate_by_class",
        "cost_latency_by_class",
    }


def test_rollout_metrics_api_surfaces_empty_state(tmp_path: Path) -> None:
    settings = Settings(api_token="test-token", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/platform/complexity-router/rollout-metrics-prerequisites",
            headers=headers(),
        )

    assert response.status_code == 200
    body = response.json()
    assert body["satisfied"] is True
    assert body["default_router_enabled"] is False
    assert body["status"] == "ready_empty_state"
