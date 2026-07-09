from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


def load_e05_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "e05_template_reuse_depth_experiment.py"
    spec = importlib.util.spec_from_file_location("e05_template_reuse_depth_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def test_e05_depth_arms_and_requirements_are_explicit() -> None:
    module = load_e05_module()

    arms = module.depth_arms()
    assert [arm.depth for arm in arms] == ["none", "shallow", "deep", "adaptive", "policy_default"]
    assert [arm.expected_action for arm in arms] == [
        "build_from_scratch",
        "expand_template",
        "compose_modules",
        "policy_selected",
        "policy_default",
    ]
    for arm in arms:
        requirement = module.requirement_for_arm(arm)
        if arm.depth == "policy_default":
            assert "without a reuse_depth parameter" in requirement
            assert "policy-defaulted" in requirement
        else:
            assert f"reuse_depth='{arm.depth}'" in requirement
        assert "code review and repair BlockFlow" in requirement


def test_e05_selected_arms_defaults_to_all() -> None:
    module = load_e05_module()

    assert module.selected_arm_depths() == ["none", "shallow", "deep", "adaptive", "policy_default"]
    assert [arm.depth for arm in module.selected_arms()] == ["none", "shallow", "deep", "adaptive", "policy_default"]


def test_e05_selected_arms_accepts_deep_only(monkeypatch) -> None:
    module = load_e05_module()
    monkeypatch.setenv(module.SELECTED_ARMS_ENV, "deep")

    assert module.selected_arm_depths() == ["deep"]
    assert [arm.depth for arm in module.selected_arms()] == ["deep"]


def test_e05_selected_arms_rejects_unknown_value(monkeypatch) -> None:
    module = load_e05_module()
    monkeypatch.setenv(module.SELECTED_ARMS_ENV, "deep,sidecar")

    try:
        module.selected_arm_depths()
    except ValueError as error:
        message = str(error)
    else:
        raise AssertionError("expected ValueError for invalid E05 arm filter")

    assert module.SELECTED_ARMS_ENV in message
    assert "sidecar" in message


def test_e05_template_preflight_distinguishes_depth_actions(tmp_path: Path) -> None:
    module = load_e05_module()
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        templates_dir=Path(__file__).resolve().parents[1] / "templates",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        preflight = module.run_template_preflight(client, "workflow-test")

    assert preflight["template_count"] >= 1
    assert preflight["suggestions"]["none"] == []
    assert preflight["suggestions"]["shallow"][0]["recommended_action"] == "expand_template"
    assert preflight["suggestions"]["deep"][0]["recommended_action"] == "compose_modules"
    assert preflight["suggestions"]["adaptive"][0]["effective_reuse_depth"] == "shallow"
    assert preflight["suggestions"]["policy_default"][0]["reuse_depth"] == "adaptive"
    assert preflight["suggestions"]["policy_default"][0]["reuse_depth_source"] == "policy_default"
    assert preflight["suggestions"]["policy_default"][0]["defaulted_by_policy"] is True
    assert preflight["suggestions"]["policy_default"][0]["effective_reuse_depth"] == "shallow"
    assert any(item["name"] == "code_reviewer" for item in preflight["suggestions"]["shallow"])


def test_e05_customer_support_case_has_distinct_requirement_and_reference() -> None:
    module = load_e05_module()
    case = module.experiment_case("customer_support_router")

    requirement = module.requirement_for_arm(module.depth_arms()[1], case)
    reference = module.benchmark_reference(case)

    assert case.name == "customer_support_router"
    assert case.template_name == "customer_support_router"
    assert "customer support routing BlockFlow" in requirement
    assert "code review and repair BlockFlow" not in requirement
    assert "question_classifier" in case.required_node_types
    assert any(node["type"] == "question_classifier" for node in reference["nodes"])
    assert any(node["type"] == "template_transform" for node in reference["nodes"])


def test_e05_data_analyzer_case_has_distinct_requirement_and_reference() -> None:
    module = load_e05_module()
    case = module.experiment_case("data_analyzer")

    requirement = module.requirement_for_arm(module.depth_arms()[1], case)
    reference = module.benchmark_reference(case)

    assert case.name == "data_analyzer"
    assert case.template_name == "data_analyzer"
    assert "editable data analysis BlockFlow" in requirement
    assert "customer support routing BlockFlow" not in requirement
    assert "parameter_extractor" in case.required_node_types
    assert any(node["type"] == "parameter_extractor" for node in reference["nodes"])
    assert any(node["type"] == "template_transform" for node in reference["nodes"])


def test_e05_data_analyzer_preflight_adaptive_resolves_to_deep(tmp_path: Path) -> None:
    module = load_e05_module()
    case = module.experiment_case("data_analyzer")
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        templates_dir=Path(__file__).resolve().parents[1] / "templates",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        preflight = module.run_template_preflight(client, "workflow-test", case)

    adaptive = preflight["suggestions"]["adaptive"][0]
    assert adaptive["name"] == "data_analyzer"
    assert adaptive["effective_reuse_depth"] == "deep"
    assert adaptive["recommended_action"] == "compose_modules"
    assert "parameter_extractor" in adaptive["policy_reason"]

    policy_default = preflight["suggestions"]["policy_default"][0]
    assert policy_default["name"] == "data_analyzer"
    assert policy_default["reuse_depth"] == "adaptive"
    assert policy_default["reuse_depth_source"] == "policy_default"
    assert policy_default["defaulted_by_policy"] is True
    assert policy_default["effective_reuse_depth"] == "deep"
    assert policy_default["recommended_action"] == "compose_modules"


def test_e05_build_payload_includes_optional_build_deadline() -> None:
    module = load_e05_module()
    original = module.MAX_ELAPSED_SECONDS
    try:
        module.MAX_ELAPSED_SECONDS = 12.5
        payload = module.build_request_payload("Build a BlockFlow.")
    finally:
        module.MAX_ELAPSED_SECONDS = original

    assert payload["max_elapsed_seconds"] == 12.5
    assert payload["max_turns"] == module.MAX_TURNS
    assert payload["max_repair_cycles"] == module.MAX_REPAIR_CYCLES
    assert payload["planning_mode"] == "required"


def test_e05_benchmark_outcome_preserves_case_failure_when_suite_passes() -> None:
    module = load_e05_module()
    report = {
        "passed": True,
        "score": 0.78,
        "pass_rate": 0.0,
        "case_count": 1,
        "failed_cases": ["customer_support_router blockflow reuse_depth=none"],
        "reports": [
            {
                "name": "customer_support_router blockflow reuse_depth=none",
                "passed": False,
                "score": 0.78,
                "missing": {"node_types": ["if_else"], "tool_nodes": [], "harness_nodes": []},
            }
        ],
    }

    outcome = module.summarize_benchmark_report(report)

    assert module.BENCHMARK_MINIMUM_PASS_RATE == 1.0
    assert outcome["suite_passed"] is True
    assert outcome["suite_pass_rate"] == 0.0
    assert outcome["case_passed"] is False
    assert outcome["case_score"] == 0.78
    assert outcome["missing"]["node_types"] == ["if_else"]


def test_e05_event_summary_extracts_template_metrics() -> None:
    module = load_e05_module()
    events = [
        {
            "type": "build.operation",
            "data": {
                "tool": "template_suggestions",
                "input": {},
                "success": True,
                "result": '{"reuse_depth":"adaptive","reuse_depth_source":"policy_default","defaulted_by_policy":true,"default_policy_version":"v0.2.52_adaptive_default_productization","available_overrides":["adaptive","deep","none","shallow"],"effective_reuse_depth":"deep","policy_reason":"adaptive:complex_blocks:parameter_extractor","recommended_action":"compose_modules","templates":[{"name":"data_analyzer"}]}',
            },
        },
        {
            "type": "build.operation",
            "data": {
                "tool": "template_expand",
                "input": {"name": "code_reviewer"},
                "success": False,
                "result": "KeyError: unknown workflow template: code_reviewer",
            },
        },
    ]

    summary = module.summarize_events(events)

    assert summary["template_suggestion_count"] == 1
    assert summary["template_expand_count"] == 1
    assert summary["template_suggestions"][0]["effective_reuse_depth"] == "deep"
    assert summary["template_suggestions"][0]["reuse_depth_source"] == "policy_default"
    assert summary["template_suggestions"][0]["defaulted_by_policy"] is True
    assert summary["template_suggestions"][0]["default_policy_version"] == "v0.2.52_adaptive_default_productization"
    assert summary["template_suggestions"][0]["available_overrides"] == ["adaptive", "deep", "none", "shallow"]
    assert summary["template_suggestions"][0]["policy_reason"] == "adaptive:complex_blocks:parameter_extractor"
    assert summary["template_suggestions"][0]["recommended_action"] == "compose_modules"
    assert summary["template_expands"][0]["success"] is False
    assert summary["failed_operations"][0]["tool"] == "template_expand"


def test_e05_event_summary_extracts_timeout_failure_metadata() -> None:
    module = load_e05_module()
    events = [
        {
            "type": "build.coordinator.model.failed",
            "data": {
                "model": "deepseek/deepseek-v4-pro",
                "error": "DeepSeek request timed out",
                "error_type": "ProviderError",
                "retryable": True,
                "status_code": None,
            },
        },
        {
            "type": "build.coordinator.model.timeout",
            "data": {
                "model": "deepseek/deepseek-v4-pro",
                "timeout_seconds": 180,
            },
        },
        {
            "type": "build.needs_attention",
            "data": {
                "error": "DeepSeek request timed out",
                "error_type": "ProviderError",
                "failure": {
                    "type": "model_provider",
                    "error_type": "ProviderError",
                    "retryable": True,
                    "status_code": None,
                    "timeout_like": True,
                },
            },
        },
    ]

    summary = module.summarize_events(events)
    failure = module.summarize_failure(
        {"status": "needs_attention", "error": "DeepSeek request timed out"},
        {
            "status": "failed",
            "error": "DeepSeek request timed out",
            "metadata": {
                "failure": {
                    "type": "model_provider",
                    "error_type": "ProviderError",
                    "retryable": True,
                    "status_code": None,
                    "timeout_like": True,
                }
            },
        },
        summary,
    )

    assert summary["provider_failure_events"][0]["retryable"] is True
    assert summary["model_timeout_events"][0]["timeout_seconds"] == 180
    assert summary["needs_attention_events"][0]["failure"]["type"] == "model_provider"
    assert failure["task_status"] == "failed"
    assert failure["provider_failure_event_count"] == 1
    assert failure["model_timeout_event_count"] == 1
    assert failure["timeout_like"] is True
