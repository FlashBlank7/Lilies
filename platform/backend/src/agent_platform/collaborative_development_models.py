"""Platform-neutral contracts for reusable Lilies/Codex development work.

These contracts deliberately do not import workflow, application, formal-task,
or oracle types.  A collaborative development assignment is an explicit
authority boundary for arbitrary software; it is not a privileged variant of a
Builder session.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, TypeAlias, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


T = TypeVar("T")

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$",
    ),
]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=240,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]*$",
    ),
]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, max_length=20_000)]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC timezone")
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must use UTC (offset +00:00)")
    return value.astimezone(timezone.utc)


def _unique(values: list[T] | tuple[T, ...], *, label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")


def _validate_relative_path(value: str, *, label: str) -> str:
    if "\x00" in value or "\\" in value:
        raise ValueError(f"{label} must be a normalized POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must stay inside the granted workspace")
    normalized = str(path)
    if normalized in {"", "."}:
        raise ValueError(f"{label} must identify a path below the workspace root")
    return normalized


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class AgentRole(str, Enum):
    lilies = "lilies"
    codex = "codex"


class DevelopmentTaskRole(str, Enum):
    implementer = "implementer"
    reviewer = "reviewer"
    coordinator = "coordinator"


class ApprovalMode(str, Enum):
    """Controls whether a human must approve each inter-agent handoff."""

    manual = "manual"
    auto_forward = "auto_forward"


class ExecutionMode(str, Enum):
    """Controls dispatch, never the authority contained in the frozen grant."""

    manual_dispatch = "manual_dispatch"
    autonomous = "autonomous"


class AssignmentStatus(str, Enum):
    active = "active"
    stopped = "stopped"
    closed = "closed"
    archived = "archived"


class WorkItemKind(str, Enum):
    feature = "feature"
    bug = "bug"
    refactor = "refactor"
    test = "test"
    review = "review"


class WorkItemStatus(str, Enum):
    proposed = "proposed"
    awaiting_dispatch = "awaiting_dispatch"
    leased = "leased"
    working = "working"
    ready_for_lilies_review = "ready_for_lilies_review"
    rework = "rework"
    accepted = "accepted"
    closed = "closed"
    cancelled = "cancelled"


class LeaseStatus(str, Enum):
    active = "active"
    released = "released"
    expired = "expired"
    revoked = "revoked"


class ReviewVerdict(str, Enum):
    accepted = "accepted"
    rework = "rework"


class SideEffect(str, Enum):
    workspace_write = "workspace_write"
    process_execute = "process_execute"
    git_commit = "git_commit"
    network_access = "network_access"
    external_mutation = "external_mutation"


class OutboxStatus(str, Enum):
    pending = "pending"
    delivered = "delivered"
    failed = "failed"
    cancelled = "cancelled"


class AgentRoleGrant(StrictModel):
    agent_role: AgentRole
    task_roles: tuple[DevelopmentTaskRole, ...] = Field(min_length=1)

    @field_validator("task_roles")
    @classmethod
    def task_roles_are_unique(
        cls, value: tuple[DevelopmentTaskRole, ...]
    ) -> tuple[DevelopmentTaskRole, ...]:
        _unique(value, label="task roles")
        return value


class WorkspaceGrant(StrictModel):
    """Frozen, role-scoped workspace and tool authority.

    ``allowed_argv`` contains exact argv vectors.  Shell strings are
    intentionally not accepted.  A later grant revision is required to add a
    path, command, host, secret reference, or side effect.
    """

    workspace_id: UUID
    agent_role: AgentRole
    workspace_root: str = Field(min_length=1, max_length=4_096)
    baseline_commit: CommitSha
    grant_revision: int = Field(default=1, ge=1)
    allowed_paths: tuple[str, ...] = Field(min_length=1)
    allowed_argv: tuple[tuple[str, ...], ...] = ()
    allowed_hosts: tuple[str, ...] = ()
    allowed_side_effects: tuple[SideEffect, ...] = ()
    secret_refs: tuple[Identifier, ...] = ()
    created_at: datetime

    @field_validator("workspace_root")
    @classmethod
    def root_is_absolute_and_normalized(cls, value: str) -> str:
        if "\x00" in value or "\\" in value or not value.startswith("/"):
            raise ValueError("workspace_root must be an absolute POSIX path")
        path = PurePosixPath(value)
        if ".." in path.parts or str(path) != value.rstrip("/"):
            raise ValueError("workspace_root must be normalized")
        if str(path) == "/":
            raise ValueError("workspace_root cannot be the filesystem root")
        return str(path)

    @field_validator("allowed_paths")
    @classmethod
    def paths_are_relative_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_validate_relative_path(path, label="allowed path") for path in value)
        _unique(normalized, label="allowed paths")
        return normalized

    @field_validator("allowed_argv")
    @classmethod
    def argv_is_exact_and_unique(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        normalized: list[tuple[str, ...]] = []
        for argv in value:
            if not argv or any(not isinstance(item, str) or not item.strip() for item in argv):
                raise ValueError("each allowed argv must contain non-empty string elements")
            if any("\x00" in item or "\n" in item or "\r" in item for item in argv):
                raise ValueError("allowed argv cannot contain NUL or newline characters")
            normalized.append(tuple(item.strip() for item in argv))
        _unique(normalized, label="allowed argv")
        return tuple(normalized)

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_are_exact_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for host in value:
            candidate = host.strip().lower()
            if (
                not candidate
                or "://" in candidate
                or "/" in candidate
                or "*" in candidate
                or any(char.isspace() for char in candidate)
            ):
                raise ValueError("allowed hosts must be exact host or host:port values")
            normalized.append(candidate)
        _unique(normalized, label="allowed hosts")
        return tuple(normalized)

    @field_validator("allowed_side_effects", "secret_refs")
    @classmethod
    def tuple_values_are_unique(cls, value: tuple[T, ...]) -> tuple[T, ...]:
        _unique(value, label="grant values")
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def network_authority_is_consistent(self) -> WorkspaceGrant:
        has_network_effect = SideEffect.network_access in self.allowed_side_effects
        if self.allowed_hosts and not has_network_effect:
            raise ValueError("allowed hosts require the network_access side effect")
        if has_network_effect and not self.allowed_hosts:
            raise ValueError("network_access requires at least one exact allowed host")
        if self.allowed_argv and SideEffect.process_execute not in self.allowed_side_effects:
            raise ValueError("allowed argv requires the process_execute side effect")
        return self


class DevelopmentBudget(StrictModel):
    max_work_items: int = Field(ge=1, le=100_000)
    max_commands: int = Field(ge=0, le=1_000_000)
    max_tool_calls: int = Field(ge=0, le=10_000_000)
    max_wall_seconds: int = Field(ge=1, le=31_536_000)
    max_cost_usd: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)


class DevelopmentAssignment(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    goal: str = Field(min_length=1, max_length=20_000)
    software_id: Identifier
    baseline_commit: CommitSha
    agent_roles: tuple[AgentRoleGrant, ...] = Field(min_length=2, max_length=2)
    workspace_grants: tuple[WorkspaceGrant, ...] = Field(min_length=2, max_length=2)
    budget: DevelopmentBudget
    deadline: datetime
    approval_mode: ApprovalMode = ApprovalMode.manual
    execution_mode: ExecutionMode = ExecutionMode.manual_dispatch
    status: AssignmentStatus = AssignmentStatus.active
    revision: int = Field(default=1, ge=1)
    enterprise_denominator: Literal[False] = False
    created_at: datetime
    updated_at: datetime

    @field_validator("deadline", "created_at", "updated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def assignment_authority_is_complete(self) -> DevelopmentAssignment:
        roles = [grant.agent_role for grant in self.agent_roles]
        if set(roles) != {AgentRole.lilies, AgentRole.codex}:
            raise ValueError("assignment must grant exactly Lilies and Codex roles")
        _unique(roles, label="agent roles")
        if not any(
            DevelopmentTaskRole.implementer in grant.task_roles for grant in self.agent_roles
        ):
            raise ValueError("at least one agent must be an implementer")
        if not any(
            grant.agent_role == AgentRole.lilies
            and DevelopmentTaskRole.reviewer in grant.task_roles
            for grant in self.agent_roles
        ):
            raise ValueError("Lilies must have the reviewer role")

        workspace_roles = [grant.agent_role for grant in self.workspace_grants]
        if set(workspace_roles) != set(roles):
            raise ValueError("each agent role requires one independent workspace grant")
        _unique(workspace_roles, label="workspace agent roles")
        _unique(
            [grant.workspace_id for grant in self.workspace_grants],
            label="workspace ids",
        )
        _unique(
            [grant.workspace_root for grant in self.workspace_grants],
            label="workspace roots",
        )
        workspace_roots = [PurePosixPath(grant.workspace_root) for grant in self.workspace_grants]
        if any(
            left in right.parents or right in left.parents
            for index, left in enumerate(workspace_roots)
            for right in workspace_roots[index + 1 :]
        ):
            raise ValueError("role workspace roots must not contain one another")
        if any(grant.baseline_commit != self.baseline_commit for grant in self.workspace_grants):
            raise ValueError("all workspaces must derive from the frozen baseline")
        if self.deadline <= self.created_at:
            raise ValueError("deadline must be later than created_at")
        if self.deadline > self.created_at + timedelta(seconds=self.budget.max_wall_seconds):
            raise ValueError("deadline cannot exceed the assignment wall-time budget")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class DevelopmentAssignmentProjection(StrictModel):
    """The assignment view exposed to one bound agent credential.

    The owner receives the complete :class:`DevelopmentAssignment`.  An agent
    receives only its own task-role and workspace grant, so discovering one
    role credential never reveals the other role's filesystem or secret
    references.
    """

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    goal: str = Field(min_length=1, max_length=20_000)
    software_id: Identifier
    baseline_commit: CommitSha
    agent_role: AgentRoleGrant
    workspace_grant: WorkspaceGrant
    budget: DevelopmentBudget
    deadline: datetime
    approval_mode: ApprovalMode
    execution_mode: ExecutionMode
    status: AssignmentStatus
    revision: int = Field(ge=1)
    enterprise_denominator: Literal[False] = False
    created_at: datetime
    updated_at: datetime

    @field_validator("deadline", "created_at", "updated_at")
    @classmethod
    def projection_timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def projection_role_is_consistent(self) -> DevelopmentAssignmentProjection:
        if self.agent_role.agent_role != self.workspace_grant.agent_role:
            raise ValueError("projected role and workspace grant must match")
        return self

    @classmethod
    def from_assignment(
        cls,
        assignment: DevelopmentAssignment,
        role: AgentRole,
    ) -> DevelopmentAssignmentProjection:
        task_grants = [grant for grant in assignment.agent_roles if grant.agent_role == role]
        workspace_grants = [
            grant for grant in assignment.workspace_grants if grant.agent_role == role
        ]
        if len(task_grants) != 1 or len(workspace_grants) != 1:
            raise ValueError("assignment has no unique projection for the requested role")
        return cls(
            assignment_id=assignment.assignment_id,
            goal=assignment.goal,
            software_id=assignment.software_id,
            baseline_commit=assignment.baseline_commit,
            agent_role=task_grants[0],
            workspace_grant=workspace_grants[0],
            budget=assignment.budget,
            deadline=assignment.deadline,
            approval_mode=assignment.approval_mode,
            execution_mode=assignment.execution_mode,
            status=assignment.status,
            revision=assignment.revision,
            enterprise_denominator=False,
            created_at=assignment.created_at,
            updated_at=assignment.updated_at,
        )


class DevelopmentWorkItem(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    work_item_id: UUID
    assignment_id: UUID
    kind: WorkItemKind
    objective: str = Field(min_length=1, max_length=20_000)
    acceptance: tuple[str, ...] = Field(min_length=1, max_length=100)
    dependencies: tuple[UUID, ...] = ()
    assigned_role: AgentRole
    status: WorkItemStatus = WorkItemStatus.proposed
    lease_revision: int = Field(default=0, ge=0)
    revision: int = Field(default=1, ge=1)
    parent_work_item_id: UUID | None = None
    evidence_refs: tuple[Digest, ...] = ()
    created_at: datetime
    updated_at: datetime

    @field_validator("acceptance")
    @classmethod
    def acceptance_is_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("acceptance items cannot be empty")
        normalized = tuple(item.strip() for item in value)
        _unique(normalized, label="acceptance items")
        return normalized

    @field_validator("dependencies", "evidence_refs")
    @classmethod
    def references_are_unique(cls, value: tuple[T, ...]) -> tuple[T, ...]:
        _unique(value, label="references")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def work_item_timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def work_item_is_consistent(self) -> DevelopmentWorkItem:
        if self.work_item_id in self.dependencies:
            raise ValueError("work item cannot depend on itself")
        if self.parent_work_item_id == self.work_item_id:
            raise ValueError("work item cannot be its own parent")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if (
            self.status
            in {
                WorkItemStatus.leased,
                WorkItemStatus.working,
                WorkItemStatus.ready_for_lilies_review,
                WorkItemStatus.rework,
                WorkItemStatus.accepted,
                WorkItemStatus.closed,
            }
            and self.lease_revision < 1
        ):
            raise ValueError("advanced work item states require a lease revision")
        return self


class DevelopmentLease(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    lease_id: UUID
    assignment_id: UUID
    work_item_id: UUID
    owner_role: AgentRole
    owner_id: Identifier
    fence: int = Field(ge=1)
    work_item_revision: int = Field(ge=1)
    status: LeaseStatus = LeaseStatus.active
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None = None

    @field_validator("acquired_at", "expires_at", "released_at")
    @classmethod
    def lease_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value) if value is not None else None

    @model_validator(mode="after")
    def lease_lifetime_is_consistent(self) -> DevelopmentLease:
        if self.expires_at <= self.acquired_at:
            raise ValueError("lease expires_at must be later than acquired_at")
        if self.status == LeaseStatus.active and self.released_at is not None:
            raise ValueError("active lease cannot have released_at")
        if self.status != LeaseStatus.active and self.released_at is None:
            raise ValueError("non-active lease requires released_at")
        return self


class CommandReceipt(StrictModel):
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str = Field(min_length=1, max_length=4_096)
    exit_code: int
    output_digest: Digest
    started_at: datetime
    finished_at: datetime

    @field_validator("argv")
    @classmethod
    def command_is_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part or "\x00" in part for part in value):
            raise ValueError("argv must contain non-empty NUL-free elements")
        return value

    @field_validator("cwd")
    @classmethod
    def cwd_is_relative(cls, value: str) -> str:
        return _validate_relative_path(value, label="command cwd")

    @field_validator("started_at", "finished_at")
    @classmethod
    def command_timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def command_lifetime_is_consistent(self) -> CommandReceipt:
        if self.finished_at < self.started_at:
            raise ValueError("command finished_at cannot precede started_at")
        return self


class TestReceipt(StrictModel):
    name: str = Field(min_length=1, max_length=1_000)
    command_digest: Digest
    exit_code: int
    passed: bool
    output_digest: Digest

    @model_validator(mode="after")
    def pass_matches_exit_code(self) -> TestReceipt:
        if self.passed != (self.exit_code == 0):
            raise ValueError("test passed must agree with exit_code")
        return self


class DevelopmentResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    result_id: UUID
    assignment_id: UUID
    work_item_id: UUID
    lease_id: UUID
    agent_role: AgentRole
    baseline_commit: CommitSha
    commit_sha: CommitSha | None = None
    diff_digest: Digest
    commands: tuple[CommandReceipt, ...] = Field(min_length=1)
    tests: tuple[TestReceipt, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    artifact_refs: tuple[Digest, ...] = ()
    evidence_refs: tuple[Digest, ...] = Field(min_length=1)
    reproduction_steps: tuple[str, ...] = Field(min_length=1)
    created_at: datetime

    @field_validator("artifact_refs", "evidence_refs")
    @classmethod
    def result_refs_are_unique(cls, value: tuple[Digest, ...]) -> tuple[Digest, ...]:
        _unique(value, label="result references")
        return value

    @field_validator("limitations", "reproduction_steps")
    @classmethod
    def result_text_lists_are_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("result list entries cannot be empty")
        return tuple(item.strip() for item in value)

    @field_validator("created_at")
    @classmethod
    def result_created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class AcceptanceCheck(StrictModel):
    criterion: str = Field(min_length=1, max_length=20_000)
    passed: bool
    evidence_refs: tuple[Digest, ...] = Field(min_length=1)

    @field_validator("evidence_refs")
    @classmethod
    def check_refs_are_unique(cls, value: tuple[Digest, ...]) -> tuple[Digest, ...]:
        _unique(value, label="acceptance evidence")
        return value


class LiliesReview(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    review_id: UUID
    assignment_id: UUID
    work_item_id: UUID
    result_id: UUID
    reviewer_role: Literal[AgentRole.lilies] = AgentRole.lilies
    verdict: ReviewVerdict
    acceptance_checks: tuple[AcceptanceCheck, ...] = Field(min_length=1)
    verification_commands: tuple[CommandReceipt, ...] = Field(min_length=1)
    evidence_refs: tuple[Digest, ...] = Field(min_length=1)
    next_requirements: tuple[str, ...] = ()
    created_at: datetime

    @field_validator("evidence_refs")
    @classmethod
    def review_refs_are_unique(cls, value: tuple[Digest, ...]) -> tuple[Digest, ...]:
        _unique(value, label="review evidence")
        return value

    @field_validator("next_requirements")
    @classmethod
    def next_requirements_are_nonempty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("next requirements cannot contain empty entries")
        return tuple(item.strip() for item in value)

    @field_validator("created_at")
    @classmethod
    def review_created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def review_verdict_is_supported(self) -> LiliesReview:
        all_passed = all(item.passed for item in self.acceptance_checks)
        if self.verdict == ReviewVerdict.accepted:
            if not all_passed:
                raise ValueError("accepted review requires every acceptance check to pass")
            if any(command.exit_code != 0 for command in self.verification_commands):
                raise ValueError("accepted review requires every verification command to exit zero")
            if self.next_requirements:
                raise ValueError("accepted review cannot request rework")
        else:
            if all_passed:
                raise ValueError("rework review requires at least one failed check")
            if not self.next_requirements:
                raise ValueError("rework review requires next_requirements")
        return self


class DevelopmentEvent(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: UUID
    assignment_id: UUID
    seq: int = Field(ge=1)
    event_type: Identifier
    actor_role: str = Field(min_length=1, max_length=80)
    actor_id: Identifier
    aggregate_type: str = Field(min_length=1, max_length=80)
    aggregate_id: UUID
    aggregate_revision: int = Field(ge=1)
    idempotency_key: IdempotencyKey
    payload: dict[str, Any]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def event_created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class ReaderCursor(StrictModel):
    assignment_id: UUID
    reader_role: str = Field(min_length=1, max_length=80)
    reader_id: Identifier
    ack_seq: int = Field(ge=0)
    revision: int = Field(ge=1)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def cursor_updated_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class DispatchOutboxItem(StrictModel):
    outbox_id: UUID
    assignment_id: UUID
    work_item_id: UUID
    destination_role: AgentRole
    kind: Literal["work_dispatch", "lilies_review"]
    idempotency_key: IdempotencyKey
    payload: dict[str, Any]
    status: OutboxStatus
    attempts: int = Field(ge=0)
    available_at: datetime
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None = None
    last_error: str | None = Field(default=None, max_length=20_000)

    @field_validator("available_at", "created_at", "updated_at", "delivered_at")
    @classmethod
    def outbox_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value) if value is not None else None


class DispatchOutboxClaim(StrictModel):
    """A fenced, restart-visible right to invoke one outbox handler."""

    claim_id: UUID
    outbox: DispatchOutboxItem
    claimed_by: Identifier
    claimed_at: datetime
    expires_at: datetime

    @field_validator("claimed_at", "expires_at")
    @classmethod
    def claim_timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def claim_has_positive_lifetime(self) -> DispatchOutboxClaim:
        if self.expires_at <= self.claimed_at:
            raise ValueError("outbox claim must expire after it is acquired")
        return self


class DevelopmentInvocationFence(StrictModel):
    """One durable, budget-neutral dispatcher-handler invocation fence.

    ``(outbox_id, attempt)`` is the idempotency boundary.  The fence prevents
    an uncertain external handoff from being entered twice, but it is not a
    tool call, command, or provider charge.  Those real resources are metered
    separately at their trusted execution boundaries.
    """

    schema_version: Literal["1.0"] = "1.0"
    fence_id: UUID
    assignment_id: UUID
    outbox_id: UUID
    attempt: int = Field(ge=1)
    claim_id: UUID
    destination_role: AgentRole
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def fence_created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class DevelopmentInvocationFenceResult(StrictModel):
    """Result of an idempotent invocation-fence acquisition."""

    fence: DevelopmentInvocationFence
    acquired: bool


# Concise aliases for callers that use the names in the Stage Contract.
WorkItem: TypeAlias = DevelopmentWorkItem
Lease: TypeAlias = DevelopmentLease
Result: TypeAlias = DevelopmentResult
Review: TypeAlias = LiliesReview


__all__ = [
    "AcceptanceCheck",
    "AgentRole",
    "AgentRoleGrant",
    "ApprovalMode",
    "AssignmentStatus",
    "CommandReceipt",
    "DevelopmentAssignment",
    "DevelopmentAssignmentProjection",
    "DevelopmentBudget",
    "DevelopmentEvent",
    "DevelopmentInvocationFence",
    "DevelopmentInvocationFenceResult",
    "DevelopmentLease",
    "DevelopmentResult",
    "DevelopmentTaskRole",
    "DevelopmentWorkItem",
    "Digest",
    "DispatchOutboxItem",
    "DispatchOutboxClaim",
    "ExecutionMode",
    "IdempotencyKey",
    "Lease",
    "LeaseStatus",
    "LiliesReview",
    "OutboxStatus",
    "ReaderCursor",
    "Result",
    "Review",
    "ReviewVerdict",
    "SideEffect",
    "TestReceipt",
    "WorkItem",
    "WorkItemKind",
    "WorkItemStatus",
    "WorkspaceGrant",
    "utc_now",
]
