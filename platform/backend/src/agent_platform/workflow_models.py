from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .capability_contracts import (
    AcceptanceEvidenceTarget,
    CapabilityBuildContract,
    CarrierType,
)
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


class DeliveryMode(str, Enum):
    quick = "quick"
    guided = "guided"
    governed = "governed"


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


class TestFrameSpec(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = Field(
        default="",
        description="Human-readable test frame title, e.g. Outline and setting adherence.",
    )
    category: Literal["structure", "tooling", "content", "safety", "human_review", "custom"] = "custom"
    purpose: str = Field(
        default="",
        description="What this test frame proves in the larger acceptance framework.",
    )
    reviewer_guidance: str = Field(
        default="",
        description="How a human reviewer should interpret the test result.",
    )
    reference: str = Field(
        default="",
        description="Requirement, document section, or acceptance source this frame is tied to.",
    )
    failure_target: str = Field(
        default="",
        description="Likely node, block type, or behavior to inspect when this frame fails.",
    )


class WorkflowTestCase(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    requirement: str
    frame: TestFrameSpec | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    simulated_human_inputs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        max_length=100,
        description=(
            "Test-only typed responses keyed by human_input node id. The runtime "
            "injects them internally; production run inputs cannot use reserved keys."
        ),
    )
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
    feedback_hints: list[str] = Field(
        default_factory=list,
        description="Human-readable hints for local repair when this test fails.",
    )
    capability_ids: list[str] = Field(
        default_factory=list,
        description="Capability Build Contract ids this case is intended to verify.",
    )
    evidence_target: AcceptanceEvidenceTarget | None = Field(
        default=None,
        description="Scoped evidence level and environment expected from this case.",
    )


class ApplicationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    mode: ApplicationMode = ApplicationMode.workflow
    delivery_mode: DeliveryMode = DeliveryMode.guided
    governed_hard_gate: bool = False
    requirement: str
    capability_build_contract: CapabilityBuildContract | None = None
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
    delivery_mode: DeliveryMode = DeliveryMode.guided
    governed_hard_gate: bool = False
    capability_build_contract: CapabilityBuildContract | None = None


class PublishApplicationRequest(BaseModel):
    acknowledge_warnings: bool = False


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
        "set_capability_build_contract",
    ]
    data: dict[str, Any] = Field(default_factory=dict)


class BuildRequest(BaseModel):
    requirement: str = Field(min_length=10, max_length=30_000)
    auto_publish: bool = True
    max_turns: int = Field(default=36, ge=5, le=200)
    max_repair_cycles: int = Field(default=4, ge=1, le=30)
    max_elapsed_seconds: float | None = Field(default=480.0, ge=0.001, le=86_400)
    planning_mode: Literal["auto", "required", "disabled"] = "auto"


class WorkflowRunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = None
    use_draft: bool = False
    workspace_path: str = "."


class ResumeRunRequest(BaseModel):
    values: dict[str, Any]


class ManualScheduleTriggerRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=200)


class WorkflowRunState(BaseModel):
    run_id: str
    application_id: str
    snapshot: ApplicationSnapshot
    inputs: dict[str, Any]
    workspace_path: str
    # Optional execution policy set only by trusted platform callers.  Legacy
    # API runs omit both fields and retain their historical behavior.  Keeping
    # the policy in the persisted run state makes pause/resume and process
    # recovery preserve the original black-box boundary.
    workspace_boundary: str | None = None
    allowed_nested_application_ids: list[str] | None = None
    allowed_runtime_tools: list[str] | None = None
    allowed_network_hosts: list[str] | None = None
    model_access: bool | None = None
    allowed_connector_operations: list[str] | None = None
    writable_connector_operations: list[str] | None = None
    permission_required_connector_operations: list[str] | None = None
    compensation_connector_operations: list[str] | None = None
    max_connector_write_count: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
    )
    max_connector_payload_bytes: int | None = Field(
        default=None,
        ge=1,
        le=100 * 1024 * 1024,
    )
    governed_host_actions: bool = False
    # Public task runs may authorize a connector mutation only after workflow
    # references resolve to one concrete payload. These trusted, digest-only
    # fields preserve the exact task-policy ceiling across pause/resume without
    # persisting a bearer token or broad owner authority.
    connector_descriptor_digests: dict[str, str] | None = None
    task_credential_ref_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    task_policy_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    allowed_actions_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    budget_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    task_deadline_at: str | None = None
    # A published version may carry an immutable execution-policy snapshot.
    # The first digest names that version-bound ceiling; the second names the
    # effective policy after a task caller has optionally narrowed it.
    published_execution_policy_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    execution_policy_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    connector_write_count: int = Field(default=0, ge=0, le=1_000_000)
    connector_write_keys: list[str] = Field(
        default_factory=list,
        max_length=1_000_000,
    )
    runtime_connector_authorization_ids: dict[str, str] = Field(
        default_factory=dict,
        max_length=1_000_000,
    )
    # Black-box runs are owned by one exact task assignment and one exact
    # Lilies session.  Legacy/internal runs leave both values unset and are
    # intentionally undiscoverable through the public task-token facade.
    assignment_id: str | None = None
    session_id: str | None = None
    # Ordered application ancestry, including this run's application.  It is
    # persisted so nested workflow recursion/depth gates survive pause/resume
    # and process recovery instead of relying on an in-memory call stack.
    application_call_chain: list[str] = Field(default_factory=list, max_length=16)
    outputs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    completed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    waiting_node_id: str | None = None
    resumed_values: dict[str, Any] | None = None
    human_input_values: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        max_length=1_000_000,
    )
    created_at: str = Field(default_factory=utc_now)


class BuildTask(BaseModel):
    id: int
    subject: str
    description: str = ""
    status: Literal["pending", "in_progress", "completed", "blocked"] = "pending"
    owner: str | None = None
    blocked_by: list[int] = Field(default_factory=list)
    acceptance: list[str] = Field(default_factory=list)


class BuildPlanModule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    title: str
    purpose: str = ""
    status: Literal["planned", "building", "tested", "blocked", "done"] = "planned"
    depends_on: list[str] = Field(default_factory=list)
    expected_blocks: list[str] = Field(default_factory=list)
    draft_node_ids: list[str] = Field(default_factory=list)
    test_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    capability_ids: list[str] = Field(default_factory=list)
    carrier_type: CarrierType | None = None
    reusable_module_ref: str | None = Field(
        default=None,
        pattern=r"^module:[A-Za-z][A-Za-z0-9_.-]{1,119}@[1-9][0-9]*$",
    )
    evidence_targets: list[AcceptanceEvidenceTarget] = Field(default_factory=list)


class BuildPlan(BaseModel):
    goal: str
    strategy: str = ""
    modules: list[BuildPlanModule] = Field(default_factory=list)
    reuse_depth: Literal["none", "shallow", "deep"] = "none"
    complexity: Literal["simple", "medium", "complex"] = "medium"
    risks: list[str] = Field(default_factory=list)
    capability_contract_id: str | None = None
    claim_scope: str = ""


class TeammateState(BaseModel):
    name: str
    purpose: str
    status: Literal["working", "idle", "completed", "failed"] = "working"
    messages: list[dict[str, Any]] = Field(default_factory=list)
    mailbox: list[str] = Field(default_factory=list)


class BuildTeamState(BaseModel):
    tasks: list[BuildTask] = Field(default_factory=list)
    build_plan: BuildPlan | None = None
    complexity_router: dict[str, Any] | None = None
    runtime_builder_policy: dict[str, Any] | None = None
    capability_build_contract: CapabilityBuildContract | None = None
    capability_closure: dict[str, Any] | None = None
    capability_routing: dict[str, Any] | None = None
    teammates: dict[str, TeammateState] = Field(default_factory=dict)
    coordinator_messages: list[dict[str, Any]] = Field(default_factory=list)
    manual_lookups: list[str] = Field(default_factory=list)
    revision: int = 0
    published_version: int | None = None
    repair_cycles: int = 0
    last_failed_test_revision: int | None = None
    planning_mode: Literal["auto", "required", "disabled"] = "auto"
