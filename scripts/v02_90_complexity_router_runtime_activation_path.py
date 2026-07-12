#!/usr/bin/env python3
"""Generate v0.2.90 complexity-router runtime activation evidence."""

from __future__ import annotations

import json
import sys
import tempfile
import time
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
OUTPUT_NAME = "activation_v0.2.90_complexity_router_runtime_activation_path"


class TemplateSuggestionProbeProvider(ModelProvider):
    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

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
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "router-template-suggestions",
                    "name": "template_suggestions",
                    "input": {},
                },
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": json.dumps({"requirement": "Fix a typo"})},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
            return
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "text", "text": "inspected"},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "text_delta", "text": "inspected"},
        })
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def create_application(client: TestClient, requirement: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Router runtime evidence", "requirement": requirement},
    )
    response.raise_for_status()
    return str(response.json()["id"])


def create_build(client: TestClient, application_id: str, requirement: str) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/applications/{application_id}/builds",
        headers=headers(),
        json={"requirement": requirement, "auto_publish": False, "max_turns": 5},
    )
    response.raise_for_status()
    return response.json()


def wait_for_template_suggestion_result(client: TestClient, build_id: str) -> dict[str, Any]:
    for _ in range(100):
        build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
        if build["status"] in {"ready", "needs_attention", "published", "cancelled"}:
            break
        time.sleep(0.01)
    events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
    for event in events:
        if event["type"] == "build.operation" and event["data"].get("tool") == "template_suggestions":
            return json.loads(event["data"]["result"])
    raise RuntimeError("template_suggestions event not found")


def build_evidence() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="v02_90_router_") as temp:
        temp_root = Path(temp)
        default_settings = Settings(
            api_token="test-token",
            data_dir=temp_root / "default-data",
            workspace_root=temp_root / "default-work",
            complexity_router_default_mode="disabled",
            complexity_router_limited_default_enabled=False,
        )
        default_app = create_app(default_settings, TemplateSuggestionProbeProvider())
        with TestClient(default_app) as client:
            default_app_id = create_application(client, "Fix a typo in a settings label.")
            default_build = create_build(client, default_app_id, "Fix a typo in a settings label.")
            default_state = client.get(f"/api/v1/builds/{default_build['build_id']}", headers=headers()).json()

        enabled_settings = Settings(
            api_token="test-token",
            data_dir=temp_root / "enabled-data",
            workspace_root=temp_root / "enabled-work",
            complexity_router_default_mode="limited_default",
            complexity_router_limited_default_enabled=True,
        )
        enabled_app = create_app(enabled_settings, TemplateSuggestionProbeProvider())
        with TestClient(enabled_app) as client:
            simple_app_id = create_application(client, "Fix a typo in a settings label.")
            simple_build = create_build(client, simple_app_id, "Fix a typo in a settings label.")
            simple_state = client.get(f"/api/v1/builds/{simple_build['build_id']}", headers=headers()).json()
            suggestion_result = wait_for_template_suggestion_result(client, simple_build["build_id"])

            unknown_app_id = create_application(client, "")
            unknown_build = create_build(client, unknown_app_id, "          ")
            unknown_state = client.get(f"/api/v1/builds/{unknown_build['build_id']}", headers=headers()).json()

    frontend = run_frontend_verification()
    checks = {
        "default_inactive": default_build["complexity_router"]["active"] is False
        and default_state["team_state"]["runtime_builder_policy"] is None,
        "simple_active": simple_build["complexity_router"]["active"] is True
        and simple_build["complexity_router"]["effective_planning_mode"] == "disabled"
        and simple_state["team_state"]["runtime_builder_policy"]["reuse_depth"] == "shallow",
        "template_suggestion_routed": suggestion_result["reuse_depth"] == "shallow"
        and suggestion_result["reuse_depth_source"] == "complexity_router",
        "unknown_inactive": unknown_build["complexity_router"]["active"] is False
        and unknown_state["team_state"]["runtime_builder_policy"] is None,
        "frontend_passed": frontend["passed"] is True,
    }
    return {
        "version": "v0.2.90",
        "activation_id": "complexity_router_runtime_activation_path",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.89_complexity_router_limited_default_enablement_contract.md",
        "status": "completed" if all(checks.values()) else "failed",
        "checks": checks,
        "default_build": default_build,
        "default_team_state": default_state["team_state"],
        "simple_build": simple_build,
        "simple_team_state": simple_state["team_state"],
        "simple_template_suggestion_result": suggestion_result,
        "unknown_build": unknown_build,
        "unknown_team_state": unknown_state["team_state"],
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
        "# v0.2.90 complexity-router runtime activation path",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Default settings active: `{result['default_build']['complexity_router']['active']}`",
        f"- Explicit simple active: `{result['simple_build']['complexity_router']['active']}`",
        f"- Explicit simple effective planning mode: `{result['simple_build']['complexity_router']['effective_planning_mode']}`",
        f"- Explicit simple runtime reuse depth: `{result['simple_team_state']['runtime_builder_policy']['reuse_depth']}`",
        f"- Omitted template suggestion reuse depth: `{result['simple_template_suggestion_result']['reuse_depth']}`",
        f"- Omitted template suggestion source: `{result['simple_template_suggestion_result']['reuse_depth_source']}`",
        f"- Unknown active: `{result['unknown_build']['complexity_router']['active']}`",
        f"- Frontend verification passed: `{result['frontend_verification']['passed']}`",
        "",
        "## Runtime Contract",
        "",
        "- Default settings do not activate runtime builder policy.",
        "- Explicit limited-default settings can activate simple routing and set `planning_mode=disabled`.",
        "- Builder `template_suggestions` without an explicit reuse depth uses runtime policy `shallow`.",
        "- Unknown requirements remain inactive and do not persist runtime builder policy.",
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
