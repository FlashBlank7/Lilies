from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from .lilies_models import Digest, IdempotencyKey, OpaqueReference


T = TypeVar("T")

TaskId = Annotated[
    str,
    StringConstraints(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
ActorId = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
CommitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]


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
    """Strict collaboration wire/storage contract."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ApprovalMode(str, Enum):
    manual = "manual"
    auto_forward = "auto_forward"


class ChannelStatus(str, Enum):
    created = "created"
    active = "active"
    disconnected = "disconnected"
    closing = "closing"
    closed = "closed"
    archived = "archived"


class MessageType(str, Enum):
    report = "report"
    approval = "approval"
    task_amendment = "task_amendment"
    environment_response = "environment_response"
    developer_response = "developer_response"
    verification_claim = "verification_claim"
    verification_result = "verification_result"
    control = "control"


class SenderRole(str, Enum):
    lilies = "lilies"
    user = "user"
    task_author = "task_author"
    codex = "codex"
    verifier = "verifier"
    platform = "platform"


class MessageVisibility(str, Enum):
    user_only = "user_only"
    user_and_lilies = "user_and_lilies"
    approved_developer = "approved_developer"
    verifier = "verifier"


class ReportCategory(str, Enum):
    """The only four categories that may become collaboration reports.

    ``workflow_design_error`` remains in the normal Lilies repair loop and
    ``permission_request`` remains in the separate permission channel.  Their
    deliberate absence here makes accidental routing fail schema validation.
    Verification claims also use their own schema and endpoint.
    """

    task_spec_gap = "task_spec_gap"
    environment_gap = "environment_gap"
    platform_capability_gap = "platform_capability_gap"
    platform_defect_suspected = "platform_defect_suspected"


class ReportPhase(str, Enum):
    preflight = "preflight"
    planning = "planning"
    draft_mutation = "draft_mutation"
    run = "run"
    acceptance = "acceptance"
    resume = "resume"


class ReportSeverity(str, Enum):
    blocking = "blocking"
    major = "major"
    minor = "minor"


class ReportRoute(str, Enum):
    task_author = "task_author"
    environment = "environment"
    capability_approval = "capability_approval"
    developer = "developer"
    verifier = "verifier"


class ReportStatus(str, Enum):
    # Platform capability/defect state machine.
    observed = "observed"
    evidence_collecting = "evidence_collecting"
    needs_more_evidence = "needs_more_evidence"
    awaiting_user_review = "awaiting_user_review"
    rejected = "rejected"
    approved_for_codex = "approved_for_codex"
    implementing = "implementing"
    ready_for_lilies_verification = "ready_for_lilies_verification"
    lilies_verified = "lilies_verified"
    verification_failed = "verification_failed"
    independently_verified = "independently_verified"
    withdrawn = "withdrawn"
    # Task-package and environment state machines.
    reported = "reported"
    routed_to_task_author = "routed_to_task_author"
    task_package_amended = "task_package_amended"
    rejected_with_evidence = "rejected_with_evidence"
    lilies_rechecks = "lilies_rechecks"
    environment_failed = "environment_failed"
    environment_restored = "environment_restored"
    unresolved = "unresolved"
    lilies_health_checks = "lilies_health_checks"


class ReportDecision(str, Enum):
    approve = "approve"
    reject = "reject"
    needs_more_evidence = "needs_more_evidence"


class DeveloperOutcome(str, Enum):
    implemented = "implemented"
    not_reproduced = "not_reproduced"
    rejected_as_specific = "rejected_as_specific"
    needs_task_change = "needs_task_change"


class TaskAmendmentOutcome(str, Enum):
    amended = "amended"
    rejected_with_evidence = "rejected_with_evidence"


class EnvironmentOutcome(str, Enum):
    restored = "restored"
    unresolved = "unresolved"


class ReprobeOutcome(str, Enum):
    lilies_verified = "lilies_verified"
    verification_failed = "verification_failed"


class ClaimStatus(str, Enum):
    frozen = "frozen"
    invalidated = "invalidated"
    independently_verified = "independently_verified"
    verification_failed = "verification_failed"


class VerificationVerdict(str, Enum):
    independently_verified = "independently_verified"
    verification_failed = "verification_failed"


class LeaseStatus(str, Enum):
    active = "active"
    released = "released"
    expired = "expired"


class EvidenceKind(str, Enum):
    artifact = "artifact"
    archive = "archive"
    trace = "trace"
    run = "run"
    test_run = "test_run"
    contract = "contract"
    manual = "manual"
    task_package = "task_package"
    host_receipt = "host_receipt"
    health_check = "health_check"
    source_commit = "source_commit"
    browser = "browser"
    other = "other"


class ControlKind(str, Enum):
    channel_activated = "channel_activated"
    channel_disconnected = "channel_disconnected"
    channel_reconnected = "channel_reconnected"
    channel_closing = "channel_closing"
    channel_closed = "channel_closed"
    channel_archived = "channel_archived"
    approval_mode_changed = "approval_mode_changed"
    report_status_changed = "report_status_changed"
    claim_invalidated = "claim_invalidated"
    subscriber_overflow = "subscriber_overflow"


class PayloadSchema(str, Enum):
    report_v1 = "collaboration.report.v1"
    approval_v1 = "collaboration.approval.v1"
    task_amendment_v1 = "collaboration.task_amendment.v1"
    environment_response_v1 = "collaboration.environment_response.v1"
    developer_response_v1 = "collaboration.developer_response.v1"
    verification_claim_v1 = "collaboration.verification_claim.v1"
    verification_result_v1 = "collaboration.verification_result.v1"
    lilies_reprobe_result_v1 = "collaboration.lilies_reprobe_result.v1"
    control_v1 = "collaboration.control.v1"


_FORBIDDEN_PAYLOAD_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "expected_answer",
    "hidden_oracle",
    "oracle",
    "oracle_path",
    "password",
    "private_key",
    "proxy_authorization",
    "refresh_token",
    "secret",
    "secrets",
    "set_cookie",
    "token",
}
_PROTECTED_ORACLE_PAYLOAD_KEYS = frozenset(
    {"expected_answer", "hidden_oracle", "oracle", "oracle_path"}
)
_REDACTED_PAYLOAD = "[REDACTED]"

_FORBIDDEN_PAYLOAD_VALUE_PATTERNS = (
    (
        "authorization header",
        re.compile(
            r"\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+\S+",
            re.IGNORECASE,
        ),
    ),
    (
        "cookie header",
        re.compile(
            r"\b(?:cookie|set-cookie)\s*:\s*(?!\*+|\[redacted\]|<redacted>)\S+",
            re.IGNORECASE,
        ),
    ),
    ("private key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    (
        "provider credential",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,})\b"
        ),
    ),
    (
        "JWT bearer",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "platform task bearer",
        re.compile(r"\blpt_[0-9a-f]{32}_[A-Za-z0-9_-]{43,}\b"),
    ),
    (
        "daemon bearer",
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{12}\.[A-Za-z0-9_-]{32,}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pairing code",
        re.compile(r"\b[A-HJ-NP-Z2-9]{12}-[A-HJ-NP-Z2-9]{24}\b"),
    ),
    (
        "protected oracle reference",
        re.compile(
            r"(?:\b(?:oracle|protected)://\S+|(?:^|[/\\])(?:hidden[-_]?oracle)(?:[/\\]|$))",
            re.IGNORECASE,
        ),
    ),
    (
        "credential query parameter",
        re.compile(
            r"(?:[?&]|\b)(?:api[_-]?key|access[_-]?token|password|secret|token)="
            r"(?!\*+|%2A+|\[redacted\]|<redacted>)[^\s&]+",
            re.IGNORECASE,
        ),
    ),
)


def _normalized_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def sanitize_collaboration_payload(value: Any) -> Any:
    """Redact credentials while preserving protected-oracle rejection."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            normalized = _normalized_key(key)
            if (
                normalized in _FORBIDDEN_PAYLOAD_KEYS
                and normalized not in _PROTECTED_ORACLE_PAYLOAD_KEYS
            ):
                sanitized[str(key)] = _REDACTED_PAYLOAD
            else:
                sanitized[str(key)] = sanitize_collaboration_payload(child)
        return sanitized
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_collaboration_payload(item) for item in value]
    if isinstance(value, str):
        for label, pattern in _FORBIDDEN_PAYLOAD_VALUE_PATTERNS:
            if label != "protected oracle reference" and pattern.search(value):
                return _REDACTED_PAYLOAD
    return value


def _find_forbidden_payload_key(value: Any, path: str = "payload") -> str | None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = _normalized_key(key)
            if normalized in _FORBIDDEN_PAYLOAD_KEYS:
                if (
                    normalized in _PROTECTED_ORACLE_PAYLOAD_KEYS
                    or child != _REDACTED_PAYLOAD
                ):
                    return f"{path}.{key}"
                continue
            found = _find_forbidden_payload_key(child, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _find_forbidden_payload_key(child, f"{path}[{index}]")
            if found is not None:
                return found
    return None


def _find_forbidden_payload_value(
    value: Any,
    path: str = "payload",
) -> tuple[str, str] | None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        for key, child in value.items():
            found = _find_forbidden_payload_value(child, f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _find_forbidden_payload_value(child, f"{path}[{index}]")
            if found is not None:
                return found
    elif isinstance(value, str):
        for label, pattern in _FORBIDDEN_PAYLOAD_VALUE_PATTERNS:
            if pattern.search(value):
                return path, label
    return None


def validate_collaboration_payload_safety(value: Any) -> Any:
    """Reject plaintext credentials and protected-oracle material.

    The service redacts before it builds one of these models.  Validation is a
    fail-closed second boundary: an unrecognised producer cannot persist a raw
    secret merely by choosing a generic text field.
    """

    found_key = _find_forbidden_payload_key(value)
    if found_key is not None:
        raise ValueError(f"collaboration payload contains forbidden sensitive field: {found_key}")
    found_value = _find_forbidden_payload_value(value)
    if found_value is not None:
        path, label = found_value
        raise ValueError(f"collaboration payload contains forbidden plaintext {label} at {path}")
    return value


class SafePayloadModel(StrictModel):
    model_config = ConfigDict(revalidate_instances="always")

    @model_validator(mode="before")
    @classmethod
    def sanitize_and_reject_sensitive_payload(cls, value: Any) -> Any:
        sanitized = sanitize_collaboration_payload(value)
        return validate_collaboration_payload_safety(sanitized)


class EvidenceRef(StrictModel):
    """A content-addressed, immutable evidence reference."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        frozen=True,
    )

    evidence_id: OpaqueReference
    kind: EvidenceKind
    digest: Digest
    media_type: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=240)
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class ManualReference(StrictModel):
    manual_id: OpaqueReference
    version: str = Field(min_length=1, max_length=80)
    digest: Digest


class AttemptedRoute(StrictModel):
    attempt_id: UUID
    route: str = Field(min_length=3, max_length=500)
    input_digest: Digest
    outcome: str = Field(min_length=1, max_length=5_000)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=100)
    attempted_at: datetime

    @field_validator("attempted_at")
    @classmethod
    def attempted_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_is_unique(cls, value: list[EvidenceRef]) -> list[EvidenceRef]:
        _unique([item.evidence_id for item in value], label="attempt evidence_ids")
        return value


class TestRunEvidence(StrictModel):
    test_id: OpaqueReference
    command: str = Field(min_length=1, max_length=4_000)
    exit_code: int = Field(ge=0, le=255)
    summary: str = Field(min_length=1, max_length=10_000)
    evidence_ref: EvidenceRef


class ReprobeStep(StrictModel):
    order: int = Field(ge=1, le=1_000)
    action: str = Field(min_length=1, max_length=4_000)
    expected: str = Field(min_length=1, max_length=10_000)


class CollaborationChannel(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    channel_id: UUID
    task_id: TaskId
    task_revision: int = Field(ge=1)
    assignment_id: UUID
    lilies_session_id: UUID
    # Empty is only a read-only compatibility projection for pre-binding
    # channels. New activation paths require at least one application.
    application_ids: list[UUID] = Field(default_factory=list, max_length=500)
    approval_mode: ApprovalMode = ApprovalMode.manual
    # Formal channels freeze the assignment's per-report evidence-revision
    # budget. ``None`` is reserved for read-only projection of pre-budget
    # legacy channels; new formal activation always supplies a concrete bound.
    max_report_evidence_rounds: int | None = Field(default=None, ge=1, le=100)
    status: ChannelStatus
    revision: int = Field(default=1, ge=1)
    next_seq: int = Field(default=1, ge=1)
    created_at: datetime
    closed_at: datetime | None = None
    retention_until: datetime | None = None

    @field_validator("created_at", "closed_at", "retention_until")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value)

    @field_validator("application_ids")
    @classmethod
    def applications_are_unique(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("channel application_ids must be unique")
        return value

    @model_validator(mode="after")
    def lifecycle_is_consistent(self) -> CollaborationChannel:
        terminal = self.status in {ChannelStatus.closed, ChannelStatus.archived}
        if terminal and self.closed_at is None:
            raise ValueError("closed and archived channels require closed_at")
        if not terminal and self.closed_at is not None:
            raise ValueError("non-terminal channels must omit closed_at")
        if self.closed_at is not None and self.closed_at < self.created_at:
            raise ValueError("closed_at cannot be earlier than created_at")
        boundary = self.closed_at or self.created_at
        if self.retention_until is not None and self.retention_until < boundary:
            raise ValueError("retention_until cannot precede channel lifetime")
        return self


class CollaborationReportPayload(SafePayloadModel):
    schema_version: Literal["1.0"] = "1.0"
    report_id: UUID
    category: ReportCategory
    phase: ReportPhase
    severity: ReportSeverity
    summary: str = Field(min_length=1, max_length=500)
    original_goal: str = Field(min_length=1, max_length=10_000)
    requirement_digest: Digest
    platform_contract_digest: Digest | None = None
    manuals_checked: list[ManualReference] = Field(default_factory=list, max_length=100)
    attempted_routes: list[AttemptedRoute] = Field(default_factory=list, max_length=100)
    expected: str | None = Field(default=None, max_length=20_000)
    actual: str | None = Field(default=None, max_length=20_000)
    reproduction: list[str] | None = Field(default=None, min_length=1, max_length=100)
    missing_contract: str | None = Field(default=None, max_length=20_000)
    blocking_scope: str = Field(min_length=1, max_length=10_000)
    independent_work: list[str] = Field(default_factory=list, max_length=100)
    workaround_considered: list[str] = Field(min_length=1, max_length=100)
    workaround_loss: str = Field(min_length=1, max_length=10_000)
    requested_outcome: str = Field(min_length=1, max_length=10_000)
    confidence: float = Field(ge=0, le=1)
    secret_redactions: list[str] = Field(max_length=100)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)

    @field_validator(
        "manuals_checked",
        "attempted_routes",
        "reproduction",
        "independent_work",
        "workaround_considered",
        "secret_redactions",
        "evidence_refs",
    )
    @classmethod
    def entries_are_unique(cls, value: list[Any] | None) -> list[Any] | None:
        if value is None:
            return None
        if not value:
            return value
        first = value[0]
        if isinstance(first, ManualReference):
            keys = [item.manual_id for item in value]
        elif isinstance(first, AttemptedRoute):
            keys = [item.attempt_id for item in value]
        elif isinstance(first, EvidenceRef):
            keys = [item.evidence_id for item in value]
        else:
            keys = value
        _unique(keys, label="report entries")
        return value

    def completeness_issues(self) -> tuple[str, ...]:
        """Return deterministic missing evidence fields without rejecting intake.

        Incomplete platform reports must be persistable so the state machine can
        place them in ``needs_more_evidence``.  They are not approvable until
        this tuple is empty.
        """

        issues: list[str] = []
        if not self.attempted_routes:
            issues.append("attempted_routes")
        if not self.expected:
            issues.append("expected")
        if not self.actual:
            issues.append("actual")
        if not self.evidence_refs:
            issues.append("evidence_refs")

        platform_report = self.category in {
            ReportCategory.platform_capability_gap,
            ReportCategory.platform_defect_suspected,
        }
        if platform_report:
            if self.platform_contract_digest is None:
                issues.append("platform_contract_digest")
            if not self.manuals_checked:
                issues.append("manuals_checked")
        if (
            self.category == ReportCategory.platform_capability_gap
            and not self.missing_contract
        ):
            issues.append("missing_contract")
        if (
            self.category == ReportCategory.platform_defect_suspected
            and not self.reproduction
        ):
            issues.append("reproduction")
        return tuple(issues)

    def is_complete_for_routing(self) -> bool:
        return not self.completeness_issues()

    @model_validator(mode="after")
    def direct_routes_require_complete_common_evidence(
        self,
    ) -> CollaborationReportPayload:
        if self.category in {
            ReportCategory.task_spec_gap,
            ReportCategory.environment_gap,
        }:
            issues = self.completeness_issues()
            if issues:
                raise ValueError(
                    "direct task and environment reports require common evidence fields: "
                    + ", ".join(issues)
                )
        return self


_CAPABILITY_STATUSES = {
    ReportStatus.observed,
    ReportStatus.evidence_collecting,
    ReportStatus.needs_more_evidence,
    ReportStatus.awaiting_user_review,
    ReportStatus.rejected,
    ReportStatus.approved_for_codex,
    ReportStatus.implementing,
    ReportStatus.ready_for_lilies_verification,
    ReportStatus.lilies_verified,
    ReportStatus.verification_failed,
    ReportStatus.independently_verified,
    ReportStatus.withdrawn,
}
_TASK_STATUSES = {
    ReportStatus.reported,
    ReportStatus.routed_to_task_author,
    ReportStatus.task_package_amended,
    ReportStatus.rejected_with_evidence,
    ReportStatus.lilies_rechecks,
}
_ENVIRONMENT_STATUSES = {
    ReportStatus.reported,
    ReportStatus.environment_failed,
    ReportStatus.routed_to_task_author,
    ReportStatus.environment_restored,
    ReportStatus.unresolved,
    ReportStatus.lilies_health_checks,
}

_CAPABILITY_ROUTE_STATUSES: dict[ReportRoute, frozenset[ReportStatus]] = {
    ReportRoute.capability_approval: frozenset(
        {
            ReportStatus.observed,
            ReportStatus.evidence_collecting,
            ReportStatus.needs_more_evidence,
            ReportStatus.awaiting_user_review,
            ReportStatus.rejected,
            ReportStatus.withdrawn,
        }
    ),
    ReportRoute.developer: frozenset(
        {
            ReportStatus.approved_for_codex,
            ReportStatus.implementing,
            ReportStatus.ready_for_lilies_verification,
            ReportStatus.lilies_verified,
            ReportStatus.verification_failed,
        }
    ),
    ReportRoute.verifier: frozenset(
        {
            ReportStatus.lilies_verified,
            ReportStatus.independently_verified,
            ReportStatus.verification_failed,
        }
    ),
}


class CollaborationReport(CollaborationReportPayload):
    channel_id: UUID
    source_message_id: UUID
    route: ReportRoute
    status: ReportStatus
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def route_and_status_match_category(self) -> CollaborationReport:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be earlier than created_at")
        if self.category == ReportCategory.task_spec_gap:
            if self.route != ReportRoute.task_author or self.status not in _TASK_STATUSES:
                raise ValueError("task_spec_gap requires task_author route and task status")
        elif self.category == ReportCategory.environment_gap:
            if self.route != ReportRoute.environment or self.status not in _ENVIRONMENT_STATUSES:
                raise ValueError("environment_gap requires environment route and environment status")
        else:
            allowed_statuses = _CAPABILITY_ROUTE_STATUSES.get(self.route)
            if (
                allowed_statuses is None
                or self.status not in _CAPABILITY_STATUSES
                or self.status not in allowed_statuses
            ):
                raise ValueError(
                    "platform report route and status must match the capability state machine"
                )
        return self


class ApprovalDecision(SafePayloadModel):
    schema_version: Literal["1.0"] = "1.0"
    approval_id: UUID
    channel_id: UUID
    report_id: UUID
    expected_report_revision: int = Field(ge=1)
    resulting_report_revision: int = Field(ge=2)
    decision: ReportDecision
    actor_id: ActorId
    reason: str | None = Field(default=None, max_length=10_000)
    idempotency_key: IdempotencyKey
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def decision_is_complete(self) -> ApprovalDecision:
        if self.resulting_report_revision != self.expected_report_revision + 1:
            raise ValueError("resulting_report_revision must increment expected_report_revision")
        if self.decision in {ReportDecision.reject, ReportDecision.needs_more_evidence}:
            if not self.reason:
                raise ValueError("reject and needs_more_evidence decisions require reason")
        return self


class TaskPackageAmendmentPayload(SafePayloadModel):
    """Task-author supplied fields, excluding route-bound server metadata."""

    schema_version: Literal["1.0"] = "1.0"
    amendment_id: UUID
    outcome: TaskAmendmentOutcome
    previous_task_revision: int = Field(ge=1)
    previous_requirement_digest: Digest
    new_task_revision: int | None = Field(default=None, ge=2)
    new_requirement_digest: Digest | None = None
    reason: str = Field(min_length=1, max_length=10_000)
    changes: list[str] = Field(default_factory=list, max_length=100)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def amendment_is_consistent(self) -> TaskPackageAmendmentPayload:
        if self.outcome == TaskAmendmentOutcome.amended:
            if self.new_task_revision != self.previous_task_revision + 1:
                raise ValueError("amendment must increment task revision by one")
            if self.new_requirement_digest is None:
                raise ValueError("amendment requires new_requirement_digest")
            if self.new_requirement_digest == self.previous_requirement_digest:
                raise ValueError("amendment must change requirement digest")
            if not self.changes:
                raise ValueError("amendment requires a non-empty changes list")
        elif self.new_task_revision is not None or self.new_requirement_digest is not None:
            raise ValueError("rejected task response must not claim a new revision or digest")
        return self


class TaskPackageAmendment(TaskPackageAmendmentPayload):
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    task_id: TaskId
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class EnvironmentResponsePayload(SafePayloadModel):
    """Task-author supplied environment result without route bindings."""

    schema_version: Literal["1.0"] = "1.0"
    response_id: UUID
    outcome: EnvironmentOutcome
    environment_digest: Digest
    summary: str = Field(min_length=1, max_length=10_000)
    health_checks: list[TestRunEvidence] = Field(min_length=1, max_length=100)
    known_limits: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def restored_response_has_passing_health_check(self) -> EnvironmentResponsePayload:
        if self.outcome == EnvironmentOutcome.restored and any(
            check.exit_code != 0 for check in self.health_checks
        ):
            raise ValueError("restored environment requires all health checks to pass")
        return self


class EnvironmentResponse(EnvironmentResponsePayload):
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class LiliesReprobeResultPayload(SafePayloadModel):
    """Lilies-supplied black-box result without path or server-time fields."""

    schema_version: Literal["1.0"] = "1.0"
    reprobe_id: UUID
    outcome: ReprobeOutcome
    contract_digest: Digest
    steps: list[ReprobeStep] = Field(min_length=1, max_length=100)
    expected: str = Field(min_length=1, max_length=20_000)
    actual: str = Field(min_length=1, max_length=20_000)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=500)

    @field_validator("steps")
    @classmethod
    def steps_are_contiguous(cls, value: list[ReprobeStep]) -> list[ReprobeStep]:
        if [step.order for step in value] != list(range(1, len(value) + 1)):
            raise ValueError("reprobe step order must be contiguous from one")
        return value


class LiliesReprobeResult(LiliesReprobeResultPayload):
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class DeveloperResponsePayload(SafePayloadModel):
    """Developer supplied result without report bindings or server time."""

    schema_version: Literal["1.0"] = "1.0"
    response_id: UUID
    outcome: DeveloperOutcome
    commit_sha: CommitSha | None = None
    implementation_diff_digest: Digest | None = None
    generic_capability_changes: list[str] = Field(min_length=1, max_length=100)
    generality_rationale: str | None = Field(
        default=None,
        min_length=1,
        max_length=10_000,
    )
    new_contract_digest: Digest | None = None
    tests_run: list[TestRunEvidence] = Field(min_length=1, max_length=200)
    browser_or_live_evidence: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    known_limits: list[str] = Field(default_factory=list, max_length=100)
    reprobe_steps: list[ReprobeStep] = Field(min_length=1, max_length=100)

    @field_validator("reprobe_steps")
    @classmethod
    def reprobe_steps_are_contiguous(cls, value: list[ReprobeStep]) -> list[ReprobeStep]:
        if [step.order for step in value] != list(range(1, len(value) + 1)):
            raise ValueError("reprobe step order must be contiguous from one")
        return value

    @model_validator(mode="after")
    def response_is_substantive_and_consistent(self) -> DeveloperResponsePayload:
        normalized = {change.strip().lower().rstrip(".!。") for change in self.generic_capability_changes}
        if normalized <= {"ok", "okay", "done", "fixed"}:
            raise ValueError("DeveloperResponse must describe substantive generic changes")
        if self.outcome == DeveloperOutcome.implemented:
            if self.commit_sha is None or self.new_contract_digest is None:
                raise ValueError("implemented response requires commit_sha and new_contract_digest")
            if any(test.exit_code != 0 for test in self.tests_run):
                raise ValueError("implemented response requires all declared tests to pass")
        elif self.commit_sha is not None:
            raise ValueError("non-implemented response must not claim an implementation commit")
        if (self.implementation_diff_digest is None) != (
            self.generality_rationale is None
        ):
            raise ValueError(
                "implementation diff digest and generality rationale must be supplied together"
            )
        return self


class DeveloperResponse(DeveloperResponsePayload):
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


def frozen_claim_context_digest(value: Mapping[str, Any]) -> str:
    """Digest the complete public, frozen verification context.

    The protected package digest remains verifier-only. The public package,
    environment-ready, archive, draft, run, artifact, and receipt bindings are
    safe for Lilies to submit and sufficient for the broker to resolve the
    protected package independently.
    """

    keys = (
        "claim_id",
        "application_id",
        "draft_revision",
        "content_hash",
        "published_version",
        "test_run_ids",
        "business_run_ids",
        "artifact_refs",
        "host_receipt_refs",
        "resolved_report_ids",
        "remaining_limits",
        "task_package_digest",
        "environment_ready_digest",
        "archive_manifest_digest",
        "verification_process_digest",
        "validation_mode",
    )
    context = {key: value.get(key) for key in keys}
    encoded = json.dumps(
        context,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


class VerificationClaimPayload(SafePayloadModel):
    """Lilies-supplied frozen-draft evidence without server-owned lifecycle fields."""

    schema_version: Literal["1.0", "1.1"] = "1.0"
    claim_id: UUID
    application_id: UUID
    draft_revision: int = Field(ge=0)
    content_hash: Digest
    published_version: int | None = Field(default=None, ge=1)
    test_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    business_run_ids: list[OpaqueReference] = Field(min_length=1, max_length=500)
    artifact_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    host_receipt_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    resolved_report_ids: list[UUID] = Field(default_factory=list, max_length=500)
    remaining_limits: list[str] = Field(default_factory=list, max_length=100)
    task_package_digest: Digest | None = None
    environment_ready_digest: Digest | None = None
    archive_manifest_digest: Digest | None = None
    verification_process_digest: Digest | None = None
    validation_mode: Literal["real_host"] | None = None
    frozen_context_digest: Digest | None = None
    claim: Literal["ready_for_independent_verification"]

    @field_validator(
        "test_run_ids",
        "business_run_ids",
        "artifact_refs",
        "host_receipt_refs",
        "resolved_report_ids",
        "remaining_limits",
    )
    @classmethod
    def claim_entries_are_unique(cls, value: list[Any]) -> list[Any]:
        if value and isinstance(value[0], EvidenceRef):
            keys = [item.evidence_id for item in value]
        else:
            keys = value
        _unique(keys, label="claim entries")
        return value

    @model_validator(mode="after")
    def frozen_context_is_complete(self) -> VerificationClaimPayload:
        frozen_fields = (
            self.task_package_digest,
            self.environment_ready_digest,
            self.archive_manifest_digest,
            self.verification_process_digest,
            self.validation_mode,
            self.frozen_context_digest,
        )
        if self.schema_version == "1.0":
            if any(item is not None for item in frozen_fields):
                raise ValueError("claim schema 1.0 cannot carry v1.1 frozen context")
            return self
        if any(item is None for item in frozen_fields):
            raise ValueError("claim schema 1.1 requires complete frozen context")
        expected = frozen_claim_context_digest(
            self.model_dump(mode="json", exclude_none=True)
        )
        if not hmac.compare_digest(str(self.frozen_context_digest), expected):
            raise ValueError("claim frozen_context_digest does not match its bindings")
        return self


class VerificationClaim(VerificationClaimPayload):
    channel_id: UUID
    assignment_id: UUID
    claim_revision: int = Field(default=1, ge=1)
    status: ClaimStatus = ClaimStatus.frozen
    created_at: datetime
    invalidated_at: datetime | None = None
    invalidation_reason: str | None = Field(default=None, max_length=10_000)

    @field_validator("created_at", "invalidated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value)

    @model_validator(mode="after")
    def invalidation_is_consistent(self) -> VerificationClaim:
        if self.status == ClaimStatus.invalidated:
            if self.invalidated_at is None or not self.invalidation_reason:
                raise ValueError("invalidated claim requires timestamp and reason")
            if self.invalidated_at < self.created_at:
                raise ValueError("invalidated_at cannot precede created_at")
        elif self.invalidated_at is not None or self.invalidation_reason is not None:
            raise ValueError("only an invalidated claim may carry invalidation metadata")
        return self


class DeveloperInboxResponse(StrictModel):
    """The complete and deliberately minimal developer inbox projection."""

    reports: list[CollaborationReport] = Field(default_factory=list, max_length=500)
    claims: list[VerificationClaim] = Field(default_factory=list, max_length=500)
    pending_user_action: bool
    next_cursor: int = Field(ge=0)


class VerificationDifference(StrictModel):
    check_id: OpaqueReference
    expected: str = Field(min_length=1, max_length=20_000)
    actual: str = Field(min_length=1, max_length=20_000)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=100)


class VerificationResultPayload(SafePayloadModel):
    """Verifier supplied oracle result without identity and claim bindings."""

    schema_version: Literal["1.0", "1.1"] = "1.0"
    verification_id: UUID
    verdict: VerificationVerdict
    oracle_digest: Digest
    differences: list[VerificationDifference] = Field(default_factory=list, max_length=500)
    evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=500)
    task_package_digest: Digest | None = None
    environment_ready_digest: Digest | None = None
    archive_manifest_digest: Digest | None = None
    frozen_context_digest: Digest | None = None
    verification_process_digest: Digest | None = None
    validation_mode: Literal["real_host"] | None = None

    @model_validator(mode="after")
    def verdict_matches_differences(self) -> VerificationResultPayload:
        if self.verdict == VerificationVerdict.independently_verified and self.differences:
            raise ValueError("successful verification must not contain differences")
        if self.verdict == VerificationVerdict.verification_failed and not self.differences:
            raise ValueError("failed verification requires expected/actual differences")
        frozen_fields = (
            self.task_package_digest,
            self.environment_ready_digest,
            self.archive_manifest_digest,
            self.frozen_context_digest,
            self.verification_process_digest,
            self.validation_mode,
        )
        if self.schema_version == "1.0":
            if any(item is not None for item in frozen_fields):
                raise ValueError("verification schema 1.0 cannot carry v1.1 context")
        elif any(item is None for item in frozen_fields):
            raise ValueError("verification schema 1.1 requires complete frozen context")
        return self


class VerificationResult(VerificationResultPayload):
    channel_id: UUID
    claim_id: UUID
    claim_revision: int = Field(ge=1)
    verifier_id: ActorId
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class DeveloperWorkspaceBinding(StrictModel):
    """Private source snapshot disclosed only with an owned developer lease."""

    schema_version: Literal["1.0"] = "1.0"
    task_id: TaskId
    task_revision: int = Field(ge=1)
    run_id: OpaqueReference
    assignment_id: UUID
    path: str = Field(min_length=1, max_length=4_096)
    manifest_digest: Digest
    policy_digest: Digest
    source_manifest_digest: Digest | None = None
    baseline_commit_sha: CommitSha | None = None
    baseline_tree_sha: CommitSha | None = None
    branch_ref: str | None = Field(default=None, min_length=6, max_length=1_024)
    allowed_new_prefixes: list[str] = Field(default_factory=list, max_length=100)
    allowed_new_files: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("path")
    @classmethod
    def path_is_absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("developer workspace path must be absolute")
        return value

    @model_validator(mode="after")
    def trusted_projection_is_complete(self) -> DeveloperWorkspaceBinding:
        projection_fields = (
            self.source_manifest_digest,
            self.baseline_commit_sha,
            self.baseline_tree_sha,
            self.branch_ref,
        )
        if any(item is not None for item in projection_fields) and any(
            item is None for item in projection_fields
        ):
            raise ValueError(
                "developer workspace projection requires complete Git baseline binding"
            )
        if self.source_manifest_digest is None and (
            self.allowed_new_prefixes or self.allowed_new_files
        ):
            raise ValueError(
                "legacy developer workspace cannot declare projection allowlists"
            )
        return self


class DeveloperLease(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    lease_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    owner_id: ActorId
    status: LeaseStatus
    revision: int = Field(default=1, ge=1)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    released_at: datetime | None = None
    developer_workspace: DeveloperWorkspaceBinding | None = None

    @field_validator("acquired_at", "heartbeat_at", "expires_at", "released_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return _require_utc(value)

    @model_validator(mode="after")
    def lease_lifetime_is_consistent(self) -> DeveloperLease:
        if self.heartbeat_at < self.acquired_at:
            raise ValueError("heartbeat_at cannot precede acquired_at")
        if self.expires_at <= self.heartbeat_at:
            raise ValueError("expires_at must be later than heartbeat_at")
        if self.status == LeaseStatus.released:
            if self.released_at is None or self.released_at < self.acquired_at:
                raise ValueError("released lease requires a valid released_at")
        elif self.released_at is not None:
            raise ValueError("only a released lease may carry released_at")
        return self


class ReaderCursor(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    channel_id: UUID
    reader_role: SenderRole
    reader_id: ActorId
    ack_seq: int = Field(ge=0)
    revision: int = Field(default=0, ge=0)
    updated_at: datetime | None = None

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @model_validator(mode="after")
    def initial_cursor_is_consistent(self) -> ReaderCursor:
        if self.revision == 0:
            if self.ack_seq != 0 or self.updated_at is not None:
                raise ValueError("initial reader cursor must be zero and omit updated_at")
        elif self.updated_at is None:
            raise ValueError("persisted reader cursor requires updated_at")
        return self


class ControlMessage(SafePayloadModel):
    schema_version: Literal["1.0"] = "1.0"
    control_id: UUID
    channel_id: UUID
    kind: ControlKind
    actor_id: ActorId
    reason: str = Field(min_length=1, max_length=10_000)
    report_id: UUID | None = None
    claim_id: UUID | None = None
    previous_value: str | None = Field(default=None, max_length=200)
    new_value: str | None = Field(default=None, max_length=200)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def transition_controls_have_values(self) -> ControlMessage:
        if self.kind in {
            ControlKind.approval_mode_changed,
            ControlKind.report_status_changed,
        } and (self.previous_value is None or self.new_value is None):
            raise ValueError("state-change control requires previous_value and new_value")
        if self.kind == ControlKind.claim_invalidated and self.claim_id is None:
            raise ValueError("claim_invalidated control requires claim_id")
        return self


_PAYLOAD_MODELS: dict[PayloadSchema, type[StrictModel]] = {
    PayloadSchema.report_v1: CollaborationReportPayload,
    PayloadSchema.approval_v1: ApprovalDecision,
    PayloadSchema.task_amendment_v1: TaskPackageAmendment,
    PayloadSchema.environment_response_v1: EnvironmentResponse,
    PayloadSchema.developer_response_v1: DeveloperResponse,
    PayloadSchema.verification_claim_v1: VerificationClaim,
    PayloadSchema.verification_result_v1: VerificationResult,
    PayloadSchema.lilies_reprobe_result_v1: LiliesReprobeResult,
    PayloadSchema.control_v1: ControlMessage,
}

_PAYLOAD_MESSAGE_TYPES: dict[PayloadSchema, MessageType] = {
    PayloadSchema.report_v1: MessageType.report,
    PayloadSchema.approval_v1: MessageType.approval,
    PayloadSchema.task_amendment_v1: MessageType.task_amendment,
    PayloadSchema.environment_response_v1: MessageType.environment_response,
    PayloadSchema.developer_response_v1: MessageType.developer_response,
    PayloadSchema.verification_claim_v1: MessageType.verification_claim,
    PayloadSchema.verification_result_v1: MessageType.verification_result,
    PayloadSchema.lilies_reprobe_result_v1: MessageType.control,
    PayloadSchema.control_v1: MessageType.control,
}

_MESSAGE_ACCESS_MATRIX: dict[
    PayloadSchema, frozenset[tuple[SenderRole, MessageVisibility]]
] = {
    PayloadSchema.report_v1: frozenset(
        {(SenderRole.lilies, MessageVisibility.user_and_lilies)}
    ),
    PayloadSchema.approval_v1: frozenset(
        {
            (SenderRole.user, MessageVisibility.user_and_lilies),
            (SenderRole.platform, MessageVisibility.user_and_lilies),
        }
    ),
    PayloadSchema.task_amendment_v1: frozenset(
        {(SenderRole.task_author, MessageVisibility.user_and_lilies)}
    ),
    PayloadSchema.environment_response_v1: frozenset(
        {(SenderRole.task_author, MessageVisibility.user_and_lilies)}
    ),
    PayloadSchema.developer_response_v1: frozenset(
        {(SenderRole.codex, MessageVisibility.user_and_lilies)}
    ),
    PayloadSchema.verification_claim_v1: frozenset(
        {(SenderRole.lilies, MessageVisibility.verifier)}
    ),
    PayloadSchema.verification_result_v1: frozenset(
        {(SenderRole.verifier, MessageVisibility.user_and_lilies)}
    ),
    PayloadSchema.lilies_reprobe_result_v1: frozenset(
        {(SenderRole.lilies, MessageVisibility.user_and_lilies)}
    ),
    PayloadSchema.control_v1: frozenset(
        {
            (SenderRole.lilies, MessageVisibility.user_and_lilies),
            (SenderRole.user, MessageVisibility.user_only),
            (SenderRole.user, MessageVisibility.user_and_lilies),
            (SenderRole.platform, MessageVisibility.user_only),
            (SenderRole.platform, MessageVisibility.user_and_lilies),
            (SenderRole.platform, MessageVisibility.approved_developer),
        }
    ),
}


class CollaborationMessageEnvelope(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    message_id: UUID
    channel_id: UUID
    seq: int = Field(ge=1)
    message_type: MessageType
    sender_role: SenderRole
    sender_id: ActorId
    correlation_id: UUID
    causal_parent_id: UUID | None = None
    idempotency_key: IdempotencyKey
    visibility: MessageVisibility
    payload_schema: PayloadSchema
    payload: dict[str, Any]
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=500)
    created_at: datetime

    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_fields(cls, value: Any) -> Any:
        if isinstance(value, dict) and "payload" in value:
            validate_collaboration_payload_safety(value["payload"])
        return value

    @field_validator("created_at")
    @classmethod
    def created_at_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("evidence_refs")
    @classmethod
    def evidence_is_unique(cls, value: list[EvidenceRef]) -> list[EvidenceRef]:
        _unique([item.evidence_id for item in value], label="envelope evidence_ids")
        return value

    @model_validator(mode="after")
    def payload_matches_declared_schema(self) -> CollaborationMessageEnvelope:
        if self.causal_parent_id == self.message_id:
            raise ValueError("message cannot be its own causal parent")
        expected_type = _PAYLOAD_MESSAGE_TYPES[self.payload_schema]
        if self.message_type != expected_type:
            raise ValueError(
                f"payload_schema {self.payload_schema.value} requires message_type "
                f"{expected_type.value}"
            )
        if (self.sender_role, self.visibility) not in _MESSAGE_ACCESS_MATRIX[
            self.payload_schema
        ]:
            raise ValueError(
                f"payload_schema {self.payload_schema.value} does not allow "
                f"sender_role {self.sender_role.value} with visibility "
                f"{self.visibility.value}"
            )
        payload_model = _PAYLOAD_MODELS[self.payload_schema]
        validated = payload_model.model_validate(self.payload)
        if self.payload_schema is PayloadSchema.control_v1 and self.sender_role is SenderRole.lilies:
            if not isinstance(validated, ControlMessage) or (
                validated.kind is not ControlKind.report_status_changed
                or validated.report_id is None
                or validated.report_id != self.correlation_id
                or validated.channel_id != self.channel_id
                or validated.actor_id != self.sender_id
                or validated.previous_value != ReportStatus.awaiting_user_review.value
                or validated.new_value != ReportStatus.withdrawn.value
                or validated.claim_id is not None
            ):
                raise ValueError(
                    "Lilies control messages are restricted to exact report withdrawal"
                )
        object.__setattr__(
            self,
            "payload",
            validated.model_dump(mode="json", exclude_none=True),
        )
        return self


# API mutation models keep idempotency and compare-and-set data explicit.  The
# service still assigns IDs, sequence numbers, status, routes, and timestamps.


class ReportSubmitRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_channel_revision: int = Field(ge=1)
    report: CollaborationReportPayload


class ReportRevisionRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_report_revision: int = Field(ge=1)
    report: CollaborationReportPayload


class ReportWithdrawalRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_report_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=10_000)


class ApprovalDecisionRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_report_revision: int = Field(ge=1)
    decision: ReportDecision
    reason: str | None = Field(default=None, max_length=10_000)

    @model_validator(mode="after")
    def reason_is_present_when_required(self) -> ApprovalDecisionRequest:
        if self.decision in {ReportDecision.reject, ReportDecision.needs_more_evidence}:
            if not self.reason:
                raise ValueError("reject and needs_more_evidence decisions require reason")
        return self


class ChannelSettingsRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_channel_revision: int = Field(ge=1)
    approval_mode: ApprovalMode
    confirmed: bool = False

    @model_validator(mode="after")
    def auto_forward_is_confirmed(self) -> ChannelSettingsRequest:
        if self.approval_mode == ApprovalMode.auto_forward and not self.confirmed:
            raise ValueError("auto_forward requires explicit confirmation")
        return self


class ChannelCloseRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_channel_revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=10_000)


class ReaderAckRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_cursor_revision: int = Field(ge=0)
    reader_role: SenderRole
    reader_id: ActorId
    ack_seq: int = Field(ge=0)


class LeaseAcquireRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_report_revision: int = Field(ge=1)
    owner_id: ActorId
    ttl_seconds: int = Field(default=900, ge=60, le=900)


class LeaseRenewRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_lease_revision: int = Field(ge=1)
    owner_id: ActorId
    ttl_seconds: int = Field(default=900, ge=60, le=900)


class LeaseReleaseRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_lease_revision: int = Field(ge=1)
    owner_id: ActorId
    reason: str = Field(min_length=1, max_length=2_000)


class DeveloperWorkerReceiptReference(StrictModel):
    """Opaque reference to a receipt held by the trusted worker broker."""

    receipt_id: UUID
    receipt_digest: Digest


class DeveloperResponseRequest(StrictModel):
    idempotency_key: IdempotencyKey
    lease_id: UUID
    lease_owner_id: ActorId
    expected_report_revision: int = Field(ge=1)
    developer_worker_receipt: DeveloperWorkerReceiptReference | None = None
    response: DeveloperResponsePayload


class DeveloperSourcePromotionRequest(StrictModel):
    """Developer request to promote only its lease-bound no-``.git`` delta."""

    idempotency_key: IdempotencyKey
    lease_id: UUID
    lease_owner_id: ActorId
    expected_report_revision: int = Field(ge=1)
    response_id: UUID
    workspace_manifest_digest: Digest
    source_manifest_digest: Digest
    developer_worker_receipt: DeveloperWorkerReceiptReference | None = None


class DeveloperSourcePromotionResult(StrictModel):
    """Public, strict receipt returned by trusted source promotion."""

    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    channel_id: UUID
    report_id: UUID
    report_revision: int = Field(ge=1)
    lease_id: UUID
    response_id: UUID
    workspace_manifest_digest: Digest
    source_manifest_digest: Digest
    intent_digest: Digest
    branch_ref: str = Field(min_length=6, max_length=1_024)
    parent_commit_sha: CommitSha
    parent_tree_sha: CommitSha
    commit_sha: CommitSha
    tree_sha: CommitSha
    changed_paths: list[str] = Field(min_length=1, max_length=5_000)
    object_state: Literal["object_created"]
    activation_state: Literal["activated"]
    reload_status: Literal["not_required", "restart_required", "confirmed"]
    effective: bool | None = None
    reload_confirmed: bool | None = None
    object_created_at: datetime
    activated_at: datetime
    process_instance_id: UUID
    receipt_digest: Digest

    @field_validator("object_created_at", "activated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class TaskPackageAmendmentRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_report_revision: int = Field(ge=1)
    amendment: TaskPackageAmendmentPayload


class EnvironmentResponseRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_report_revision: int = Field(ge=1)
    response: EnvironmentResponsePayload


class LiliesReprobeResultRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_report_revision: int = Field(ge=1)
    result: LiliesReprobeResultPayload


class VerificationClaimRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_channel_revision: int = Field(ge=1)
    claim: VerificationClaimPayload


class VerificationResultRequest(StrictModel):
    idempotency_key: IdempotencyKey
    expected_claim_revision: int = Field(ge=1)
    result: VerificationResultPayload
