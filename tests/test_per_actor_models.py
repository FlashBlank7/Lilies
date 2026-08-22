"""per-actor 模型（异构构建）：协调者与队友各用一套模型、白名单硬门、
send_message 追问不漂移、血缘落库。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.builder import WorkflowBuilder
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.workflow_models import BuildTeamState

import pytest


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


class ModelRecordingProvider(ModelProvider):
    """记录每次调用的 (user_id, model)；协调者先用越界模型试派队友（应被硬门拒绝），
    再用白名单模型成功派出，队友直接收工。"""

    name = "scripted"

    def __init__(self) -> None:
        self.models_by_user: dict[str, list[str]] = {}

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
        assert user_id is not None
        self.models_by_user.setdefault(user_id, []).append(model)
        call_number = len(self.models_by_user[user_id])
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})

        def tool_call(name: str, value: dict[str, Any], call_id: str):
            return [
                StreamEvent(type="content_block_start", data={
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
                }),
                StreamEvent(type="content_block_delta", data={
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(value)},
                }),
                StreamEvent(type="content_block_stop", data={"index": 0}),
                StreamEvent(type="message_delta", data={
                    "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
                }),
            ]

        def text_turn(text: str):
            return [
                StreamEvent(type="content_block_start", data={
                    "index": 0, "content_block": {"type": "text", "text": ""},
                }),
                StreamEvent(type="content_block_delta", data={
                    "index": 0, "delta": {"type": "text_delta", "text": text},
                }),
                StreamEvent(type="message_delta", data={
                    "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1},
                }),
            ]

        if user_id.endswith("-coordinator"):
            if call_number == 1:
                events = tool_call("spawn_teammate", {
                    "name": "schema-hand",
                    "task": "机械性配置工作",
                    "model": "local/forbidden-model",
                }, "coord-1")
            elif call_number == 2:
                events = tool_call("spawn_teammate", {
                    "name": "schema-hand",
                    "task": "机械性配置工作",
                    "model": "local/tiny-4b",
                }, "coord-2")
            elif call_number == 3:
                events = tool_call("send_message", {
                    "name": "schema-hand",
                    "message": "再确认一遍",
                }, "coord-3")
            else:
                events = text_turn("协调者收工")
        else:
            events = text_turn("队友收工")
        for event in events:
            yield event


def test_heterogeneous_build_routes_models_and_enforces_allowlist(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = ModelRecordingProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=headers(),
            json={"name": "Hetero", "requirement": "Route teammates to small models."},
        ).json()["id"]
        created = client.post(
            f"/api/v1/applications/{app_id}/builds",
            headers=headers(),
            json={
                "requirement": "Route teammates to small models.",
                "auto_publish": False,
                "max_turns": 10,
                "max_repair_cycles": 1,
                "coordinator_model": "scripted/coordinator-30b",
                "teammate_models": ["local/tiny-4b", "local/tiny-2b"],
            },
        ).json()
        build_id = created["build_id"]
        for _ in range(300):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] in {"needs_attention", "ready", "published", "failed"}:
                break
            time.sleep(0.01)

        # 协调者每一轮都用协调者模型
        coordinator_models = provider.models_by_user[f"{build_id}-coordinator"]
        assert set(coordinator_models) == {"scripted/coordinator-30b"}
        # 队友（spawn + send_message 追问）每一轮都用被指派的小模型，不漂回协调者模型
        teammate_models = provider.models_by_user[f"{build_id}-schema-hand"]
        assert teammate_models and set(teammate_models) == {"local/tiny-4b"}
        assert len(teammate_models) >= 2  # spawn 一轮 + 追问一轮

        # 越界模型被硬门拒绝，错误信息回给协调者（第 1 次 spawn 失败，第 2 次成功）
        events = client.get(f"/v1/streams/{build_id}", headers=headers()).json()
        spawn_ops = [
            event["data"] for event in events
            if event["type"] == "build.operation" and event["data"].get("tool") == "spawn_teammate"
        ]
        assert any("teammate model not allowed" in str(op.get("error") or op.get("result") or "") for op in spawn_ops)

        # 血缘落库：teammates 状态记录了模型
        assert build["team_state"]["teammates"]["schema-hand"]["model"] == "local/tiny-4b"
        assert build["team_state"]["coordinator_model"] == "scripted/coordinator-30b"
        assert build["team_state"]["teammate_models"] == ["local/tiny-4b", "local/tiny-2b"]


def test_resolve_teammate_model_allowlist() -> None:
    builder = object.__new__(WorkflowBuilder)  # 只测纯校验逻辑，不需要完整装配
    state = BuildTeamState(teammate_models=["local/tiny-4b"])

    assert builder._resolve_teammate_model(state, None) is None
    assert builder._resolve_teammate_model(state, "") is None
    assert builder._resolve_teammate_model(state, "local/tiny-4b") == "local/tiny-4b"
    with pytest.raises(RuntimeError, match="teammate model not allowed"):
        builder._resolve_teammate_model(state, "local/other")
    # 未声明模型池时，任何显式指派都越界（只能跟随协调者）
    with pytest.raises(RuntimeError, match="coordinator model only"):
        builder._resolve_teammate_model(BuildTeamState(), "local/tiny-4b")
