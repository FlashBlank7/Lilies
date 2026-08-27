"""服务端管家智能体：工具在服务端执行、动作入账、最终答复带真实数据。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


class ConciergeScript(ModelProvider):
    """第一轮点 list_workflows 工具，第二轮基于工具结果作答。"""

    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(self, *, model, system, messages, tools, max_output_tokens,
                     thinking_enabled, effort, tool_choice=None, user_id=None) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            assert any(t.name == "run_workflow" for t in tools)
            yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {
                "type": "tool_use", "id": "c1", "name": "list_workflows", "input": {}}})
            yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {
                "type": "input_json_delta", "partial_json": "{}"}})
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
            return
        payload = json.loads(messages[-1].content[0].content)
        text = f"平台上有 {payload['total']} 个已发布工作流"
        yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {"type": "text_delta", "text": text}})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


def test_concierge_executes_tools_server_side(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    app = create_app(settings, ConciergeScript())
    with TestClient(app) as client:
        r = client.post("/api/v1/assistant/agent",
                        headers={"Authorization": "Bearer workflow-test"},
                        json={"messages": [{"role": "user", "text": "有哪些工作流？"}]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["actions"] == [{"tool": "list_workflows", "summary": "0 个工作流"}]
        assert data["text"] == "平台上有 0 个已发布工作流"
