from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from agent_platform.config import Settings
from agent_platform.models import (
    AgentSpec,
    ChatMessage,
    MCPServerSpec,
    NetworkPolicy,
    PermissionMode,
    StreamEvent,
    ToolDefinition,
)
from agent_platform.permissions import PermissionBroker
from agent_platform.platform_harness import PlatformHarness, PlatformHarnessViolation
from agent_platform.providers.base import ModelProvider, ProviderCapabilities
from agent_platform.runtime import AgentRuntime
from agent_platform.sandbox import CommandResult
from agent_platform.storage import Storage
from agent_platform.tools import build_core_registry


class ScriptedProvider(ModelProvider):
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
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 10}}})
        if self.calls == 1:
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "thinking", "thinking": ""},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "thinking_delta", "thinking": "need a file"}
            })
            yield StreamEvent(type="content_block_start", data={
                "index": 1,
                "content_block": {"type": "tool_use", "id": "t1", "name": "Write", "input": {}},
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": '{"path":"done.txt","content":"ok"}'},
            })
            yield StreamEvent(type="content_block_stop", data={"index": 1})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 20}
            })
        else:
            yield StreamEvent(type="content_block_start", data={
                "index": 0, "content_block": {"type": "text", "text": ""}
            })
            yield StreamEvent(type="content_block_delta", data={
                "index": 0, "delta": {"type": "text_delta", "text": "completed"}
            })
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}
            })


class FakeSandbox:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.writes: list[str] = []

    async def run(self, argv: list[str], *, stdin: str | None = None, **_: Any) -> CommandResult:
        if stdin:
            self.writes.append(stdin)
        return CommandResult(stdout="wrote done.txt\n", stderr="", exit_code=0)


class FakeSandboxes:
    def __init__(self, workspace: Path) -> None:
        self.sandbox = FakeSandbox(workspace)

    async def get_or_create(self, *_: Any, **__: Any) -> FakeSandbox:
        return self.sandbox


async def _runtime_for_stdio_mcp_policy(
    tmp_path: Path,
    *,
    platform_policy: str = "full",
) -> AgentRuntime:
    settings = Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    settings.prepare()
    storage = Storage(settings.data_dir)
    await storage.initialize()
    return AgentRuntime(
        settings=settings,
        storage=storage,
        provider=ScriptedProvider(),
        tools=build_core_registry(),
        sandboxes=FakeSandboxes(settings.workspace_root),  # type: ignore[arg-type]
        permissions=PermissionBroker(),
        harness=PlatformHarness(storage=storage, network_egress_policy=platform_policy),
    )


def _stdio_mcp_agent(*, network_policy: NetworkPolicy) -> AgentSpec:
    return AgentSpec(
        name="stdio mcp",
        description="tests stdio MCP policy",
        system_prompt="Call the configured MCP server only when allowed by policy.",
        tools=["MCP"],
        mcp_servers=[MCPServerSpec(name="local", transport="stdio", command="python")],
        permission_mode=PermissionMode.bypass,
        network_policy=network_policy,
    )


@pytest.mark.asyncio
async def test_runtime_blocks_stdio_mcp_when_agent_network_is_restricted(tmp_path: Path) -> None:
    runtime = await _runtime_for_stdio_mcp_policy(tmp_path)
    agent = _stdio_mcp_agent(network_policy=NetworkPolicy.none)

    with pytest.raises(PlatformHarnessViolation, match="stdio MCP egress policy blocked"):
        runtime._enforce_tool_network_policy(agent, "MCP", {"server": "local"})


@pytest.mark.asyncio
async def test_runtime_blocks_stdio_mcp_when_platform_network_is_restricted(tmp_path: Path) -> None:
    runtime = await _runtime_for_stdio_mcp_policy(tmp_path, platform_policy="allowlist")
    agent = _stdio_mcp_agent(network_policy=NetworkPolicy.full)

    with pytest.raises(PlatformHarnessViolation, match="stdio MCP egress policy blocked"):
        runtime._enforce_tool_network_policy(agent, "MCP", {"server": "local"})


@pytest.mark.asyncio
async def test_runtime_allows_stdio_mcp_guard_with_full_network_policies(tmp_path: Path) -> None:
    runtime = await _runtime_for_stdio_mcp_policy(tmp_path, platform_policy="full")
    agent = _stdio_mcp_agent(network_policy=NetworkPolicy.full)

    runtime._enforce_tool_network_policy(agent, "MCP", {"server": "local"})


@pytest.mark.asyncio
async def test_runtime_executes_tool_loop_and_persists_events(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    settings.prepare()
    storage = Storage(settings.data_dir)
    await storage.initialize()
    provider = ScriptedProvider()
    sandboxes = FakeSandboxes(settings.workspace_root)
    harness = PlatformHarness(storage=storage)
    runtime = AgentRuntime(
        settings=settings,
        storage=storage,
        provider=provider,
        tools=build_core_registry(),
        sandboxes=sandboxes,  # type: ignore[arg-type]
        permissions=PermissionBroker(),
        harness=harness,
    )
    spec = AgentSpec(
        name="writer",
        description="writes a file",
        system_prompt="Use the available tools to create the requested file and verify it.",
        tools=["Write"],
        permission_mode=PermissionMode.bypass,
    )
    version = await storage.save_agent_version(spec, "published")
    session = await runtime.create_session(spec, version, ".")
    answer = await runtime.run_turn_and_wait(session, "create it")
    assert answer == "completed"
    assert provider.calls == 2
    assert sandboxes.sandbox.writes == ["ok"]
    event_types = [event.type for event in await storage.list_events(session.id)]
    assert "model.thinking.delta" in event_types
    assert "tool.started" in event_types
    assert "tool.completed" in event_types
