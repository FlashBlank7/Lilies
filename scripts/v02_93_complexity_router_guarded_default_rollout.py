#!/usr/bin/env python3
"""Generate v0.2.93 guarded default rollout evidence."""

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
OUTPUT_NAME = "rollout_v0.2.93_complexity_router_guarded_default"


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
        json={"name": "Guarded default evidence", "requirement": requirement},
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


def build_evidence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="v02_93_router_") as temp:
        temp_root = Path(temp)
        default_settings = Settings(
            api_token="test-token",
            data_dir=temp_root / "default-data",
            workspace_root=temp_root / "default-work",
        )
        default_app = create_app(default_settings, QuietProvider())
        with TestClient(default_app) as client:
            safety = client.get("/api/v1/platform/complexity-router/default-safety", headers=headers()).json()
            plan = client.get("/api/v1/platform/complexity-router/default-enableable-plan", headers=headers()).json()
            simple_app_id = create_application(client, "Fix a typo in a settings label.")
            simple_build = create_build(client, simple_app_id, "Fix a typo in a settings label.")
            simple_state = client.get(f"/api/v1/builds/{simple_build['build_id']}", headers=headers()).json()
            unknown_app_id = create_application(client, "")
            unknown_build = create_build(client, unknown_app_id, "          ")
            override_app_id = create_application(client, "Add an API endpoint with tests.")
            override_build = create_build(
                client,
                override_app_id,
                "Add an API endpoint with tests.",
                planning_mode="disabled",
            )

        rollback_settings = Settings(
            api_token="test-token",
            data_dir=temp_root / "rollback-data",
            workspace_root=temp_root / "rollback-work",
            complexity_router_default_mode="disabled",
            complexity_router_limited_default_enabled=False,
        )
        rollback_app = create_app(rollback_settings, QuietProvider())
        with TestClient(rollback_app) as client:
            rollback_plan = client.get("/api/v1/platform/complexity-router/default-enableable-plan", headers=headers()).json()
            rollback_app_id = create_application(client, "Fix a typo in a settings label.")
            rollback_build = create_build(client, rollback_app_id, "Fix a typo in a settings label.")

    frontend = run_frontend_verification()
    checks = {
        "default_safety_enabled": safety["default_enabled"] is True,
        "default_plan_enabled": plan["default_enabled"] is True,
        "simple_default_active": simple_build["complexity_router"]["active"] is True
        and simple_state["team_state"]["runtime_builder_policy"]["reuse_depth"] == "shallow",
        "unknown_bypassed": unknown_build["complexity_router"]["active"] is False
        and unknown_build["complexity_router"]["classification"]["conservative_unknown"] is True,
        "request_override_visible": override_build["complexity_router"]["planning_mode_source"] == "request_override",
        "rollback_disabled": rollback_plan["default_enabled"] is False
        and rollback_build["complexity_router"]["active"] is False,
        "frontend_passed": frontend["passed"] is True,
    }
    return {
        "version": "v0.2.93",
        "rollout_id": "complexity_router_guarded_default_rollout",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.92_complexity_router_limited_default_readiness_review.md",
        "status": "completed" if all(checks.values()) else "failed",
        "checks": checks,
        "default_settings": {
            "complexity_router_default_mode": default_settings.complexity_router_default_mode,
            "complexity_router_limited_default_enabled": default_settings.complexity_router_limited_default_enabled,
        },
        "default_safety": safety,
        "default_plan": plan,
        "simple_default_build": simple_build,
        "simple_default_team_state": simple_state["team_state"],
        "unknown_default_build": unknown_build,
        "request_override_build": override_build,
        "rollback_plan": rollback_plan,
        "rollback_build": rollback_build,
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
        "# v0.2.93 complexity-router guarded default rollout",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Default mode: `{result['default_settings']['complexity_router_default_mode']}`",
        f"- Default limited enabled: `{result['default_settings']['complexity_router_limited_default_enabled']}`",
        f"- Default safety enabled: `{result['default_safety']['default_enabled']}`",
        f"- Default plan enabled: `{result['default_plan']['default_enabled']}`",
        f"- Simple default active: `{result['simple_default_build']['complexity_router']['active']}`",
        f"- Simple runtime reuse depth: `{result['simple_default_team_state']['runtime_builder_policy']['reuse_depth']}`",
        f"- Unknown default active: `{result['unknown_default_build']['complexity_router']['active']}`",
        f"- Request override source: `{result['request_override_build']['complexity_router']['planning_mode_source']}`",
        f"- Rollback plan enabled: `{result['rollback_plan']['default_enabled']}`",
        f"- Rollback build active: `{result['rollback_build']['complexity_router']['active']}`",
        f"- Frontend verification passed: `{result['frontend_verification']['passed']}`",
        "",
        "## Rollout Boundary",
        "",
        "- Normal settings now use guarded limited-default routing.",
        "- Explicit `disabled` settings remain the rollback path.",
        "- Unknown requirements remain bypassed and complex-equivalent.",
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
