from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from ..models import AgentSpec, ToolDefinition
from ..sandbox import SandboxSession


@dataclass(slots=True)
class ToolResult:
    content: str
    is_error: bool = False


@dataclass(slots=True)
class ToolContext:
    session_id: str
    agent: AgentSpec
    sandbox: SandboxSession
    emit: Callable[[str, dict[str, Any]], Awaitable[None]]
    spawn_subagent: Callable[[str, str | None], Awaitable[str]]


class Tool(ABC):
    name: str
    description: str
    input_model: type[BaseModel]
    dangerous: bool = False
    mutating: bool = False

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_model.model_json_schema(),
        )

    @abstractmethod
    async def execute(self, data: dict[str, Any], context: ToolContext) -> ToolResult: ...


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as error:
            raise KeyError(f"unknown tool: {name}") from error

    def names(self) -> list[str]:
        return sorted(self._tools)

    def definitions_for(self, agent: AgentSpec) -> list[ToolDefinition]:
        allowed = set(agent.tools or self._tools)
        allowed -= set(agent.disallowed_tools)
        if not agent.allow_subagents:
            allowed.discard("Agent")
        if not agent.skills:
            allowed.discard("Skill")
        if not agent.mcp_servers:
            allowed.discard("MCP")
        return [self._tools[name].definition() for name in sorted(allowed) if name in self._tools]
