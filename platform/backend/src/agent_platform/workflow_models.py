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
    degraded = "degraded"             # Mark degraded, inject warning, continue
    retry_with_fallback = "retry_with_fallback"  # Retry N times, fallback if exhausted


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


class NodeContract(BaseModel):
    """Runtime-enforced I/O contract for a workflow node.

    When *enforce* is True, the runtime validates that the node's actual
    output matches the declared *outputs* schema.  Warnings are emitted
    for input mismatches; output mismatches are errors unless *lenient*
    is set.
    """

    inputs: dict[str, str] = Field(
        default_factory=dict,
        description="field_name → type_string (e.g. {'task': 'string'})",
    )
    outputs: dict[str, str] = Field(
        default_factory=dict,
        description="field_name → type_string that this node guarantees to produce",
    )
    enforce: bool = Field(
        default=False,
        description="When True the runtime validates inputs and outputs",
    )
    lenient: bool = Field(
        default=True,
        description="When True, missing output keys are warnings, not errors",
    )


class BlockDefinition(BaseModel):
    type: str
    version: int = 1
    title: str
    description: str
    category: Literal["input", "model", "agent", "logic", "transform", "integration", "output"]
    block_kind: Literal["business_workflow", "agent_architecture", "legacy_compatibility"] = "business_workflow"
    config_schema: dict[str, Any]
    input_ports: list[PortDefinition] = Field(default_factory=list)
    output_ports: list[PortDefinition] = Field(default_factory=list)
    supports_retry: bool = False
    supports_error_branch: bool = False
    available: bool = True
    manual_summary: str = ""
    when_to_use: list[str] = Field(default_factory=list)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    anti_patterns: list[str] = Field(default_factory=list)
    common_errors: list[str] = Field(default_factory=list)
    claude_architecture_mapping: str | None = None
    composability_constraints: list[str] = Field(default_factory=list)
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
    contract: NodeContract | None = Field(
        default=None,
        description="Runtime-enforced I/O contract for this node",
    )
    degraded_value: Any = Field(
        default=None,
        description="Fallback value used when error_strategy=degraded",
    )
    fallback_value: Any = Field(
        default=None,
        description="Fallback value used when error_strategy=retry_with_fallback and retries exhausted",
    )


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
    operator: Literal[
        # Structural (deterministic — independent of LLM output)
        "exists", "type", "min_length", "max_length",
        # Content (non-deterministic — depends on LLM output)
        "equals", "contains", "not_contains",
    ] = "exists"
    expected: Any = None
    structural: bool = Field(
        default=False,
        description="When True, the assertion only checks structural properties "
        "(exists, type, length) and ignores content comparisons. "
        "Useful for testing workflows that include LLM calls."
    )


class WorkflowTestCase(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    requirement: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    assertions: list[TestAssertion] = Field(default_factory=list)
    required_node_types: list[str] = Field(default_factory=list)
    required_tool_nodes: list[str] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    minimum_tool_calls: int = Field(default=0, ge=0, le=100)
    require_cited_tool_urls: bool = False
    mandatory: bool = True
    structural_only: bool = Field(
        default=False,
        description="When True, all content assertions are downgraded to structural "
        "checks. LLM output variability won't cause test failures."
    )
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
    max_repair_cycles: int = Field(default=4, ge=1, le=30)


class WorkflowRunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    use_draft: bool = False
    workspace_path: str = "."


class ResumeRunRequest(BaseModel):
    values: dict[str, Any]


class ManualScheduleTriggerRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)


class ClyinsRunRequest(BaseModel):
    """Request to run the Clyins AI project manager workflow on meeting input."""
    meeting_transcript: str = Field(min_length=50, max_length=50_000)
    team_context: str = Field(default="", max_length=5_000)
    meeting_date: str = Field(default="", max_length=50)


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
    manual_lookups: list[str] = Field(default_factory=list)
    revision: int = 0
    published_version: int | None = None
    repair_cycles: int = 0
    expanded_from_template: str | None = None
