#!/usr/bin/env python3
"""Generate v0.2.91 complexity-router runtime activation observability evidence."""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from frontend_verification_runner import ROOT, run_frontend_verification


OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "metrics_v0.2.91_complexity_router_runtime_activation_observability"


class QuietProvider(ModelProvider):
    name = "scripted"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "text", "text": "observed"},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "text_delta", "text": "observed"},
        })
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def create_application(client: TestClient, requirement: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Router observability evidence", "requirement": requirement},
    )
    response.raise_for_status()
    return str(response.json()["id"])


def create_build(
    client: TestClient,
    application_id: str,
    requirement: str,
    *,
    planning_mode: str = "auto",
) -> dict[str, Any]:
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
    response.raise_for_status()
    return response.json()


def metrics(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/v1/platform/complexity-router/runtime-activation-metrics", headers=headers())
    response.raise_for_status()
    return response.json()


def build_evidence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="v02_91_router_") as temp:
        temp_root = Path(temp)
        default_settings = Settings(
            api_token="test-token",
            data_dir=temp_root / "default-data",
            workspace_root=temp_root / "default-work",
        )
        default_app = create_app(default_settings, QuietProvider())
        with TestClient(default_app) as client:
            default_app_id = create_application(client, "Fix a typo in a settings label.")
            create_build(client, default_app_id, "Fix a typo in a settings label.")
            default_metrics = metrics(client)

        enabled_settings = Settings(
            api_token="test-token",
            data_dir=temp_root / "enabled-data",
            workspace_root=temp_root / "enabled-work",
            complexity_router_default_mode="limited_default",
            complexity_router_limited_default_enabled=True,
        )
        enabled_app = create_app(enabled_settings, QuietProvider())
        with TestClient(enabled_app) as client:
            simple_app_id = create_application(client, "Fix a typo in a settings label.")
            create_build(client, simple_app_id, "Fix a typo in a settings label.")

            medium_app_id = create_application(client, "Add an API endpoint with tests.")
            create_build(client, medium_app_id, "Add an API endpoint with tests.", planning_mode="disabled")

            unknown_app_id = create_application(client, "")
            create_build(client, unknown_app_id, "          ")
            enabled_metrics = metrics(client)

    frontend = run_frontend_verification()
    checks = {
        "default_disabled_counted": default_metrics["decision_categories"]["disabled_default"] == 1
        and default_metrics["decision_categories"]["active"] == 0,
        "enabled_active_counted": enabled_metrics["decision_categories"]["active"] == 2,
        "unknown_counted": enabled_metrics["decision_categories"]["conservative_unknown"] == 1
        and enabled_metrics["decision_categories"]["bypassed"] == 1,
        "request_override_counted": enabled_metrics["decision_categories"]["request_override"] == 1,
        "planning_and_reuse_counted": enabled_metrics["effective_planning_mode_distribution"]["disabled"] == 2
        and enabled_metrics["runtime_reuse_depth_distribution"]["shallow"] == 1
        and enabled_metrics["runtime_reuse_depth_distribution"]["adaptive"] == 1
        and enabled_metrics["runtime_reuse_depth_distribution"]["none"] == 1,
        "frontend_passed": frontend["passed"] is True,
    }
    return {
        "version": "v0.2.91",
        "metric_id": "complexity_router_runtime_activation_rollout_metrics",
        "source_stage_report": "docs/stage-reports/v0.2.90_complexity_router_runtime_activation_path.md",
        "status": "completed" if all(checks.values()) else "failed",
        "checks": checks,
        "default_metrics": default_metrics,
        "enabled_metrics": enabled_metrics,
        "frontend_verification": frontend,
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.91 complexity-router runtime activation observability",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Default metrics active count: `{result['default_metrics']['decision_categories']['active']}`",
        f"- Default metrics disabled-default count: `{result['default_metrics']['decision_categories']['disabled_default']}`",
        f"- Enabled metrics active count: `{result['enabled_metrics']['decision_categories']['active']}`",
        f"- Enabled metrics bypassed count: `{result['enabled_metrics']['decision_categories']['bypassed']}`",
        f"- Enabled metrics conservative-unknown count: `{result['enabled_metrics']['decision_categories']['conservative_unknown']}`",
        f"- Enabled metrics request-override count: `{result['enabled_metrics']['decision_categories']['request_override']}`",
        f"- Enabled planning-mode distribution: `{result['enabled_metrics']['effective_planning_mode_distribution']}`",
        f"- Enabled reuse-depth distribution: `{result['enabled_metrics']['runtime_reuse_depth_distribution']}`",
        f"- Frontend verification passed: `{result['frontend_verification']['passed']}`",
        "",
        "## Metrics Contract",
        "",
        "- Metrics distinguish active, bypassed, disabled-default, conservative-unknown, and request-override decisions.",
        "- Metrics expose classification, effective planning mode, runtime reuse depth, build outcome, and sampled records.",
        "- Metrics are read-only and preserve rollback value `disabled`.",
        "",
    ]
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = build_evidence()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
