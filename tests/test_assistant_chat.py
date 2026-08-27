"""远端一般任务入口：/api/v1/assistant/chat 走服务端模型，鉴权必需。"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


class EchoProvider(ModelProvider):
    name = "scripted"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(
        self, *, model: str, system: str, messages: list[ChatMessage],
        tools: list[ToolDefinition], max_output_tokens: int, thinking_enabled: bool,
        effort: str, tool_choice=None, user_id=None,
    ) -> AsyncIterator[StreamEvent]:
        last = messages[-1].content[0].text or ""
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 3}}})
        yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {"type": "text_delta", "text": f"回声：{last}"}})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}})


def test_assistant_chat_serves_remote_general_tasks(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    app = create_app(settings, EchoProvider())
    with TestClient(app) as client:
        denied = client.post("/api/v1/assistant/chat", json={"messages": [{"role": "user", "text": "hi"}]})
        assert denied.status_code in (401, 403)
        ok = client.post(
            "/api/v1/assistant/chat",
            headers={"Authorization": "Bearer workflow-test"},
            json={"messages": [{"role": "user", "text": "你好"}]},
        )
        assert ok.status_code == 200, ok.text
        data = ok.json()
        assert data["text"] == "回声：你好"
        assert data["usage"]["output_tokens"] == 2
