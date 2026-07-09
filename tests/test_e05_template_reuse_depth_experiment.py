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
    assert [arm.depth for arm in arms] == ["none", "shallow", "deep"]
    assert [arm.expected_action for arm in arms] == [
        "build_from_scratch",
        "expand_template",
        "compose_modules",
    ]
    for arm in arms:
        requirement = module.requirement_for_arm(arm)
        assert f"reuse_depth='{arm.depth}'" in requirement
        assert "code review and repair BlockFlow" in requirement


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
    assert any(item["name"] == "code_reviewer" for item in preflight["suggestions"]["shallow"])


def test_e05_event_summary_extracts_template_metrics() -> None:
    module = load_e05_module()
    events = [
        {
            "type": "build.operation",
            "data": {
                "tool": "template_suggestions",
                "input": {"reuse_depth": "deep"},
                "success": True,
                "result": '{"reuse_depth":"deep","recommended_action":"compose_modules","templates":[{"name":"code_reviewer"}]}',
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
