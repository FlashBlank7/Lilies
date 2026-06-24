from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

import pytest

from agent_platform.config import Settings
from agent_platform.factory import AgentFactory
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.permissions import PermissionBroker
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.runtime import AgentRuntime
from agent_platform.sandbox import SandboxManager
from agent_platform.storage import Storage
from agent_platform.tools import build_core_registry


class GeneratorProvider(ModelProvider):
    name = "deepseek"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 1_000_000, 384_000)

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
        spec = {
            "name": "Python Test Fixer",
            "description": "Diagnoses and fixes Python test failures",
            "system_prompt": "Run the tests first, identify the root cause, make minimal edits, and run tests again.",
            "tools": ["Read", "Edit", "Grep", "Bash"],
            "provider_profile": {"provider": "deepseek", "model": "deepseek-v4-flash"},
            "permission_mode": "default",
            "network_policy": "full",
            "validation": {"prompt": "Run tests and repair failures", "commands": ["pytest -q"]},
        }
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0,
            "content_block": {"type": "tool_use", "id": "g1", "name": "create_agent_spec", "input": {}},
        })
        yield StreamEvent(type="content_block_delta", data={
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(spec)},
        })
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}
        })


@pytest.mark.asyncio
async def test_factory_generates_valid_platform_agent(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    settings.prepare()
    storage = Storage(settings.data_dir)
    await storage.initialize()
    provider = GeneratorProvider()
    tools = build_core_registry()
    sandboxes = SandboxManager(settings)
    runtime = AgentRuntime(
        settings=settings,
        storage=storage,
        provider=provider,
        tools=tools,
        sandboxes=sandboxes,
        permissions=PermissionBroker(),
    )
    factory = AgentFactory(
        settings=settings,
        storage=storage,
        provider=provider,
        runtime=runtime,
        tools=tools,
        sandboxes=sandboxes,
    )
    spec = await factory._generate_spec("generation-1", "Build an agent that fixes Python tests")
    assert spec.name == "Python Test Fixer"
    assert spec.provider_profile.provider == "deepseek"
    assert set(spec.tools) == {"Read", "Edit", "Grep", "Bash"}

