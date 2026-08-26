"""思考三参数（thinking/effort/输出帽）从 BuildRequest 流到 provider 调用。

2026-08-26 归因：v4-pro-0813 思考常撞满硬编码的 8192 输出帽，整轮作废
（约六成时长损耗），缓存塌方是其下游。三参数放开是研究与修复的前提。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


def headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test"}


class ParamRecordingProvider(ModelProvider):
    name = "scripted"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 65_536)

    async def stream(
        self, *, model: str, system: str, messages: list[ChatMessage],
        tools: list[ToolDefinition], max_output_tokens: int, thinking_enabled: bool,
        effort: str, tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls.append({
            "thinking": thinking_enabled, "effort": effort, "cap": max_output_tokens,
        })
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {"type": "text_delta", "text": "收工"}})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


def test_thinking_params_flow_from_build_request_to_provider(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = ParamRecordingProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "思考参数", "requirement": "验证思考参数流转到模型调用。"},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers(),
            json={
                "requirement": "验证思考参数流转到模型调用。",
                "auto_publish": False, "max_turns": 5, "max_repair_cycles": 1,
                "thinking_enabled": False, "effort": "low",
                "turn_max_output_tokens": 2048,
            },
        ).json()["build_id"]
        for _ in range(200):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] not in {"queued", "building"}:
                break
            time.sleep(0.01)

        assert provider.calls, "model was never called"
        assert all(c == {"thinking": False, "effort": "low", "cap": 2048} for c in provider.calls), provider.calls
        assert build["team_state"]["thinking_enabled"] is False
        assert build["team_state"]["effort"] == "low"
        assert build["team_state"]["turn_max_output_tokens"] == 2048


def test_defaults_preserve_historical_behavior(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    provider = ParamRecordingProvider()
    app = create_app(settings, provider)
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications", headers=headers(),
            json={"name": "默认参数", "requirement": "不传思考参数时保持历史默认。"},
        ).json()["id"]
        build_id = client.post(
            f"/api/v1/applications/{app_id}/builds", headers=headers(),
            json={"requirement": "不传思考参数时保持历史默认。", "auto_publish": False,
                  "max_turns": 5, "max_repair_cycles": 1},
        ).json()["build_id"]
        for _ in range(200):
            build = client.get(f"/api/v1/builds/{build_id}", headers=headers()).json()
            if build["status"] not in {"queued", "building"}:
                break
            time.sleep(0.01)
        assert provider.calls
        assert all(c == {"thinking": True, "effort": "high", "cap": 8192} for c in provider.calls)
