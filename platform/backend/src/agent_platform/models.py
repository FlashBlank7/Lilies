from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class NetworkPolicy(str, Enum):
    full = "full"
    allowlist = "allowlist"
    none = "none"


class PermissionMode(str, Enum):
    default = "default"
    accept_edits = "accept_edits"
    bypass = "bypass"
    plan = "plan"


class ThinkingConfig(BaseModel):
    enabled: bool = True
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "xhigh"


class ProviderProfile(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-v4-flash"
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    max_output_tokens: int = Field(default=16_384, ge=256, le=384_000)
    context_window: int = Field(default=1_000_000, ge=8_000)


class SkillSpec(BaseModel):
    name: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9_-]+$")
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=50_000)


class MCPServerSpec(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    transport: Literal["http", "stdio"]
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)
    egress_hosts: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_transport(self) -> "MCPServerSpec":
        if self.transport == "http" and not self.url:
            raise ValueError("HTTP MCP server requires url")
        if self.transport == "stdio" and not self.command:
            raise ValueError("stdio MCP server requires command")
        return self


class ValidationSpec(BaseModel):
    prompt: str = Field(default="Inspect the workspace and demonstrate your primary capability.")
    commands: list[str] = Field(default_factory=list, max_length=10)


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=1000)
    system_prompt: str = Field(min_length=20, max_length=100_000)
    initial_prompt: str | None = Field(default=None, max_length=20_000)
    tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    skills: list[SkillSpec] = Field(default_factory=list)
    mcp_servers: list[MCPServerSpec] = Field(default_factory=list)
    provider_profile: ProviderProfile = Field(default_factory=ProviderProfile)
    permission_mode: PermissionMode = PermissionMode.default
    network_policy: NetworkPolicy = NetworkPolicy.full
    network_allowlist: list[str] = Field(default_factory=list)
    max_turns: int = Field(default=30, ge=1, le=200)
    max_budget_usd: float | None = Field(default=None, gt=0)
    allow_subagents: bool = True
    validation: ValidationSpec = Field(default_factory=ValidationSpec)

    @field_validator("tools", "disallowed_tools")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_policy(self) -> "AgentSpec":
        overlap = set(self.tools) & set(self.disallowed_tools)
        if overlap:
            raise ValueError(f"tools and disallowed_tools overlap: {sorted(overlap)}")
        if self.network_policy == NetworkPolicy.allowlist and not self.network_allowlist:
            raise ValueError("allowlist network policy requires network_allowlist")
        return self


class ContentBlock(BaseModel):
    type: Literal["text", "thinking", "tool_use", "tool_result"]
    text: str | None = None
    thinking: str | None = None
    signature: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    content: str | list[dict[str, Any]] | None = None
    is_error: bool | None = None
    input_parse_error: str | None = None
    """When the LLM's tool-use JSON is malformed, this contains the parse error
    and raw input so the Builder can retry with corrected JSON."""


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: list[ContentBlock]


class ToolDefinition(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int | None = None
    cost_usd: float = 0.0
    cost_source: Literal[
        "provider_reported",
        "estimated_configured_price",
        "unsupported",
    ] = "unsupported"
    field_support: dict[
        str,
        Literal["reported", "estimated", "unsupported", "not_reported"],
    ] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    blocks: list[ContentBlock]
    stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)


class StreamEvent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


class EventRecord(BaseModel):
    id: int
    stream_id: str
    type: str
    data: dict[str, Any]
    created_at: str = Field(default_factory=utc_now)


class GenerationRequest(BaseModel):
    requirement: str = Field(min_length=10, max_length=30_000)
    workspace_path: str | None = None
    validation_prompt: str | None = None
    auto_publish: bool = True


class SessionCreateRequest(BaseModel):
    agent_id: str
    agent_version: int | None = None
    workspace_path: str


class MessageRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class PermissionDecision(BaseModel):
    behavior: Literal["allow", "deny"]
    updated_input: dict[str, Any] | None = None
    message: str | None = None
