from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from tests.test_runtime import ScriptedProvider


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


class TemplateSuggestionOnlyProvider(ModelProvider):
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


def create_test_application(client: TestClient, requirement: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=headers(),
        json={"name": "Router runtime", "requirement": requirement},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_v02_90_default_settings_do_not_activate_runtime_builder_policy(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "work",
        complexity_router_default_mode="disabled",
        complexity_router_limited_default_enabled=False,
    )
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        app_id = create_test_application(client, "Fix a typo in a settings label.")
        response = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Fix a typo in a settings label.", "auto_publish": False, "max_turns": 5},
        )
        build = client.get(f"/api/v1/builds/{response.json()['build_id']}", headers=headers()).json()

    assert response.status_code == 202, response.text
    assert response.json()["complexity_router"]["active"] is False
    assert response.json()["complexity_router"]["effective_planning_mode"] == "auto"
    assert build["team_state"]["planning_mode"] == "auto"
    assert build["team_state"]["runtime_builder_policy"] is None


def test_v02_90_explicit_limited_default_activates_simple_builder_policy(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "work",
        complexity_router_default_mode="limited_default",
        complexity_router_limited_default_enabled=True,
    )
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        app_id = create_test_application(client, "Fix a typo in a settings label.")
        response = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Fix a typo in a settings label.", "auto_publish": False, "max_turns": 5},
        )
        build = client.get(f"/api/v1/builds/{response.json()['build_id']}", headers=headers()).json()

    router = response.json()["complexity_router"]
    assert router["active"] is True
    assert router["effective_planning_mode"] == "disabled"
    assert router["runtime_builder_policy"]["reuse_depth"] == "shallow"
    assert build["team_state"]["planning_mode"] == "disabled"
    assert build["team_state"]["complexity_router"]["active"] is True
    assert build["team_state"]["runtime_builder_policy"]["plan_first"] is False


def test_v02_90_runtime_policy_controls_omitted_template_suggestion_depth(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "work",
        complexity_router_default_mode="limited_default",
        complexity_router_limited_default_enabled=True,
    )
    app = create_app(settings, TemplateSuggestionOnlyProvider())

    with TestClient(app) as client:
        app_id = create_test_application(client, "Fix a typo in a settings label.")
        response = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "Fix a typo in a settings label.", "auto_publish": False, "max_turns": 5},
        )
        build_id = response.json()["build_id"]
        for _ in range(100):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"ready", "needs_attention", "published", "cancelled"}:
                break
            time.sleep(0.01)
        events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()

    suggestion_events = [
        event for event in events
        if event["type"] == "build.operation" and event["data"].get("tool") == "template_suggestions"
    ]
    assert suggestion_events
    result = json.loads(suggestion_events[0]["data"]["result"])
    assert result["reuse_depth"] == "shallow"
    assert result["reuse_depth_source"] == "complexity_router"
    assert result["defaulted_by_policy"] is True
    assert result["default_policy_version"] == "v0.2.90_complexity_router_runtime_activation_path"
    assert result["execution_contract"]["preserve_reuse_depth_source"] == "complexity_router"


def test_v02_90_unknown_requirement_keeps_runtime_activation_disabled(tmp_path: Path) -> None:
    settings = Settings(
        api_token="test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "work",
        complexity_router_default_mode="limited_default",
        complexity_router_limited_default_enabled=True,
    )
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        app_id = create_test_application(client, "")
        response = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={"requirement": "          ", "auto_publish": False, "max_turns": 5},
        )
        build = client.get(f"/api/v1/builds/{response.json()['build_id']}", headers=headers()).json()

    assert response.status_code == 202, response.text
    router = response.json()["complexity_router"]
    assert router["classification"]["effective_class"] == "complex"
    assert router["classification"]["conservative_unknown"] is True
    assert router["active"] is False
    assert router["effective_planning_mode"] == "auto"
    assert build["team_state"]["runtime_builder_policy"] is None
