from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)


T = TypeVar("T")

Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
OpaqueReference = Annotated[
    str,
    StringConstraints(
        min_length=8,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a UTC timezone")
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must use UTC (offset +00:00)")
    return value.astimezone(timezone.utc)


def _unique(value: list[T], *, label: str) -> list[T]:
    if len(value) != len(set(value)):
        raise ValueError(f"{label} must not contain duplicates")
    return value


class StrictModel(BaseModel):
    """Base for every Lilies wire contract.

    Assignment and local-daemon payloads are security boundaries.  Keeping the
    policy on one base class prevents a newly added nested model from silently
    accepting misspelled or smuggled fields.
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class SessionStatus(str, Enum):
    ready = "ready"
    running = "running"
    waiting_permission = "waiting_permission"
    waiting_collaboration = "waiting_collaboration"
    interrupted = "interrupted"
    error = "error"
    cancelled = "cancelled"
    completed = "completed"
    closed = "closed"


class SessionKind(str, Enum):
    interactive = "interactive"
    platform = "platform"


class AssignmentMode(str, Enum):
    customer = "customer"
    formal_experiment = "formal_experiment"


class LocalScope(str, Enum):
    session_read = "lilies.session:read"
    session_write = "lilies.session:write"
    permission_resolve = "lilies.permission:resolve"
    daemon_control = "lilies.daemon:control"
    credential_write = "lilies.credential:write"


DEFAULT_LOCAL_SCOPES: tuple[LocalScope, ...] = tuple(LocalScope)


class PlatformScope(str, Enum):
    catalog_read = "workflow.catalog:read"
    application_write = "workflow.application:write"
    draft_write = "workflow.draft:write"
    test_execute = "workflow.test:execute"
    run_execute = "workflow.run:execute"
    trace_read = "workflow.trace:read"
    artifact_read = "workflow.artifact:read"
    application_publish = "workflow.application:publish"


class CollaborationScope(str, Enum):
    report_write = "collaboration.report:write"
    response_read = "collaboration.response:read"


class SessionCreateRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    idempotency_key: IdempotencyKey
    kind: SessionKind = SessionKind.interactive
    title: str | None = Field(default=None, min_length=1, max_length=200)


class SessionMessageRequest(StrictModel):
    idempotency_key: IdempotencyKey
    message_id: UUID
    content: str = Field(min_length=1, max_length=100_000)


class SessionResumeRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_status: Literal[
        SessionStatus.interrupted,
        SessionStatus.error,
        SessionStatus.waiting_permission,
        SessionStatus.waiting_collaboration,
    ]
    reason: str | None = Field(default=None, max_length=1_000)


class SessionCancelRequest(StrictModel):
    idempotency_key: IdempotencyKey
    reason: str = Field(default="requested_by_user", min_length=1, max_length=1_000)


class SessionAckRequest(StrictModel):
    idempotency_key: IdempotencyKey
    cursor: int = Field(ge=0)


class SessionAckResult(StrictModel):
    client_id: UUID
    session_id: UUID
    cursor: int = Field(ge=0)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class SessionUsage(StrictModel):
    token_count: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    tool_count: int = Field(default=0, ge=0)
    model_call_count: int = Field(default=0, ge=0)


class SessionResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: UUID
    status: SessionStatus
    kind: SessionKind
    title: str | None = None
    assignment_id: UUID | None = None
    context_summary: str | None = Field(default=None, max_length=100_000)
    summary_through_event_seq: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime
    usage: SessionUsage = Field(default_factory=SessionUsage)

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class SessionListResult(StrictModel):
    sessions: list[SessionResult] = Field(default_factory=list)


class SessionOperationResult(StrictModel):
    session_id: UUID
    status: SessionStatus
    event_cursor: int = Field(ge=0)
    accepted_at: datetime = Field(default_factory=utc_now)

    @field_validator("accepted_at")
    @classmethod
    def accepted_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class PairingCodeCreateRequest(StrictModel):
    allowed_scopes: list[LocalScope] = Field(
        default_factory=lambda: list(DEFAULT_LOCAL_SCOPES),
        min_length=1,
    )
    ttl_seconds: Literal[600] = 600

    @field_validator("allowed_scopes")
    @classmethod
    def scopes_are_unique(cls, value: list[LocalScope]) -> list[LocalScope]:
        return _unique(value, label="allowed_scopes")


class PairingCodeResult(StrictModel):
    pairing_code: str = Field(
        min_length=8,
        max_length=80,
        pattern=r"^[A-Z2-9][A-Z2-9-]*$",
    )
    allowed_scopes: list[LocalScope] = Field(min_length=1)
    expires_at: datetime
    daemon_fingerprint: Digest

    @field_validator("allowed_scopes")
    @classmethod
    def scopes_are_unique(cls, value: list[LocalScope]) -> list[LocalScope]:
        return _unique(value, label="allowed_scopes")

    @field_validator("expires_at")
    @classmethod
    def expires_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class PairingExchangeRequest(StrictModel):
    pairing_code: str = Field(min_length=8, max_length=80)
    client_name: str = Field(min_length=1, max_length=120)
    requested_scopes: list[LocalScope] = Field(min_length=1)
    client_nonce: str = Field(
        min_length=22,
        max_length=256,
        pattern=r"^[A-Za-z0-9_-]+={0,2}$",
    )
    previous_client_id: UUID | None = None
    previous_access_token: SecretStr | None = Field(
        default=None,
        min_length=32,
        max_length=512,
    )
    requested_client_id: UUID | None = None
    prepared_access_token: SecretStr | None = Field(
        default=None,
        min_length=32,
        max_length=512,
    )

    @field_validator("requested_scopes")
    @classmethod
    def scopes_are_unique(cls, value: list[LocalScope]) -> list[LocalScope]:
        return _unique(value, label="requested_scopes")

    @model_validator(mode="after")
    def previous_client_proof_is_complete(self) -> PairingExchangeRequest:
        if (self.previous_client_id is None) != (self.previous_access_token is None):
            raise ValueError(
                "previous_client_id and previous_access_token must be provided together"
            )
        if (self.requested_client_id is None) != (self.prepared_access_token is None):
            raise ValueError(
                "requested_client_id and prepared_access_token must be provided together"
            )
        if self.requested_client_id is None or self.prepared_access_token is None:
            return self
        token = self.prepared_access_token.get_secret_value()
        token_client_id, separator, token_secret = token.partition(".")
        try:
            parsed_token_client_id = UUID(token_client_id)
        except ValueError as error:
            raise ValueError(
                "prepared_access_token must be bound to requested_client_id"
            ) from error
        if (
            not separator
            or not token_secret
            or token_client_id != str(parsed_token_client_id)
            or parsed_token_client_id != self.requested_client_id
        ):
            raise ValueError(
                "prepared_access_token must be bound to requested_client_id"
            )
        if (
            self.previous_client_id is not None
            and self.requested_client_id != self.previous_client_id
        ):
            raise ValueError(
                "requested_client_id must equal previous_client_id during rotation"
            )
        return self


class PairingExchangeResult(StrictModel):
    client_id: UUID
    access_token: str = Field(min_length=32, max_length=512)
    granted_scopes: list[LocalScope] = Field(min_length=1)
    expires_at: datetime | None
    daemon_fingerprint: Digest

    @field_validator("granted_scopes")
    @classmethod
    def scopes_are_unique(cls, value: list[LocalScope]) -> list[LocalScope]:
        return _unique(value, label="granted_scopes")

    @field_validator("expires_at")
    @classmethod
    def expires_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class PermissionStatus(str, Enum):
    pending = "pending"
    allowed = "allowed"
    denied = "denied"
    cancelled = "cancelled"


class PermissionRequest(StrictModel):
    request_id: UUID
    session_id: UUID
    turn_id: UUID
    tool_call_id: str = Field(min_length=1, max_length=160)
    tool_name: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")
    input_digest: Digest
    redacted_input: dict[str, Any] = Field(default_factory=dict)
    status: Literal[PermissionStatus.pending] = PermissionStatus.pending
    requested_at: datetime
    expires_at: datetime | None = None

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("expires_at")
    @classmethod
    def expires_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def expiry_follows_request(self) -> PermissionRequest:
        if self.expires_at is not None and self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be later than requested_at")
        return self


class PermissionDecisionRequest(StrictModel):
    idempotency_key: IdempotencyKey
    behavior: Literal["allow", "deny"]
    expected_input_digest: Digest
    updated_input: dict[str, Any] | None = None
    message: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def denial_cannot_replace_input(self) -> PermissionDecisionRequest:
        if self.behavior == "deny" and self.updated_input is not None:
            raise ValueError("updated_input is only valid for an allow decision")
        return self


class PermissionDecisionResult(StrictModel):
    request_id: UUID
    status: Literal[PermissionStatus.allowed, PermissionStatus.denied]
    input_digest: Digest
    decided_at: datetime = Field(default_factory=utc_now)

    @field_validator("decided_at")
    @classmethod
    def decided_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class CredentialKind(str, Enum):
    platform_assignment = "platform_assignment"
    collaboration_channel = "collaboration_channel"


class CredentialProvisionRequest(StrictModel):
    idempotency_key: IdempotencyKey
    credential_ref: OpaqueReference
    assignment_id: UUID
    kind: CredentialKind
    secret: SecretStr = Field(min_length=16, max_length=16_384)
    scopes: list[PlatformScope | CollaborationScope] = Field(min_length=1)
    expires_at: datetime

    @field_validator("scopes")
    @classmethod
    def scopes_are_unique(
        cls,
        value: list[PlatformScope | CollaborationScope],
    ) -> list[PlatformScope | CollaborationScope]:
        return _unique(value, label="scopes")

    @field_validator("expires_at")
    @classmethod
    def expires_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def scope_family_matches_kind(self) -> CredentialProvisionRequest:
        if self.kind == CredentialKind.platform_assignment and any(
            isinstance(scope, CollaborationScope) for scope in self.scopes
        ):
            raise ValueError("platform assignment credentials cannot grant collaboration scopes")
        if self.kind == CredentialKind.collaboration_channel and any(
            isinstance(scope, PlatformScope) for scope in self.scopes
        ):
            raise ValueError("collaboration credentials cannot grant workflow scopes")
        return self


class CredentialProvisionResult(StrictModel):
    credential_ref: OpaqueReference
    assignment_id: UUID
    kind: CredentialKind
    scopes: list[PlatformScope | CollaborationScope]
    expires_at: datetime
    provisioned_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None

    @field_validator("expires_at", "provisioned_at", "revoked_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class CredentialRevokeRequest(StrictModel):
    idempotency_key: IdempotencyKey
    credential_ref: OpaqueReference
    reason: str = Field(min_length=1, max_length=1_000)


class CredentialRevokeResult(StrictModel):
    credential_ref: OpaqueReference
    revoked: bool
    revoked_at: datetime = Field(default_factory=utc_now)

    @field_validator("revoked_at")
    @classmethod
    def revoked_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class DaemonHealth(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    service: Literal["lilies"] = "lilies"
    status: Literal["ok"] = "ok"
    daemon_version: str = Field(min_length=1, max_length=80)


class DaemonStatus(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    pid: int = Field(gt=0)
    address: AnyHttpUrl
    started_at: datetime
    daemon_fingerprint: Digest
    client_id: UUID
    client_scopes: list[LocalScope] = Field(min_length=1)
    client_expires_at: datetime | None = None
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=200)
    paired_client_count: int = Field(ge=0)
    platform_paired: bool = False
    active_session_count: int = Field(ge=0)
    active_assignment_count: int = Field(ge=0)
    stopping: bool = False

    @field_validator("started_at")
    @classmethod
    def started_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("client_scopes")
    @classmethod
    def client_scopes_are_unique(cls, value: list[LocalScope]) -> list[LocalScope]:
        return _unique(value, label="client_scopes")

    @field_validator("client_expires_at")
    @classmethod
    def client_expires_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return _require_utc(value) if value is not None else None


class DaemonStopRequest(StrictModel):
    idempotency_key: IdempotencyKey
    reason: str = Field(default="requested_by_user", min_length=1, max_length=1_000)
    cancel_active_turns: Literal[True] = True
    grace_period_seconds: int = Field(default=10, ge=0, le=60)


class DaemonStopResult(StrictModel):
    accepted: Literal[True] = True
    active_turns_cancel_requested: int = Field(ge=0)
    requested_at: datetime = Field(default_factory=utc_now)

    @field_validator("requested_at")
    @classmethod
    def requested_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class BusinessContext(StrictModel):
    customer_roles: list[str] = Field(min_length=1, max_length=40)
    business_goal: str = Field(min_length=1, max_length=10_000)
    inputs: list[str] = Field(min_length=1, max_length=100)
    outputs: list[str] = Field(min_length=1, max_length=100)
    constraints: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("customer_roles", "inputs", "outputs", "constraints")
    @classmethod
    def entries_are_unique(cls, value: list[str]) -> list[str]:
        return _unique(value, label="business context entries")


class TaskPackageRef(StrictModel):
    task_id: str = Field(
        min_length=3,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    revision: int = Field(ge=1)
    public_summary_digest: Digest


class ApplicationTargetMode(str, Enum):
    create_new = "create_new"
    existing = "existing"


class ApplicationTarget(StrictModel):
    mode: ApplicationTargetMode
    application_id: UUID | None = None

    @model_validator(mode="after")
    def validate_application_id(self) -> ApplicationTarget:
        if self.mode == ApplicationTargetMode.create_new and self.application_id is not None:
            raise ValueError("create_new target must omit application_id")
        if self.mode == ApplicationTargetMode.existing and self.application_id is None:
            raise ValueError("existing target requires application_id")
        return self


class PlatformAccess(StrictModel):
    base_url: AnyHttpUrl
    contract_url: Literal["/api/v1/lilies/platform-contract"]
    contract_digest: Digest
    credential_ref: OpaqueReference
    scopes: list[PlatformScope] = Field(min_length=1)
    application_ids: list[UUID] = Field(default_factory=list)

    @field_validator("scopes")
    @classmethod
    def scopes_are_unique(cls, value: list[PlatformScope]) -> list[PlatformScope]:
        return _unique(value, label="scopes")

    @field_validator("application_ids")
    @classmethod
    def applications_are_unique(cls, value: list[UUID]) -> list[UUID]:
        return _unique(value, label="application_ids")

    @field_validator("base_url")
    @classmethod
    def base_url_has_no_embedded_authority(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        return value


class AssignmentNetworkPolicy(str, Enum):
    none = "none"
    allowlist = "allowlist"
    full = "full"


class AllowedAction(str, Enum):
    platform_contract_get = "platform_contract_get"
    platform_block_search = "platform_block_search"
    platform_block_get = "platform_block_get"
    platform_tool_catalog = "platform_tool_catalog"
    platform_application_create = "platform_application_create"
    platform_application_get = "platform_application_get"
    platform_draft_inspect = "platform_draft_inspect"
    platform_draft_apply = "platform_draft_apply"
    platform_tests_run = "platform_tests_run"
    platform_run_start = "platform_run_start"
    platform_run_get = "platform_run_get"
    platform_run_resume = "platform_run_resume"
    platform_run_cancel = "platform_run_cancel"
    platform_trace_get = "platform_trace_get"
    platform_artifact_read = "platform_artifact_read"
    platform_publish = "platform_publish"


class ProhibitedAction(str, Enum):
    read_platform_source = "read_platform_source"
    read_hidden_oracle = "read_hidden_oracle"
    write_task_package = "write_task_package"


REQUIRED_PROHIBITED_ACTIONS = frozenset(ProhibitedAction)


class AssignmentConstraints(StrictModel):
    deadline_at: datetime
    max_turns: int = Field(ge=5, le=200)
    max_budget_usd: float | None = Field(default=None, gt=0)
    max_tool_calls: int = Field(ge=1, le=1_000)
    network_policy: AssignmentNetworkPolicy
    allowed_hosts: list[str] = Field(default_factory=list, max_length=100)
    allowed_actions: list[AllowedAction] = Field(min_length=1)
    prohibited_actions: list[ProhibitedAction] = Field(min_length=3)
    no_substitute_validation: bool = False

    @field_validator("deadline_at")
    @classmethod
    def deadline_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("allowed_hosts")
    @classmethod
    def hosts_are_safe_and_unique(cls, value: list[str]) -> list[str]:
        for host in value:
            if not host or any(marker in host for marker in ("://", "/", "@", " ")):
                raise ValueError("allowed_hosts entries must be host names, not URLs or paths")
        return _unique(value, label="allowed_hosts")

    @field_validator("allowed_actions")
    @classmethod
    def actions_are_unique(cls, value: list[AllowedAction]) -> list[AllowedAction]:
        return _unique(value, label="allowed_actions")

    @field_validator("prohibited_actions")
    @classmethod
    def prohibited_actions_are_complete(
        cls,
        value: list[ProhibitedAction],
    ) -> list[ProhibitedAction]:
        _unique(value, label="prohibited_actions")
        missing = REQUIRED_PROHIBITED_ACTIONS - set(value)
        if missing:
            missing_values = sorted(action.value for action in missing)
            raise ValueError(f"prohibited_actions missing mandatory entries: {missing_values}")
        return value

    @model_validator(mode="after")
    def network_policy_matches_hosts(self) -> AssignmentConstraints:
        if self.network_policy == AssignmentNetworkPolicy.allowlist and not self.allowed_hosts:
            raise ValueError("allowlist network policy requires allowed_hosts")
        if self.network_policy != AssignmentNetworkPolicy.allowlist and self.allowed_hosts:
            raise ValueError("allowed_hosts is only valid with allowlist network policy")
        return self


class ArtifactRef(StrictModel):
    artifact_id: OpaqueReference
    digest: Digest
    media_type: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=240)


class DeliverableSpec(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=2_000)
    media_type: str = Field(min_length=1, max_length=200)
    required: bool = True


class CollaborationAccess(StrictModel):
    channel_id: UUID
    credential_ref: OpaqueReference
    scopes: list[CollaborationScope] = Field(min_length=2, max_length=2)
    expires_at: datetime

    @field_validator("scopes")
    @classmethod
    def scopes_are_complete_and_unique(
        cls,
        value: list[CollaborationScope],
    ) -> list[CollaborationScope]:
        _unique(value, label="collaboration scopes")
        if set(value) != set(CollaborationScope):
            raise ValueError("collaboration access requires report:write and response:read")
        return value

    @field_validator("expires_at")
    @classmethod
    def expires_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


_FORBIDDEN_ASSIGNMENT_KEYS = {
    "access_token",
    "api_key",
    "database_path",
    "expected_answer",
    "oracle",
    "oracle_path",
    "password",
    "platform_source_path",
    "private_key",
    "repo_path",
    "repository_path",
    "secret",
    "secrets",
    "source_path",
    "token",
}

_FORBIDDEN_ASSIGNMENT_VALUE_PATTERNS = (
    (
        "platform task bearer",
        re.compile(r"lpt_[0-9a-f]{32}_[A-Za-z0-9_-]{43,}"),
    ),
    (
        "daemon bearer",
        re.compile(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{12}\.[A-Za-z0-9_-]{32,}",
            re.IGNORECASE,
        ),
    ),
    ("authorization header", re.compile(r"\b(?:authorization|proxy-authorization)\s*:", re.IGNORECASE)),
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("provider API key", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,})\b")),
    ("JWT bearer", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    (
        "pairing code",
        re.compile(
            r"\b(?:[A-HJ-NP-Z2-9]{12}-[A-HJ-NP-Z2-9]{24}|"
            r"[0-9a-f]{16}-[A-Za-z0-9_-]{24,})\b",
            re.IGNORECASE,
        ),
    ),
    ("protected oracle reference", re.compile(r"\b(?:oracle|protected)://\S+", re.IGNORECASE)),
)


def _find_forbidden_assignment_key(value: Any, path: str = "assignment") -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_ASSIGNMENT_KEYS:
                return f"{path}.{key}"
            found = _find_forbidden_assignment_key(child, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_forbidden_assignment_key(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _find_forbidden_assignment_value(
    value: Any, path: str = "assignment"
) -> tuple[str, str] | None:
    if isinstance(value, dict):
        for key, child in value.items():
            found = _find_forbidden_assignment_value(child, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _find_forbidden_assignment_value(child, f"{path}[{index}]")
            if found is not None:
                return found
    elif isinstance(value, str):
        for label, pattern in _FORBIDDEN_ASSIGNMENT_VALUE_PATTERNS:
            if pattern.search(value):
                return path, label
    return None


def validate_assignment_payload_safety(value: Any) -> Any:
    """Reject sensitive keys and recognizable plaintext secret/oracle values."""

    found = _find_forbidden_assignment_key(value)
    if found is not None:
        raise ValueError(f"BuildAssignment contains forbidden sensitive field: {found}")
    forbidden_value = _find_forbidden_assignment_value(value)
    if forbidden_value is not None:
        path, label = forbidden_value
        raise ValueError(
            f"BuildAssignment contains forbidden plaintext {label} at {path}"
        )
    return value


class BuildAssignment(StrictModel):
    schema_version: Literal["1.0"]
    assignment_id: UUID
    idempotency_key: IdempotencyKey
    mode: AssignmentMode
    requirement: str = Field(min_length=10, max_length=100_000)
    business_context: BusinessContext
    task_package: TaskPackageRef | None = None
    target: ApplicationTarget
    platform: PlatformAccess
    constraints: AssignmentConstraints
    fixture_refs: list[ArtifactRef] | None = Field(default=None, max_length=500)
    deliverables: list[DeliverableSpec] = Field(min_length=1, max_length=100)
    collaboration: CollaborationAccess | None = None
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_fields(cls, value: Any) -> Any:
        return validate_assignment_payload_safety(value)

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("fixture_refs")
    @classmethod
    def fixtures_are_unique(cls, value: list[ArtifactRef] | None) -> list[ArtifactRef] | None:
        if value is None:
            return None
        ids = [fixture.artifact_id for fixture in value]
        _unique(ids, label="fixture artifact_ids")
        return value

    @field_validator("deliverables")
    @classmethod
    def deliverables_are_unique(cls, value: list[DeliverableSpec]) -> list[DeliverableSpec]:
        names = [deliverable.name for deliverable in value]
        _unique(names, label="deliverable names")
        return value

    @model_validator(mode="after")
    def validate_mode_boundary(self) -> BuildAssignment:
        if self.constraints.deadline_at <= self.created_at:
            raise ValueError("deadline_at must be later than created_at")

        if self.target.application_id is not None and (
            self.target.application_id not in self.platform.application_ids
        ):
            raise ValueError("existing target application_id must be in platform.application_ids")

        collaboration_was_supplied = "collaboration" in self.model_fields_set
        if self.mode == AssignmentMode.customer:
            if collaboration_was_supplied:
                raise ValueError("customer assignment must completely omit collaboration")
            if self.task_package is not None:
                raise ValueError("customer assignment cannot carry a formal task package")
            return self

        if self.task_package is None:
            raise ValueError("formal_experiment requires task_package")
        if not self.fixture_refs:
            raise ValueError("formal_experiment requires non-empty fixture_refs")
        if self.constraints.max_budget_usd is None:
            raise ValueError("formal_experiment requires max_budget_usd")
        if not self.constraints.no_substitute_validation:
            raise ValueError("formal_experiment requires no_substitute_validation=true")
        if self.constraints.network_policy == AssignmentNetworkPolicy.full:
            raise ValueError("formal_experiment cannot use full network access")
        return self


class AssignmentSubmissionResult(StrictModel):
    """Durable receipt for the one assignment accepted by a local session."""

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    session_id: UUID
    turn_id: UUID
    start_message_id: UUID
    status: SessionStatus
    event_cursor: int = Field(ge=1)
    accepted_at: datetime
    replayed: bool = False

    @field_validator("accepted_at")
    @classmethod
    def accepted_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)
