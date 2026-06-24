from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import AgentSpec, utc_now


class ValueType(str, Enum):
    any = "any"
    string = "string"
    number = "number"
    boolean = "boolean"
    object = "object"
    array = "array"
    file = "file"
    file_list = "file_list"


class ApplicationMode(str, Enum):
    workflow = "workflow"
    chat = "chat"


class ErrorStrategy(str, Enum):
    fail = "fail"
    continue_on_error = "continue"
    error_branch = "error_branch"


class RetryPolicy(BaseModel):
    enabled: bool = False
    max_attempts: int = Field(default=1, ge=1, le=10)
    delay_seconds: float = Field(default=0.5, ge=0, le=60)


class PortDefinition(BaseModel):
    name: str
    value_type: ValueType = ValueType.any
    required: bool = False
    multiple: bool = False
    description: str = ""


class BlockDefinition(BaseModel):
    type: str
    version: int = 1
    title: str
    description: str
    category: Literal["input", "model", "agent", "logic", "transform", "integration", "output"]
    config_schema: dict[str, Any]
    input_ports: list[PortDefinition] = Field(default_factory=list)
    output_ports: list[PortDefinition] = Field(default_factory=list)
    supports_retry: bool = False
    supports_error_branch: bool = False
    available: bool = True
    editor: dict[str, Any] = Field(default_factory=dict)


class Position(BaseModel):
    x: float = 0
    y: float = 0


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    block_version: int = 1
    title: str
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    position: Position = Field(default_factory=Position)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    error_strategy: ErrorStrategy = ErrorStrategy.fail


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    source: str
    target: str
    source_port: str = "output"
    target_port: str = "input"
    branch: str | None = None


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[NodeSpec] = Field(default_factory=list)
    edges: list[EdgeSpec] = Field(default_factory=list)
    viewport: dict[str, float] = Field(default_factory=lambda: {"x": 0, "y": 0, "zoom": 0.8})

    @model_validator(mode="after")
    def validate_identity(self) -> "WorkflowSpec":
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("workflow contains duplicate node ids")
        edge_ids = [edge.id for edge in self.edges]
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("workflow contains duplicate edge ids")
        unknown = {
            endpoint
            for edge in self.edges
            for endpoint in (edge.source, edge.target)
            if endpoint not in set(node_ids)
        }
        if unknown:
            raise ValueError(f"edges reference unknown nodes: {sorted(unknown)}")
        return self


class TestAssertion(BaseModel):
    path: list[str] = Field(default_factory=list)
    operator: Literal["exists", "equals", "contains", "not_contains", "type"] = "exists"
    expected: Any = None


class WorkflowTestCase(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    requirement: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    assertions: list[TestAssertion] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    minimum_tool_calls: int = Field(default=0, ge=0, le=100)
    require_cited_tool_urls: bool = False
    mandatory: bool = True


class ApplicationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    mode: ApplicationMode = ApplicationMode.workflow
    requirement: str
    workflow: WorkflowSpec = Field(default_factory=WorkflowSpec)
    agents: dict[str, AgentSpec] = Field(default_factory=dict)
    tests: list[WorkflowTestCase] = Field(default_factory=list)

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()


class ApplicationCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    requirement: str = Field(default="", max_length=30_000)
    mode: ApplicationMode = ApplicationMode.workflow


class DraftOperation(BaseModel):
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    op: Literal[
        "add_node",
        "update_node",
        "remove_node",
        "add_edge",
        "remove_edge",
        "set_metadata",
        "upsert_agent",
        "add_test",
        "remove_test",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


class BuildRequest(BaseModel):
    requirement: str = Field(min_length=10, max_length=30_000)
    auto_publish: bool = True
    max_turns: int = Field(default=60, ge=5, le=200)
    max_repair_cycles: int = Field(default=8, ge=1, le=30)


class WorkflowRunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    use_draft: bool = False
    workspace_path: str = "."


class ResumeRunRequest(BaseModel):
    values: dict[str, Any]


class ManualScheduleTriggerRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


class WorkflowRunState(BaseModel):
    run_id: str
    application_id: str
    snapshot: ApplicationSnapshot
    inputs: dict[str, Any]
    workspace_path: str
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    completed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    waiting_node_id: str | None = None
    resumed_values: dict[str, Any] | None = None
    created_at: str = Field(default_factory=utc_now)


class BuildTask(BaseModel):
    id: int
    subject: str
    description: str = ""
    status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"
    owner: str | None = None
    blocked_by: list[int] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)


class TeammateState(BaseModel):
    name: str
    purpose: str
    status: Literal["working", "idle", "completed", "failed"] = "working"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    mailbox: list[str] = Field(default_factory=list)


class BuildTeamState(BaseModel):
    tasks: list[BuildTask] = Field(default_factory=list)
    teammates: dict[str, TeammateState] = Field(default_factory=dict)
    coordinator_messages: list[dict[str, Any]] = Field(default_factory=list)
    revision: int = 0
    published_version: int | None = None
    repair_cycles: int = 0
