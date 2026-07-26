from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import inspect
import json
import os
import re
import secrets
import sqlite3
import stat
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from .collaboration_models import (
    ChannelStatus,
    ClaimStatus,
    CollaborationChannel,
    VerificationClaim,
    VerificationClaimPayload,
)
from .collaboration_service import CollaborationError
from .formal_assignment_broker import (
    FormalAssignmentBroker,
    PrepareFormalAssignmentRequest,
    PreparedFormalAssignment,
)
from .formal_assignment_runtime import FormalAssignmentRuntimeError
from .formal_run_archiver import (
    FormalRunArchiveError,
    FormalRunArchiveInvalid,
    FormalRunArchiveIntentReceipt,
    FormalRunArchivePreparationRequest,
    FormalRunArchivePreparationResult,
    FormalRunArchiveUnavailable,
    FormalTerminalArchiveResult,
)
from .lilies_models import (
    AllowedAction,
    ApplicationTarget,
    ApplicationTargetMode,
    AssignmentConstraints,
    AssignmentMode,
    AssignmentNetworkPolicy,
    BuildAssignment,
    BusinessContext,
    AssignmentSubmissionResult,
    CredentialProvisionResult,
    CredentialRevokeResult,
    DaemonStatus,
    DeliverableSpec,
    FormalWorkspaceBundle,
    FormalWorkspaceFileEntry,
    FormalWorkspaceStagingReceipt,
    FormalWorkspaceStagingRequest,
    LocalScope,
    MAX_FORMAL_WORKSPACE_FILE_BYTES,
    MAX_FORMAL_WORKSPACE_FILES,
    MAX_FORMAL_WORKSPACE_TOTAL_BYTES,
    PairingExchangeResult,
    PermissionDecisionRequest,
    PermissionDecisionResult,
    PlatformAccess,
    PlatformScope,
    ProhibitedAction,
    SessionAckResult,
    SessionResult,
    SessionOperationResult,
    SessionStatus,
    formal_assignment_digest,
    validate_assignment_payload_safety,
)
from .local_lilies_client import (
    LocalLiliesClientError,
    LocalLiliesHttpClient,
    LocalLiliesRemoteError,
    LocalLiliesUnavailable,
)
from .platform_blackbox_auth import (
    PlatformBlackboxAuthStore,
    PlatformBlackboxNotFound,
    PlatformBlackboxOperation,
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from .platform_harness import PlatformHarness, PlatformHarnessViolation
from .task_packages import ArchiveStatus, TaskPackageNotReady
from .workflow_storage import WorkflowStorage


BRIDGE_SCHEMA_VERSION = 7
REQUIRED_DAEMON_SCOPES = (
    LocalScope.session_read,
    LocalScope.session_write,
    LocalScope.permission_resolve,
    LocalScope.credential_write,
)
DAEMON_CREDENTIAL_REVOCATION_REASON = "platform assignment cancellation"
DEFAULT_PLATFORM_SCOPES = (
    PlatformBlackboxScope.catalog_read,
    PlatformBlackboxScope.application_write,
    PlatformBlackboxScope.draft_write,
    PlatformBlackboxScope.test_execute,
    PlatformBlackboxScope.run_execute,
    PlatformBlackboxScope.trace_read,
    PlatformBlackboxScope.artifact_read,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(timezone.utc).isoformat()


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("persisted bridge timestamp must include timezone")
    return parsed.astimezone(timezone.utc)


class StrictBridgeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class BridgeConnectionStatus(str, Enum):
    pairing = "pairing"
    connected = "connected"
    unavailable = "unavailable"
    reconnecting = "reconnecting"
    expired = "expired"


class BridgeAssignmentPhase(str, Enum):
    recorded = "recorded"
    creating_session = "creating_session"
    provisioning_credential = "provisioning_credential"
    submitting = "submitting"
    running = "running"
    waiting = "waiting"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"
    unavailable = "unavailable"


class BridgeAssignmentStep(str, Enum):
    recorded = "recorded"
    session_created = "session_created"
    credential_issuing = "credential_issuing"
    credential_issued = "credential_issued"
    credential_provisioned = "credential_provisioned"
    collaboration_provisioning = "collaboration_provisioning"
    collaboration_provisioned = "collaboration_provisioned"
    workspace_staging = "workspace_staging"
    workspace_staged = "workspace_staged"
    submitting = "submitting"
    submitted = "submitted"
    running = "running"
    interrupted = "interrupted"
    completed = "completed"
    cancelled = "cancelled"
    error = "error"


class BridgeDesiredState(str, Enum):
    active = "active"
    cancelled = "cancelled"


class PairLocalLiliesRequest(StrictBridgeModel):
    idempotency_key: str = Field(min_length=16, max_length=128)
    base_url: str = Field(min_length=1, max_length=500)
    pairing_code: str = Field(min_length=8, max_length=80)
    expected_daemon_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ReconnectLocalLiliesRequest(StrictBridgeModel):
    idempotency_key: str = Field(min_length=16, max_length=128)
    pairing_code: str = Field(min_length=8, max_length=80)


class LocalLiliesBuildConstraints(StrictBridgeModel):
    deadline_at: datetime | None = None
    # A Local Lilies build edits the draft incrementally and validates the
    # result through public platform tools.  The former 36-turn default could
    # terminate a healthy build after substantial draft progress, while the
    # Studio offered no budget control.  Keep the route bounded, but leave
    # enough headroom for graph construction and acceptance repair.
    max_turns: int = Field(default=80, ge=5, le=200)
    max_budget_usd: float | None = Field(default=None, gt=0)
    max_tool_calls: int = Field(default=400, ge=1, le=1_000)
    network_policy: AssignmentNetworkPolicy = AssignmentNetworkPolicy.allowlist
    allowed_hosts: list[str] = Field(default_factory=list, max_length=100)

    @field_validator("deadline_at")
    @classmethod
    def deadline_is_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("deadline_at must use UTC")
        return value


class StartLocalLiliesBuildRequest(StrictBridgeModel):
    idempotency_key: str = Field(min_length=16, max_length=128)
    connection_id: UUID
    requirement: str = Field(min_length=10, max_length=30_000)
    business_context: BusinessContext
    deliverables: list[DeliverableSpec] = Field(min_length=1, max_length=100)
    constraints: LocalLiliesBuildConstraints | None = None
    auto_publish: bool = False

    @model_validator(mode="before")
    @classmethod
    def reject_plaintext_sensitive_content(cls, value: Any) -> Any:
        return validate_assignment_payload_safety(value)


class StartFormalLocalLiliesBuildRequest(StrictBridgeModel):
    """Caller selector for a sealed task revision; authority is not accepted."""

    idempotency_key: str = Field(min_length=16, max_length=128)
    connection_id: UUID
    task_id: str = Field(
        min_length=3,
        max_length=160,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    revision: int = Field(ge=1)
    environment_instance_id: str = Field(
        min_length=3,
        max_length=512,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    user_notified: Literal[True]


class LocalLiliesConnection(StrictBridgeModel):
    connection_id: UUID
    base_url: str
    daemon_fingerprint: str
    client_id: UUID | None = None
    granted_scopes: list[LocalScope] = Field(default_factory=list)
    expires_at: datetime | None = None
    status: BridgeConnectionStatus
    last_error: dict[str, str] | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LocalLiliesUsageItem(StrictBridgeModel):
    session_id: UUID | None = None
    stage: str | None = Field(default=None, min_length=1, max_length=120)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    recorded_calls: int = Field(ge=0)
    unknown_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def total_matches_components(self) -> LocalLiliesUsageItem:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("usage total_tokens must equal input_tokens plus output_tokens")
        return self


class LocalLiliesUsagePage(StrictBridgeModel):
    schema_version: Literal["1.0"]
    group_by: list[Literal["session", "stage", "model"]] = Field(
        min_length=1,
        max_length=3,
    )
    items: list[LocalLiliesUsageItem] = Field(default_factory=list, max_length=100)
    page: int = Field(ge=1, le=1_000)
    page_size: int = Field(ge=1, le=100)
    returned_count: int = Field(ge=0, le=100)
    total_items: int = Field(ge=0)
    total_pages: int = Field(ge=0, le=1_000)
    truncated: bool

    @model_validator(mode="after")
    def page_is_self_consistent(self) -> LocalLiliesUsagePage:
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("usage group_by dimensions must be unique")
        if self.returned_count != len(self.items):
            raise ValueError("usage returned_count must match items")
        unbounded_pages = (
            0
            if self.total_items == 0
            else (self.total_items + self.page_size - 1) // self.page_size
        )
        expected_pages = min(unbounded_pages, 1_000)
        if self.total_pages != expected_pages:
            raise ValueError("usage total_pages is inconsistent")
        if self.truncated is not (unbounded_pages > 1_000):
            raise ValueError("usage truncated flag is inconsistent")
        expected_count = min(
            self.page_size,
            max(self.total_items - (self.page - 1) * self.page_size, 0),
        )
        if self.returned_count != expected_count:
            raise ValueError("usage returned_count is inconsistent with the requested page")
        dimensions = set(self.group_by)
        group_keys: set[tuple[object, ...]] = set()
        for item in self.items:
            values = {
                "session": item.session_id,
                "stage": item.stage,
                "model": item.model,
            }
            if any((value is not None) != (name in dimensions) for name, value in values.items()):
                raise ValueError("usage item dimensions do not match group_by")
            group_key = tuple(values[name] for name in self.group_by)
            if group_key in group_keys:
                raise ValueError("usage items contain duplicate group keys")
            group_keys.add(group_key)
            if item.recorded_calls + item.unknown_calls < 1:
                raise ValueError("usage item must aggregate at least one call")
            if item.recorded_calls == 0 and any(
                (
                    item.input_tokens,
                    item.output_tokens,
                    item.total_tokens,
                    item.cost_usd,
                )
            ):
                raise ValueError("unknown-only usage item cannot include measured usage")
        return self


class LocalLiliesAssignment(StrictBridgeModel):
    assignment_id: UUID
    application_id: UUID
    build_id: UUID
    session_id: UUID
    connection_id: UUID
    phase: BridgeAssignmentPhase
    status: str
    desired_state: BridgeDesiredState
    daemon_status: SessionStatus | None = None
    relay_cursor: int = Field(ge=0)
    ack_cursor: int = Field(ge=0)
    last_error: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime


class LocalLiliesRelayEvent(StrictBridgeModel):
    assignment_id: UUID
    session_id: UUID
    daemon_seq: int = Field(ge=1)
    event_type: str = Field(min_length=1, max_length=200)
    data: dict[str, Any] = Field(default_factory=dict)
    received_at: datetime


class LocalLiliesRelayResult(StrictBridgeModel):
    assignment: LocalLiliesAssignment
    inserted: int = Field(ge=0)
    replayed: int = Field(ge=0)
    relay_cursor: int = Field(ge=0)
    ack_cursor: int = Field(ge=0)


class LocalLiliesRecoveryItem(StrictBridgeModel):
    assignment_id: UUID
    application_id: UUID
    build_id: UUID
    session_id: UUID
    connection_id: UUID
    outcome: Literal[
        "recovered",
        "waiting",
        "cancelled",
        "unavailable",
        "failed",
    ]
    phase: BridgeAssignmentPhase
    error_code: str | None = None


class LocalLiliesRecoverySummary(StrictBridgeModel):
    scanned: int = Field(ge=0)
    recovered: int = Field(ge=0)
    waiting: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    failed: int = Field(ge=0)
    assignments: list[LocalLiliesRecoveryItem] = Field(default_factory=list)


class LocalLiliesBridgeError(RuntimeError):
    code = "local_lilies_bridge_error"
    status_code = 400

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})

    def public_detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}


class LocalLiliesFeatureDisabled(LocalLiliesBridgeError):
    code = "feature_disabled"
    status_code = 404


class LocalLiliesBridgeNotFound(LocalLiliesBridgeError):
    code = "not_found"
    status_code = 404


class LocalLiliesBridgeConflict(LocalLiliesBridgeError):
    code = "idempotency_conflict"
    status_code = 409


class LocalLiliesBridgeUnavailable(LocalLiliesBridgeError):
    code = "daemon_unavailable"
    status_code = 503


class LocalLiliesBridgeSecurityError(LocalLiliesBridgeError):
    code = "security_boundary_violation"
    status_code = 400


class LocalLiliesRelayCursorGap(LocalLiliesBridgeSecurityError):
    code = "relay_cursor_gap"
    status_code = 502


class LocalLiliesBridgeDaemonRejected(LocalLiliesBridgeError):
    code = "daemon_rejected"
    status_code = 502


class _AssignmentCancellationWon(RuntimeError):
    def __init__(self, assignment: LocalLiliesAssignment) -> None:
        super().__init__("persisted assignment cancellation won the operation race")
        self.assignment = assignment


class _AssignmentDesiredStateChanged(RuntimeError):
    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__("assignment desired state changed during compare-and-set")
        self.row = row


class LocalLiliesBridgeStore:
    """Bridge-owned metadata and relay log; generated secrets never enter this DB."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)
        return conn

    async def initialize(self) -> dict[str, int]:
        async with self._lock:
            await asyncio.to_thread(self._initialize_sync)
        return {"schema_version": BRIDGE_SCHEMA_VERSION}

    def _initialize_sync(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_lilies_bridge_schema (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_lilies_connections (
                  id TEXT PRIMARY KEY,
                  idempotency_key TEXT NOT NULL UNIQUE,
                  request_digest TEXT NOT NULL,
                  base_url TEXT NOT NULL,
                  daemon_fingerprint TEXT NOT NULL,
                  client_id TEXT,
                  granted_scopes_json TEXT NOT NULL DEFAULT '[]',
                  access_token_secret_ref TEXT NOT NULL,
                  expires_at TEXT,
                  status TEXT NOT NULL,
                  last_error_code TEXT,
                  last_error_message TEXT,
                  last_seen_at TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_lilies_connection_operations (
                  connection_id TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  result_json TEXT,
                  completed_at TEXT,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(connection_id, operation, idempotency_key),
                  FOREIGN KEY(connection_id) REFERENCES local_lilies_connections(id)
                );
                CREATE TABLE IF NOT EXISTS local_lilies_assignments (
                  assignment_id TEXT PRIMARY KEY,
                  application_id TEXT NOT NULL,
                  build_id TEXT NOT NULL UNIQUE,
                  session_id TEXT NOT NULL UNIQUE,
                  connection_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  request_json TEXT NOT NULL,
                  assignment_mode TEXT NOT NULL DEFAULT 'customer',
                  phase TEXT NOT NULL,
                  status TEXT NOT NULL,
                  desired_state TEXT NOT NULL,
                  daemon_status TEXT,
                  daemon_session_creation_started_at TEXT,
                  credential_ref TEXT,
                  collaboration_credential_ref TEXT,
                  task_token_secret_ref TEXT NOT NULL,
                  relay_cursor INTEGER NOT NULL DEFAULT 0,
                  ack_cursor INTEGER NOT NULL DEFAULT 0,
                  terminal_events_drained_at TEXT,
                  submission_json TEXT,
                  formal_workspace_receipt_json TEXT,
                  formal_channel_close_receipt_json TEXT,
                  formal_archive_intent_json TEXT,
                  formal_archive_intent_digest TEXT,
                  formal_archive_intent_accepted_at TEXT,
                  formal_archive_result_json TEXT,
                  formal_claim_result_json TEXT,
                  formal_archive_completed_at TEXT,
                  formal_terminal_archive_result_json TEXT,
                  formal_terminal_archive_manifest_digest TEXT,
                  formal_terminal_archive_completed_at TEXT,
                  last_error_code TEXT,
                  last_error_message TEXT,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  UNIQUE(application_id, idempotency_key),
                  FOREIGN KEY(connection_id) REFERENCES local_lilies_connections(id)
                );
                CREATE TABLE IF NOT EXISTS local_lilies_assignment_events (
                  assignment_id TEXT NOT NULL,
                  daemon_seq INTEGER NOT NULL,
                  session_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  data_json TEXT NOT NULL,
                  received_at TEXT NOT NULL,
                  PRIMARY KEY(assignment_id, daemon_seq),
                  FOREIGN KEY(assignment_id) REFERENCES local_lilies_assignments(assignment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_local_lilies_assignment_application
                  ON local_lilies_assignments(application_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_local_lilies_assignment_connection
                  ON local_lilies_assignments(connection_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_local_lilies_events_session
                  ON local_lilies_assignment_events(session_id, daemon_seq);
                """
            )
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM local_lilies_bridge_schema"
                ).fetchone()["version"]
            )
            if current > BRIDGE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"local Lilies bridge schema {current} is newer than supported "
                    f"{BRIDGE_SCHEMA_VERSION}"
                )
            if current < 1:
                conn.execute(
                    "INSERT INTO local_lilies_bridge_schema(version,applied_at) VALUES (?,?)",
                    (1, _now().isoformat()),
                )
                current = 1
            if current < 2:
                columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(local_lilies_connection_operations)"
                    ).fetchall()
                }
                if "result_json" not in columns:
                    conn.execute(
                        "ALTER TABLE local_lilies_connection_operations ADD COLUMN result_json TEXT"
                    )
                if "completed_at" not in columns:
                    conn.execute(
                        "ALTER TABLE local_lilies_connection_operations "
                        "ADD COLUMN completed_at TEXT"
                    )
                conn.execute(
                    "INSERT INTO local_lilies_bridge_schema(version,applied_at) VALUES (?,?)",
                    (2, _now().isoformat()),
                )
                current = 2
            if current < 3:
                assignment_columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(local_lilies_assignments)"
                    ).fetchall()
                }
                if "terminal_events_drained_at" not in assignment_columns:
                    conn.execute(
                        "ALTER TABLE local_lilies_assignments "
                        "ADD COLUMN terminal_events_drained_at TEXT"
                    )
                conn.execute(
                    "INSERT INTO local_lilies_bridge_schema(version,applied_at) VALUES (?,?)",
                    (3, _now().isoformat()),
                )
                current = 3
            if current < 4:
                assignment_columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(local_lilies_assignments)"
                    ).fetchall()
                }
                for column, declaration in (
                    (
                        "assignment_mode",
                        "TEXT NOT NULL DEFAULT 'customer'",
                    ),
                    ("collaboration_credential_ref", "TEXT"),
                    ("formal_workspace_receipt_json", "TEXT"),
                    ("formal_channel_close_receipt_json", "TEXT"),
                ):
                    if column not in assignment_columns:
                        conn.execute(
                            "ALTER TABLE local_lilies_assignments "
                            f"ADD COLUMN {column} {declaration}"
                        )
                conn.execute(
                    "INSERT INTO local_lilies_bridge_schema(version,applied_at) VALUES(?,?)",
                    (4, _now().isoformat()),
                )
                current = 4
            if current < 5:
                assignment_columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(local_lilies_assignments)"
                    ).fetchall()
                }
                for column, declaration in (
                    ("formal_archive_intent_json", "TEXT"),
                    ("formal_archive_intent_digest", "TEXT"),
                    ("formal_archive_intent_accepted_at", "TEXT"),
                    ("formal_archive_result_json", "TEXT"),
                    ("formal_claim_result_json", "TEXT"),
                    ("formal_archive_completed_at", "TEXT"),
                ):
                    if column not in assignment_columns:
                        conn.execute(
                            "ALTER TABLE local_lilies_assignments "
                            f"ADD COLUMN {column} {declaration}"
                        )
                conn.execute(
                    "INSERT INTO local_lilies_bridge_schema(version,applied_at) VALUES(?,?)",
                    (5, _now().isoformat()),
                )
                current = 5
            if current < 6:
                assignment_columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(local_lilies_assignments)"
                    ).fetchall()
                }
                for column, declaration in (
                    ("formal_terminal_archive_result_json", "TEXT"),
                    ("formal_terminal_archive_manifest_digest", "TEXT"),
                    ("formal_terminal_archive_completed_at", "TEXT"),
                ):
                    if column not in assignment_columns:
                        conn.execute(
                            "ALTER TABLE local_lilies_assignments "
                            f"ADD COLUMN {column} {declaration}"
                        )
                conn.execute(
                    "INSERT INTO local_lilies_bridge_schema(version,applied_at) VALUES(?,?)",
                    (6, _now().isoformat()),
                )
                current = 6
            if current < 7:
                assignment_columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(local_lilies_assignments)"
                    ).fetchall()
                }
                if "daemon_session_creation_started_at" not in assignment_columns:
                    conn.execute(
                        "ALTER TABLE local_lilies_assignments "
                        "ADD COLUMN daemon_session_creation_started_at TEXT"
                    )
                conn.execute(
                    "INSERT INTO local_lilies_bridge_schema(version,applied_at) VALUES(?,?)",
                    (7, _now().isoformat()),
                )
                current = 7
        for path in (self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            if path.exists():
                os.chmod(path, 0o600)

    async def reserve_connection(
        self,
        *,
        connection_id: UUID,
        request: PairLocalLiliesRequest,
        base_url: str,
        request_digest: str,
        secret_ref: str,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_connection_sync,
                str(connection_id),
                request,
                base_url,
                request_digest,
                secret_ref,
            )

    def _reserve_connection_sync(
        self,
        connection_id: str,
        request: PairLocalLiliesRequest,
        base_url: str,
        request_digest: str,
        secret_ref: str,
    ) -> dict[str, Any]:
        now = _now().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM local_lilies_connections WHERE idempotency_key=?",
                (request.idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["request_digest"] != request_digest:
                    raise LocalLiliesBridgeConflict(
                        "connection idempotency key was reused with another request"
                    )
                return dict(row)
            conn.execute(
                """
                INSERT INTO local_lilies_connections(
                  id,idempotency_key,request_digest,base_url,daemon_fingerprint,
                  access_token_secret_ref,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,'pairing',?,?)
                """,
                (
                    connection_id,
                    request.idempotency_key,
                    request_digest,
                    base_url,
                    request.expected_daemon_fingerprint,
                    secret_ref,
                    now,
                    now,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM local_lilies_connections WHERE id=?",
                    (connection_id,),
                ).fetchone()
            )

    async def complete_connection(
        self,
        connection_id: UUID | str,
        *,
        client_id: UUID,
        scopes: list[LocalScope],
        expires_at: datetime | None,
        fingerprint: str,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._complete_connection_sync,
                str(connection_id),
                str(client_id),
                [scope.value for scope in scopes],
                _iso(expires_at),
                fingerprint,
            )

    def _complete_connection_sync(
        self,
        connection_id: str,
        client_id: str,
        scopes: list[str],
        expires_at: str | None,
        fingerprint: str,
    ) -> dict[str, Any]:
        now = _now().isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE local_lilies_connections
                SET client_id=?,granted_scopes_json=?,expires_at=?,daemon_fingerprint=?,
                    status='connected',last_error_code=NULL,last_error_message=NULL,
                    last_seen_at=?,updated_at=?
                WHERE id=?
                """,
                (
                    client_id,
                    _canonical_json(scopes),
                    expires_at,
                    fingerprint,
                    now,
                    now,
                    connection_id,
                ),
            )
            if cursor.rowcount != 1:
                raise LocalLiliesBridgeNotFound("local Lilies connection not found")
            return dict(
                conn.execute(
                    "SELECT * FROM local_lilies_connections WHERE id=?",
                    (connection_id,),
                ).fetchone()
            )

    async def set_connection_state(
        self,
        connection_id: UUID | str,
        *,
        status: BridgeConnectionStatus,
        error_code: str | None = None,
        error_message: str | None = None,
        seen: bool = False,
        expected_statuses: tuple[BridgeConnectionStatus, ...] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._set_connection_state_sync,
                str(connection_id),
                status.value,
                error_code,
                error_message,
                seen,
                (
                    tuple(item.value for item in expected_statuses)
                    if expected_statuses is not None
                    else None
                ),
            )

    def _set_connection_state_sync(
        self,
        connection_id: str,
        status: str,
        error_code: str | None,
        error_message: str | None,
        seen: bool,
        expected_statuses: tuple[str, ...] | None,
    ) -> dict[str, Any]:
        now = _now().isoformat()
        with self._connect() as conn:
            status_guard = ""
            parameters: list[Any] = [
                status,
                error_code,
                error_message,
                int(seen),
                now,
                now,
                connection_id,
            ]
            if expected_statuses is not None:
                if not expected_statuses:
                    raise ValueError("expected connection statuses cannot be empty")
                placeholders = ",".join("?" for _ in expected_statuses)
                status_guard = f" AND status IN ({placeholders})"
                parameters.extend(expected_statuses)
            cursor = conn.execute(
                f"""
                UPDATE local_lilies_connections
                SET status=?,last_error_code=?,last_error_message=?,
                    last_seen_at=CASE WHEN ? THEN ? ELSE last_seen_at END,updated_at=?
                WHERE id=?{status_guard}
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                existing = conn.execute(
                    "SELECT * FROM local_lilies_connections WHERE id=?",
                    (connection_id,),
                ).fetchone()
                if existing is None:
                    raise LocalLiliesBridgeNotFound("local Lilies connection not found")
                if expected_statuses is not None:
                    return dict(existing)
                raise LocalLiliesBridgeNotFound("local Lilies connection not found")
            return dict(
                conn.execute(
                    "SELECT * FROM local_lilies_connections WHERE id=?",
                    (connection_id,),
                ).fetchone()
            )

    async def get_connection(self, connection_id: UUID | str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_connection_sync, str(connection_id))

    def _get_connection_sync(self, connection_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM local_lilies_connections WHERE id=?",
                (connection_id,),
            ).fetchone()
        if row is None:
            raise LocalLiliesBridgeNotFound("local Lilies connection not found")
        return dict(row)

    async def list_connections(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_connections_sync)

    def _list_connections_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM local_lilies_connections ORDER BY created_at,id"
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_pending_connection_operations(self, *, operation: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_pending_connection_operations_sync, operation)

    def _list_pending_connection_operations_sync(self, operation: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM local_lilies_connection_operations
                WHERE operation=? AND completed_at IS NULL
                ORDER BY created_at,connection_id,idempotency_key
                """,
                (operation,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def reserve_connection_operation(
        self,
        connection_id: UUID | str,
        *,
        operation: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_connection_operation_sync,
                str(connection_id),
                operation,
                idempotency_key,
                request_digest,
            )

    def _reserve_connection_operation_sync(
        self,
        connection_id: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM local_lilies_connection_operations
                WHERE connection_id=? AND operation=? AND idempotency_key=?
                """,
                (connection_id, operation, idempotency_key),
            ).fetchone()
            if row is not None:
                if row["request_digest"] != request_digest:
                    raise LocalLiliesBridgeConflict(
                        f"{operation} idempotency key was reused with another request"
                    )
                return dict(row)
            conn.execute(
                """
                INSERT INTO local_lilies_connection_operations(
                  connection_id,operation,idempotency_key,request_digest,created_at
                ) VALUES(?,?,?,?,?)
                """,
                (connection_id, operation, idempotency_key, request_digest, _now().isoformat()),
            )
            return dict(
                conn.execute(
                    """
                    SELECT * FROM local_lilies_connection_operations
                    WHERE connection_id=? AND operation=? AND idempotency_key=?
                    """,
                    (connection_id, operation, idempotency_key),
                ).fetchone()
            )

    async def complete_connection_operation(
        self,
        connection_id: UUID | str,
        *,
        operation: str,
        idempotency_key: str,
        result: LocalLiliesConnection,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._complete_connection_operation_sync,
                str(connection_id),
                operation,
                idempotency_key,
                result.model_dump_json(exclude_none=True),
            )

    def _complete_connection_operation_sync(
        self,
        connection_id: str,
        operation: str,
        idempotency_key: str,
        result_json: str,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE local_lilies_connection_operations
                SET result_json=?,completed_at=?
                WHERE connection_id=? AND operation=? AND idempotency_key=?
                """,
                (
                    result_json,
                    _now().isoformat(),
                    connection_id,
                    operation,
                    idempotency_key,
                ),
            )
            if cursor.rowcount != 1:
                raise LocalLiliesBridgeNotFound("local Lilies connection operation not found")

    async def reserve_assignment(
        self,
        *,
        assignment_id: UUID,
        application_id: UUID,
        build_id: UUID,
        session_id: UUID,
        request: StartLocalLiliesBuildRequest | StartFormalLocalLiliesBuildRequest,
        request_digest: str,
        request_json: str,
        task_token_secret_ref: str,
        assignment_mode: AssignmentMode = AssignmentMode.customer,
    ) -> tuple[dict[str, Any], bool]:
        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_assignment_sync,
                str(assignment_id),
                str(application_id),
                str(build_id),
                str(session_id),
                request,
                request_digest,
                request_json,
                task_token_secret_ref,
                assignment_mode.value,
            )

    def _reserve_assignment_sync(
        self,
        assignment_id: str,
        application_id: str,
        build_id: str,
        session_id: str,
        request: StartLocalLiliesBuildRequest | StartFormalLocalLiliesBuildRequest,
        request_digest: str,
        request_json: str,
        task_token_secret_ref: str,
        assignment_mode: str,
    ) -> tuple[dict[str, Any], bool]:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM local_lilies_assignments
                WHERE application_id=? AND idempotency_key=?
                """,
                (application_id, request.idempotency_key),
            ).fetchone()
            if row is not None:
                if row["request_digest"] != request_digest:
                    raise LocalLiliesBridgeConflict(
                        "build idempotency key was reused with another request"
                    )
                return dict(row), True
            active = conn.execute(
                """
                SELECT * FROM local_lilies_assignments
                WHERE application_id=?
                  AND phase NOT IN ('completed','cancelled','error')
                ORDER BY created_at DESC,assignment_id DESC
                LIMIT 1
                """,
                (application_id,),
            ).fetchone()
            if active is not None:
                raise LocalLiliesBridgeConflict(
                    "application already has a nonterminal local Lilies assignment",
                    details={
                        "application_id": application_id,
                        "assignment_id": str(active["assignment_id"]),
                        "build_id": str(active["build_id"]),
                        "session_id": str(active["session_id"]),
                        "connection_id": str(active["connection_id"]),
                        "phase": str(active["phase"]),
                        "status": str(active["status"]),
                    },
                )
            conn.execute(
                """
                INSERT INTO local_lilies_assignments(
                  assignment_id,application_id,build_id,session_id,connection_id,
                  idempotency_key,request_digest,request_json,assignment_mode,
                  phase,status,desired_state,
                  task_token_secret_ref,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,'queued','active',?,?,?)
                """,
                (
                    assignment_id,
                    application_id,
                    build_id,
                    session_id,
                    str(request.connection_id),
                    request.idempotency_key,
                    request_digest,
                    request_json,
                    assignment_mode,
                    BridgeAssignmentStep.recorded.value,
                    task_token_secret_ref,
                    now,
                    now,
                ),
            )
            return (
                dict(
                    conn.execute(
                        "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                        (assignment_id,),
                    ).fetchone()
                ),
                False,
            )

    async def update_assignment(
        self,
        assignment_id: UUID | str,
        *,
        expected_desired_state: BridgeDesiredState | str | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        allowed = {
            "phase",
            "status",
            "desired_state",
            "daemon_status",
            "daemon_session_creation_started_at",
            "credential_ref",
            "collaboration_credential_ref",
            "relay_cursor",
            "ack_cursor",
            "terminal_events_drained_at",
            "submission_json",
            "formal_workspace_receipt_json",
            "formal_channel_close_receipt_json",
            "formal_archive_intent_json",
            "formal_archive_intent_digest",
            "formal_archive_intent_accepted_at",
            "formal_archive_result_json",
            "formal_claim_result_json",
            "formal_archive_completed_at",
            "formal_terminal_archive_result_json",
            "formal_terminal_archive_manifest_digest",
            "formal_terminal_archive_completed_at",
            "last_error_code",
            "last_error_message",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown assignment fields: {sorted(unknown)}")
        normalized = {
            key: (value.value if isinstance(value, Enum) else value)
            for key, value in changes.items()
        }
        normalized_expected = (
            expected_desired_state.value
            if isinstance(expected_desired_state, BridgeDesiredState)
            else expected_desired_state
        )
        async with self._lock:
            return await asyncio.to_thread(
                self._update_assignment_sync,
                str(assignment_id),
                normalized,
                normalized_expected,
            )

    def _update_assignment_sync(
        self,
        assignment_id: str,
        changes: dict[str, Any],
        expected_desired_state: str | None,
    ) -> dict[str, Any]:
        if not changes:
            return self._get_assignment_sync(assignment_id)
        values = {**changes, "updated_at": _now().isoformat()}
        columns = ",".join(f"{key}=?" for key in values)
        with self._connect() as conn:
            desired_clause = " AND desired_state=?" if expected_desired_state is not None else ""
            parameters: tuple[Any, ...] = (*values.values(), assignment_id)
            if expected_desired_state is not None:
                parameters = (*parameters, expected_desired_state)
            cursor = conn.execute(
                f"UPDATE local_lilies_assignments SET {columns} "
                f"WHERE assignment_id=?{desired_clause}",
                parameters,
            )
            if cursor.rowcount != 1:
                current = conn.execute(
                    "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                    (assignment_id,),
                ).fetchone()
                if current is None:
                    raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
                if expected_desired_state is not None:
                    raise _AssignmentDesiredStateChanged(dict(current))
                raise LocalLiliesBridgeConflict("local Lilies assignment update did not commit")
            return dict(
                conn.execute(
                    "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                    (assignment_id,),
                ).fetchone()
            )

    async def seal_formal_pre_submission_rejection(
        self,
        assignment_id: UUID | str,
        *,
        error_code: str,
        error_message: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically seal a formal reservation that never reached the daemon."""

        async with self._lock:
            return await asyncio.to_thread(
                self._seal_formal_pre_submission_rejection_sync,
                str(assignment_id),
                error_code,
                error_message,
            )

    def _seal_formal_pre_submission_rejection_sync(
        self,
        assignment_id: str,
        error_code: str,
        error_message: str,
    ) -> tuple[dict[str, Any], bool]:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if current_row is None:
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            current = dict(current_row)
            if current["desired_state"] != BridgeDesiredState.active.value:
                raise _AssignmentDesiredStateChanged(current)
            cursor = conn.execute(
                """
                UPDATE local_lilies_assignments
                SET phase='error',
                    status='failed',
                    terminal_events_drained_at=?,
                    last_error_code=?,
                    last_error_message=?,
                    updated_at=?
                WHERE assignment_id=?
                  AND assignment_mode='formal_experiment'
                  AND phase='recorded'
                  AND desired_state='active'
                  AND daemon_status IS NULL
                  AND daemon_session_creation_started_at IS NULL
                  AND credential_ref IS NULL
                  AND collaboration_credential_ref IS NULL
                  AND formal_workspace_receipt_json IS NULL
                  AND formal_channel_close_receipt_json IS NULL
                  AND relay_cursor=0
                  AND ack_cursor=0
                  AND terminal_events_drained_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM local_lilies_assignment_events
                    WHERE assignment_id=?
                  )
                """,
                (
                    now,
                    error_code,
                    error_message,
                    now,
                    assignment_id,
                    assignment_id,
                ),
            )
            stored = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            return dict(stored), cursor.rowcount == 1

    async def get_assignment(self, assignment_id: UUID | str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_assignment_sync, str(assignment_id))

    def _get_assignment_sync(self, assignment_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
        if row is None:
            raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
        return dict(row)

    async def reserve_formal_archive_intent(
        self,
        assignment_id: UUID | str,
        *,
        intent_json: str,
        intent_digest: str,
    ) -> tuple[dict[str, Any], bool]:
        """Freeze the one Lilies-authored completion intent for an assignment."""

        async with self._lock:
            return await asyncio.to_thread(
                self._reserve_formal_archive_intent_sync,
                str(assignment_id),
                intent_json,
                intent_digest,
            )

    def _reserve_formal_archive_intent_sync(
        self,
        assignment_id: str,
        intent_json: str,
        intent_digest: str,
    ) -> tuple[dict[str, Any], bool]:
        accepted_at = _now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            current = dict(row)
            persisted_digest = current.get("formal_archive_intent_digest")
            if persisted_digest is not None:
                if (
                    not hmac.compare_digest(str(persisted_digest), intent_digest)
                    or str(current.get("formal_archive_intent_json") or "") != intent_json
                ):
                    raise LocalLiliesBridgeConflict(
                        "formal archive intent is already frozen with another payload",
                        details=LocalLiliesBridge._safe_assignment_ids(current),
                    )
                return current, True
            if (
                str(current.get("assignment_mode")) != AssignmentMode.formal_experiment.value
                or not current.get("submission_json")
                or str(current.get("desired_state")) != BridgeDesiredState.active.value
                or str(current.get("phase"))
                in {
                    BridgeAssignmentStep.completed.value,
                    BridgeAssignmentStep.cancelled.value,
                    BridgeAssignmentStep.error.value,
                }
            ):
                raise LocalLiliesBridgeConflict(
                    "formal archive intent must be frozen by a running formal assignment",
                    details=LocalLiliesBridge._safe_assignment_ids(current),
                )
            conn.execute(
                """
                UPDATE local_lilies_assignments
                SET formal_archive_intent_json=?,
                    formal_archive_intent_digest=?,
                    formal_archive_intent_accepted_at=?,
                    updated_at=?
                WHERE assignment_id=?
                  AND formal_archive_intent_digest IS NULL
                """,
                (
                    intent_json,
                    intent_digest,
                    accepted_at,
                    accepted_at,
                    assignment_id,
                ),
            )
            stored = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if stored is None:  # pragma: no cover - guarded by the transaction
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            return dict(stored), False

    async def commit_formal_archive_result(
        self,
        assignment_id: UUID | str,
        *,
        intent_digest: str,
        result_json: str,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._commit_formal_archive_result_sync,
                str(assignment_id),
                intent_digest,
                result_json,
            )

    def _commit_formal_archive_result_sync(
        self,
        assignment_id: str,
        intent_digest: str,
        result_json: str,
    ) -> dict[str, Any]:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            current = dict(row)
            if not hmac.compare_digest(
                str(current.get("formal_archive_intent_digest") or ""),
                intent_digest,
            ):
                raise LocalLiliesBridgeConflict(
                    "formal archive result does not match the frozen intent",
                    details=LocalLiliesBridge._safe_assignment_ids(current),
                )
            if (
                str(current.get("phase")) != BridgeAssignmentStep.completed.value
                or current.get("terminal_events_drained_at") is None
            ):
                raise LocalLiliesBridgeConflict(
                    "formal archive result requires a drained completed assignment",
                    details=LocalLiliesBridge._safe_assignment_ids(current),
                )
            persisted = current.get("formal_archive_result_json")
            if persisted is not None:
                if str(persisted) != result_json:
                    raise LocalLiliesBridgeConflict(
                        "formal archive replay changed its result",
                        details=LocalLiliesBridge._safe_assignment_ids(current),
                    )
                return current
            conn.execute(
                """
                UPDATE local_lilies_assignments
                SET formal_archive_result_json=?,
                    status='verification_pending',
                    last_error_code=NULL,
                    last_error_message=NULL,
                    updated_at=?
                WHERE assignment_id=? AND formal_archive_result_json IS NULL
                """,
                (result_json, now, assignment_id),
            )
            stored = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            return dict(stored)

    async def complete_formal_archive_claim(
        self,
        assignment_id: UUID | str,
        *,
        intent_digest: str,
        claim_result_json: str,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._complete_formal_archive_claim_sync,
                str(assignment_id),
                intent_digest,
                claim_result_json,
            )

    def _complete_formal_archive_claim_sync(
        self,
        assignment_id: str,
        intent_digest: str,
        claim_result_json: str,
    ) -> dict[str, Any]:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            current = dict(row)
            if (
                not hmac.compare_digest(
                    str(current.get("formal_archive_intent_digest") or ""),
                    intent_digest,
                )
                or current.get("formal_archive_result_json") is None
            ):
                raise LocalLiliesBridgeConflict(
                    "formal verification claim has no matching archived intent",
                    details=LocalLiliesBridge._safe_assignment_ids(current),
                )
            persisted = current.get("formal_claim_result_json")
            if persisted is not None:
                if str(persisted) != claim_result_json:
                    raise LocalLiliesBridgeConflict(
                        "formal verification claim replay changed its result",
                        details=LocalLiliesBridge._safe_assignment_ids(current),
                    )
                return current
            conn.execute(
                """
                UPDATE local_lilies_assignments
                SET formal_claim_result_json=?,
                    status='verification_pending',
                    last_error_code=NULL,
                    last_error_message=NULL,
                    updated_at=?
                WHERE assignment_id=? AND formal_claim_result_json IS NULL
                """,
                (claim_result_json, now, assignment_id),
            )
            stored = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            return dict(stored)

    async def mark_formal_archive_completed(
        self,
        assignment_id: UUID | str,
        *,
        intent_digest: str,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._mark_formal_archive_completed_sync,
                str(assignment_id),
                intent_digest,
            )

    def _mark_formal_archive_completed_sync(
        self,
        assignment_id: str,
        intent_digest: str,
    ) -> dict[str, Any]:
        """Commit the final checkpoint only after every external projection."""

        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            current = dict(row)
            if (
                not hmac.compare_digest(
                    str(current.get("formal_archive_intent_digest") or ""),
                    intent_digest,
                )
                or str(current.get("phase")) != BridgeAssignmentStep.completed.value
                or current.get("terminal_events_drained_at") is None
                or current.get("formal_archive_result_json") is None
                or current.get("formal_claim_result_json") is None
            ):
                raise LocalLiliesBridgeConflict(
                    "formal archive completion has unfinished durable projections",
                    details=LocalLiliesBridge._safe_assignment_ids(current),
                )
            if current.get("formal_archive_completed_at") is not None:
                return current
            conn.execute(
                """
                UPDATE local_lilies_assignments
                SET formal_archive_completed_at=?,
                    status='verification_pending',
                    last_error_code=NULL,
                    last_error_message=NULL,
                    updated_at=?
                WHERE assignment_id=? AND formal_archive_completed_at IS NULL
                """,
                (now, now, assignment_id),
            )
            stored = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            return dict(stored)

    async def commit_formal_terminal_archive_result(
        self,
        assignment_id: UUID | str,
        *,
        result_json: str,
        manifest_digest: str,
    ) -> dict[str, Any]:
        """Atomically checkpoint one immutable failed/cancelled run archive."""

        async with self._lock:
            return await asyncio.to_thread(
                self._commit_formal_terminal_archive_result_sync,
                str(assignment_id),
                result_json,
                manifest_digest,
            )

    def _commit_formal_terminal_archive_result_sync(
        self,
        assignment_id: str,
        result_json: str,
        manifest_digest: str,
    ) -> dict[str, Any]:
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None:
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            current = dict(row)
            if (
                str(current.get("assignment_mode")) != AssignmentMode.formal_experiment.value
                or str(current.get("phase"))
                not in {
                    BridgeAssignmentStep.cancelled.value,
                    BridgeAssignmentStep.error.value,
                }
                or current.get("terminal_events_drained_at") is None
                or int(current.get("relay_cursor") or 0) != int(current.get("ack_cursor") or 0)
            ):
                raise LocalLiliesBridgeConflict(
                    "formal terminal archive requires a sealed terminal assignment",
                    details=LocalLiliesBridge._safe_assignment_ids(current),
                )
            persisted_result = current.get("formal_terminal_archive_result_json")
            persisted_digest = current.get("formal_terminal_archive_manifest_digest")
            completed_at = current.get("formal_terminal_archive_completed_at")
            if (
                persisted_result is not None
                or persisted_digest is not None
                or completed_at is not None
            ):
                if (
                    str(persisted_result or "") != result_json
                    or not hmac.compare_digest(
                        str(persisted_digest or ""),
                        manifest_digest,
                    )
                    or completed_at is None
                ):
                    raise LocalLiliesBridgeConflict(
                        "formal terminal archive replay changed its result",
                        details=LocalLiliesBridge._safe_assignment_ids(current),
                    )
                return current
            conn.execute(
                """
                UPDATE local_lilies_assignments
                SET formal_terminal_archive_result_json=?,
                    formal_terminal_archive_manifest_digest=?,
                    formal_terminal_archive_completed_at=?,
                    updated_at=?
                WHERE assignment_id=?
                  AND formal_terminal_archive_completed_at IS NULL
                """,
                (
                    result_json,
                    manifest_digest,
                    now,
                    now,
                    assignment_id,
                ),
            )
            stored = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            return dict(stored)

    async def get_assignment_by_build(self, build_id: UUID | str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_assignment_by_sync, "build_id", str(build_id))

    async def get_assignment_by_session(self, session_id: UUID | str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_assignment_by_sync, "session_id", str(session_id))

    def _get_assignment_by_sync(self, column: str, value: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM local_lilies_assignments WHERE {column}=?", (value,)
            ).fetchone()
        if row is None:
            raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
        return dict(row)

    async def list_assignments_for_application(
        self, application_id: UUID | str
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_assignments_for_application_sync, str(application_id)
        )

    def _list_assignments_for_application_sync(self, application_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM local_lilies_assignments
                WHERE application_id=? ORDER BY created_at,assignment_id
                """,
                (application_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_recoverable_assignments(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_recoverable_assignments_sync)

    def _list_recoverable_assignments_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM local_lilies_assignments
                WHERE phase NOT IN ('completed','cancelled','error')
                   OR (desired_state='cancelled' AND phase!='cancelled')
                   OR (
                     desired_state='cancelled'
                     AND phase='cancelled'
                     AND terminal_events_drained_at IS NULL
                   )
                   OR (
                     assignment_mode='formal_experiment'
                     AND desired_state='active'
                     AND phase='completed'
                     AND submission_json IS NOT NULL
                     AND formal_archive_completed_at IS NULL
                   )
                   OR (
                     assignment_mode='formal_experiment'
                     AND desired_state='active'
                     AND phase='error'
                     AND formal_terminal_archive_completed_at IS NULL
                   )
                   OR (
                     assignment_mode='formal_experiment'
                     AND phase='cancelled'
                     AND formal_terminal_archive_completed_at IS NULL
                   )
                   OR (
                     assignment_mode='formal_experiment'
                     AND desired_state='cancelled'
                     AND submission_json IS NOT NULL
                     AND formal_channel_close_receipt_json IS NULL
                   )
                ORDER BY created_at,assignment_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    async def list_terminal_assignments(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_terminal_assignments_sync)

    def _list_terminal_assignments_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM local_lilies_assignments
                WHERE phase IN ('completed','cancelled','error')
                ORDER BY created_at,assignment_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    async def commit_relay_events(
        self,
        assignment_id: UUID | str,
        session_id: UUID | str,
        events: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int, int]:
        async with self._lock:
            return await asyncio.to_thread(
                self._commit_relay_events_sync,
                str(assignment_id),
                str(session_id),
                events,
            )

    def _commit_relay_events_sync(
        self,
        assignment_id: str,
        session_id: str,
        events: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], int, int]:
        inserted = 0
        replayed = 0
        now = _now().isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            assignment = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            if str(assignment["session_id"]) != session_id:
                raise LocalLiliesBridgeSecurityError(
                    "daemon relay escaped its reserved session binding",
                    details=LocalLiliesBridge._safe_assignment_ids(assignment),
                )
            active_relay = assignment["desired_state"] == BridgeDesiredState.active.value
            cancelled_terminal_drain = (
                assignment["desired_state"] == BridgeDesiredState.cancelled.value
                and assignment["phase"] == BridgeAssignmentStep.cancelled.value
                and assignment["terminal_events_drained_at"] is None
            )
            if not active_relay and not cancelled_terminal_drain:
                raise _AssignmentDesiredStateChanged(dict(assignment))
            cursor = int(assignment["relay_cursor"])
            for event in events:
                seq = int(event["seq"])
                event_type = str(event["event"])
                data = dict(event.get("data") or {})
                existing = conn.execute(
                    """
                    SELECT event_type,data_json FROM local_lilies_assignment_events
                    WHERE assignment_id=? AND daemon_seq=?
                    """,
                    (assignment_id, seq),
                ).fetchone()
                encoded = _canonical_json(data)
                if existing is not None:
                    if existing["event_type"] != event_type or existing["data_json"] != encoded:
                        raise LocalLiliesBridgeConflict(
                            "daemon event cursor was replayed with different content"
                        )
                    replayed += 1
                else:
                    conn.execute(
                        """
                        INSERT INTO local_lilies_assignment_events(
                          assignment_id,daemon_seq,session_id,event_type,data_json,received_at
                        ) VALUES(?,?,?,?,?,?)
                        """,
                        (assignment_id, seq, session_id, event_type, encoded, now),
                    )
                    inserted += 1
                cursor = max(cursor, seq)
            conn.execute(
                """
                UPDATE local_lilies_assignments
                SET relay_cursor=?,updated_at=? WHERE assignment_id=?
                """,
                (cursor, now, assignment_id),
            )
            row = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            return dict(row), inserted, replayed

    async def get_assignment_event_data(
        self,
        assignment_id: UUID | str,
        event_type: str,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._get_assignment_event_data_sync,
            str(assignment_id),
            event_type,
        )

    def _get_assignment_event_data_sync(
        self,
        assignment_id: str,
        event_type: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT data_json FROM local_lilies_assignment_events
                WHERE assignment_id=? AND event_type=? ORDER BY daemon_seq
                """,
                (assignment_id, event_type),
            ).fetchall()
        return [json.loads(str(row["data_json"])) for row in rows]

    async def list_events(
        self,
        assignment_id: UUID | str,
        *,
        after: int = 0,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_events_sync, str(assignment_id), max(0, after))

    def _list_events_sync(self, assignment_id: str, after: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM local_lilies_assignment_events
                WHERE assignment_id=? AND daemon_seq>? ORDER BY daemon_seq
                """,
                (assignment_id, after),
            ).fetchall()
        return [dict(row) for row in rows]

    async def export_assignment(
        self,
        assignment_id: UUID | str,
    ) -> dict[str, Any]:
        """Read one assignment and its complete relay log from one SQLite snapshot."""

        return await asyncio.to_thread(
            self._export_assignment_sync,
            str(assignment_id),
        )

    def _export_assignment_sync(self, assignment_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            assignment = conn.execute(
                "SELECT * FROM local_lilies_assignments WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if assignment is None:
                raise LocalLiliesBridgeNotFound("local Lilies assignment not found")
            events = conn.execute(
                """
                SELECT * FROM local_lilies_assignment_events
                WHERE assignment_id=? ORDER BY daemon_seq
                """,
                (assignment_id,),
            ).fetchall()
        assignment_value = dict(assignment)
        event_values = [dict(row) for row in events]
        seqs = [int(row["daemon_seq"]) for row in event_values]
        relay_cursor = int(assignment_value.get("relay_cursor") or 0)
        ack_cursor = int(assignment_value.get("ack_cursor") or 0)
        complete = (
            seqs == list(range(1, relay_cursor + 1))
            and ack_cursor == relay_cursor
            and assignment_value.get("terminal_events_drained_at") is not None
            and str(assignment_value.get("phase")) in {"completed", "cancelled", "error"}
        )
        return {
            "schema_version": "1.0",
            "complete": complete,
            "assignment": assignment_value,
            "events": event_values,
            "counts": {"events": len(event_values)},
            "watermark": {
                "min_daemon_seq": seqs[0] if seqs else None,
                "max_daemon_seq": seqs[-1] if seqs else None,
                "relay_cursor": relay_cursor,
                "ack_cursor": ack_cursor,
            },
        }


FaultHook = Callable[[str, Mapping[str, str]], Awaitable[None] | None]
ContractDigestProvider = Callable[
    [tuple[PlatformBlackboxScope, ...], tuple[UUID, ...]],
    Awaitable[str] | str,
]
FormalCredentialSecretProvider = Callable[
    [BuildAssignment, UUID],
    Awaitable[str | SecretStr] | str | SecretStr,
]
FormalChannelCloseProvider = Callable[
    [BuildAssignment, UUID],
    Awaitable[CollaborationChannel | Mapping[str, Any]] | CollaborationChannel | Mapping[str, Any],
]
FormalTerminalArchiveProvider = Callable[
    [UUID],
    Awaitable[Any] | Any,
]
FormalSuccessArchiveProvider = Callable[
    [UUID, FormalRunArchivePreparationRequest],
    Awaitable[Any] | Any,
]
FormalArchiveIntentValidator = Callable[
    [UUID, FormalRunArchivePreparationRequest],
    Awaitable[None] | None,
]
FormalVerificationClaimProvider = Callable[
    [
        UUID,
        str,
        FormalRunArchivePreparationRequest,
        FormalRunArchivePreparationResult,
    ],
    Awaitable[Any] | Any,
]
LocalLiliesDiscoveryProvider = Callable[[], Awaitable[dict[str, Any]]]


class LocalLiliesBridge:
    """Crash-resumable platform owner for local-Lilies assignments.

    The bridge keeps only secret references in its SQLite database.  Both the
    daemon bearer and the one-time platform task bearer are written through
    :class:`PlatformHarness` before they can cross the daemon boundary.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        store: LocalLiliesBridgeStore,
        workflow_storage: WorkflowStorage,
        harness: PlatformHarness,
        auth_store: PlatformBlackboxAuthStore,
        client: LocalLiliesHttpClient,
        platform_base_url: str,
        contract_digest_provider: ContractDigestProvider,
        formal_assignment_broker: FormalAssignmentBroker | None = None,
        formal_credential_secret_provider: FormalCredentialSecretProvider | None = None,
        formal_channel_close_provider: FormalChannelCloseProvider | None = None,
        formal_terminal_archive_provider: FormalTerminalArchiveProvider | None = None,
        formal_success_archive_provider: FormalSuccessArchiveProvider | None = None,
        formal_archive_intent_validator: FormalArchiveIntentValidator | None = None,
        formal_verification_claim_provider: FormalVerificationClaimProvider | None = None,
        fault_hook: FaultHook | None = None,
        default_deadline_seconds: int = 3_600,
        default_route: bool = False,
        discovery_provider: LocalLiliesDiscoveryProvider | None = None,
    ) -> None:
        self.enabled = enabled
        self.store = store
        self.workflow_storage = workflow_storage
        self.harness = harness
        self.auth_store = auth_store
        self.client = client
        self.platform_base_url = self._normalize_loopback_url(platform_base_url)
        self.contract_digest_provider = contract_digest_provider
        self.formal_assignment_broker = formal_assignment_broker
        self.formal_credential_secret_provider = formal_credential_secret_provider
        self.formal_channel_close_provider = formal_channel_close_provider
        self.formal_terminal_archive_provider = formal_terminal_archive_provider
        self.formal_success_archive_provider = formal_success_archive_provider
        self.formal_archive_intent_validator = formal_archive_intent_validator
        self.formal_verification_claim_provider = formal_verification_claim_provider
        self.fault_hook = fault_hook
        self.default_deadline_seconds = max(60, default_deadline_seconds)
        self.default_route = bool(default_route and enabled)
        self.discovery_provider = discovery_provider
        self._assignment_locks: dict[str, asyncio.Lock] = {}
        self._connection_recovery_lock = asyncio.Lock()

    async def initialize(self) -> dict[str, int]:
        initialized = await self.store.initialize()
        if self.enabled:
            await self._recover_connection_outboxes()
        return initialized

    def require_enabled(self) -> None:
        if not self.enabled:
            raise LocalLiliesFeatureDisabled(
                "local Lilies is disabled until deterministic and browser acceptance passes"
            )

    async def status(self) -> dict[str, Any]:
        self.require_enabled()
        await self._recover_connection_outboxes()
        result: dict[str, Any] = {
            "enabled": True,
            "default_route": self.default_route,
            "connections": [
                self._connection_projection(row).model_dump(mode="json")
                for row in await self.store.list_connections()
            ],
        }
        if self.discovery_provider is not None:
            result["discovery"] = await self.discovery_provider()
        return result

    async def discovery_status(self) -> dict[str, Any]:
        """Expose only a safe local candidate while the assignment route is disabled."""

        if self.enabled:
            return await self.status()
        result: dict[str, Any] = {
            "enabled": False,
            "default_route": False,
            "connections": [],
        }
        if self.discovery_provider is not None:
            result["discovery"] = await self.discovery_provider()
        return result

    async def pair_connection(self, request: PairLocalLiliesRequest) -> LocalLiliesConnection:
        self.require_enabled()
        base_url = self._normalize_loopback_url(request.base_url)
        request_digest = _digest(
            {
                "base_url": base_url,
                "expected_daemon_fingerprint": request.expected_daemon_fingerprint,
            }
        )
        connection_id = uuid5(
            NAMESPACE_URL,
            f"lilies:platform-connection:{base_url}:{request.idempotency_key}",
        )
        owner_id = self._connection_secret_owner(connection_id)
        secret_ref = f"secret://{owner_id}/daemon-access-token"
        row = await self.store.reserve_connection(
            connection_id=connection_id,
            request=request,
            base_url=base_url,
            request_digest=request_digest,
            secret_ref=secret_ref,
        )
        if row["status"] == BridgeConnectionStatus.connected.value:
            return self._connection_projection(row)
        prepared_client_id = self._paired_client_id(connection_id)
        if await self._secret_exists(secret_ref):
            prepared_token = await self._resolve_secret(secret_ref)
            if self._token_client_id(prepared_token) != prepared_client_id:
                raise LocalLiliesBridgeSecurityError(
                    "prepared daemon bearer escaped its deterministic client binding",
                    details={"connection_id": str(connection_id)},
                )
        else:
            prepared_token = self._new_daemon_token(prepared_client_id)
            await self._save_encrypted_secret(
                owner_id=owner_id,
                name="daemon-access-token",
                value=prepared_token,
                description="Crash-safe Local Lilies daemon bearer outbox",
            )
        await self._fault("pairing.secret_saved", {"connection_id": str(connection_id)})
        try:
            raw = await self.client.exchange_pairing(
                base_url,
                {
                    "pairing_code": request.pairing_code,
                    "client_name": "platform",
                    "requested_scopes": [scope.value for scope in REQUIRED_DAEMON_SCOPES],
                    "client_nonce": self._pairing_nonce(
                        connection_id, "pair", request.idempotency_key
                    ),
                    "requested_client_id": str(prepared_client_id),
                    "prepared_access_token": prepared_token,
                },
            )
            exchange = PairingExchangeResult.model_validate(raw)
        except LocalLiliesUnavailable as error:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_unavailable",
                error_message="local Lilies pairing failed",
            )
            raise LocalLiliesBridgeUnavailable(
                "local Lilies daemon is unavailable",
                details={"connection_id": str(connection_id), "status": "unavailable"},
            ) from error
        except LocalLiliesClientError as error:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_pairing_rejected",
                error_message="local Lilies rejected the pairing exchange",
            )
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies rejected the pairing exchange",
                details={"connection_id": str(connection_id)},
            ) from error
        except ValueError as error:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_pairing_protocol_error",
                error_message="local Lilies returned an invalid pairing receipt",
            )
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies returned an invalid pairing receipt",
                details={"connection_id": str(connection_id)},
            ) from error
        if exchange.client_id != prepared_client_id or exchange.access_token != prepared_token:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_pairing_receipt_mismatch",
                error_message="daemon pairing receipt changed the prepared client bearer",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon pairing receipt did not preserve the prepared client binding",
                details={"connection_id": str(connection_id)},
            )
        await self._fault("pairing.exchange_accepted", {"connection_id": str(connection_id)})
        if exchange.daemon_fingerprint != request.expected_daemon_fingerprint:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_fingerprint_mismatch",
                error_message="daemon fingerprint did not match the trusted local identity",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon fingerprint mismatch",
                details={"connection_id": str(connection_id)},
            )
        if set(exchange.granted_scopes) != set(REQUIRED_DAEMON_SCOPES):
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_scope_denied",
                error_message="daemon did not grant the required platform scopes",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon pairing did not grant the exact required scopes",
                details={"connection_id": str(connection_id)},
            )
        row = await self.store.complete_connection(
            connection_id,
            client_id=exchange.client_id,
            scopes=exchange.granted_scopes,
            expires_at=exchange.expires_at,
            fingerprint=exchange.daemon_fingerprint,
        )
        await self._fault("pairing.connection_committed", {"connection_id": str(connection_id)})
        return self._connection_projection(row)

    async def reconnect_connection(
        self,
        connection_id: UUID | str,
        request: ReconnectLocalLiliesRequest,
    ) -> LocalLiliesConnection:
        self.require_enabled()
        row = await self.store.get_connection(connection_id)
        digest = _digest({"pairing_rotation": str(row["daemon_fingerprint"])})
        operation_receipt = await self.store.reserve_connection_operation(
            connection_id,
            operation="reconnect",
            idempotency_key=request.idempotency_key,
            request_digest=digest,
        )
        owner_id = self._connection_secret_owner(UUID(str(connection_id)))
        rotation_name = self._rotation_secret_name(request.idempotency_key)
        rotation_ref = f"secret://{owner_id}/{rotation_name}"
        if operation_receipt.get("completed_at") is not None:
            result_json = operation_receipt.get("result_json")
            if not isinstance(result_json, str) or not result_json:
                raise LocalLiliesBridgeSecurityError(
                    "completed reconnect operation has no durable result",
                    details={"connection_id": str(connection_id)},
                )
            await self._delete_secret_ref(rotation_ref)
            return LocalLiliesConnection.model_validate_json(result_json)
        await self.store.set_connection_state(
            connection_id, status=BridgeConnectionStatus.reconnecting
        )
        if row.get("client_id") is None:
            raise LocalLiliesBridgeSecurityError(
                "paired connection has no client identity",
                details={"connection_id": str(connection_id)},
            )
        previous_token = await self._resolve_secret(str(row["access_token_secret_ref"]))
        expected_client_id = UUID(str(row["client_id"]))
        if await self._secret_exists(rotation_ref):
            prepared_token = await self._resolve_secret(rotation_ref)
        else:
            prepared_token = self._new_daemon_token(expected_client_id)
            await self._save_encrypted_secret(
                owner_id=owner_id,
                name=rotation_name,
                value=prepared_token,
                description="Crash-safe rotated Local Lilies daemon bearer outbox",
            )
        if self._token_client_id(prepared_token) != expected_client_id:
            raise LocalLiliesBridgeSecurityError(
                "rotated daemon bearer escaped its existing client binding",
                details={"connection_id": str(connection_id)},
            )
        await self._fault(
            "reconnect.prepared_token_saved",
            {"connection_id": str(connection_id)},
        )
        try:
            raw = await self.client.exchange_pairing(
                str(row["base_url"]),
                {
                    "pairing_code": request.pairing_code,
                    "client_name": "platform",
                    "requested_scopes": [scope.value for scope in REQUIRED_DAEMON_SCOPES],
                    "client_nonce": self._pairing_nonce(
                        UUID(str(connection_id)), "reconnect", request.idempotency_key
                    ),
                    "previous_client_id": str(row["client_id"]),
                    "previous_access_token": previous_token,
                    "requested_client_id": str(expected_client_id),
                    "prepared_access_token": prepared_token,
                },
            )
            exchange = PairingExchangeResult.model_validate(raw)
        except LocalLiliesUnavailable as error:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_unavailable",
                error_message="local Lilies reconnect failed",
            )
            raise LocalLiliesBridgeUnavailable(
                "local Lilies daemon is unavailable",
                details={"connection_id": str(connection_id), "status": "unavailable"},
            ) from error
        except LocalLiliesRemoteError as error:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_reconnect_rejected",
                error_message="local Lilies rejected the reconnect exchange",
            )
            if error.status_code in {401, 403}:
                raise LocalLiliesBridgeSecurityError(
                    "local Lilies rejected reconnect authentication",
                    details={"connection_id": str(connection_id)},
                ) from error
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies rejected the reconnect exchange",
                details={"connection_id": str(connection_id)},
            ) from error
        except LocalLiliesClientError as error:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_reconnect_protocol_error",
                error_message="local Lilies returned an invalid reconnect response",
            )
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies returned an invalid reconnect response",
                details={"connection_id": str(connection_id)},
            ) from error
        except ValueError as error:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_reconnect_protocol_error",
                error_message="local Lilies returned an invalid reconnect receipt",
            )
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies returned an invalid reconnect receipt",
                details={"connection_id": str(connection_id)},
            ) from error
        if exchange.access_token != prepared_token:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_reconnect_receipt_mismatch",
                error_message="daemon reconnect receipt changed the prepared bearer",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon reconnect receipt did not preserve the prepared bearer",
                details={"connection_id": str(connection_id)},
            )
        await self._fault("reconnect.exchange_accepted", {"connection_id": str(connection_id)})
        if exchange.daemon_fingerprint != row["daemon_fingerprint"]:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_fingerprint_mismatch",
                error_message="daemon fingerprint changed during reconnect",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon fingerprint changed during reconnect",
                details={"connection_id": str(connection_id)},
            )
        if row.get("client_id") is None or str(exchange.client_id) != str(row["client_id"]):
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_client_mismatch",
                error_message="daemon client identity changed during reconnect",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon client identity changed during reconnect",
                details={"connection_id": str(connection_id)},
            )
        if set(exchange.granted_scopes) != set(REQUIRED_DAEMON_SCOPES):
            await self._save_encrypted_secret(
                owner_id=owner_id,
                name="daemon-access-token",
                value=prepared_token,
                description="Rotated Local Lilies bearer retained for secure recovery",
            )
            await self.store.complete_connection(
                connection_id,
                client_id=exchange.client_id,
                scopes=exchange.granted_scopes,
                expires_at=exchange.expires_at,
                fingerprint=exchange.daemon_fingerprint,
            )
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_scope_denied",
                error_message="daemon did not grant the required platform scopes",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon reconnect did not grant the exact required scopes",
                details={"connection_id": str(connection_id)},
            )
        await self._save_encrypted_secret(
            owner_id=owner_id,
            name="daemon-access-token",
            value=prepared_token,
            description="Rotated Local Lilies daemon bearer",
        )
        await self._fault("reconnect.stable_token_saved", {"connection_id": str(connection_id)})
        row = await self.store.complete_connection(
            connection_id,
            client_id=exchange.client_id,
            scopes=exchange.granted_scopes,
            expires_at=exchange.expires_at,
            fingerprint=exchange.daemon_fingerprint,
        )
        await self._fault("reconnect.connection_committed", {"connection_id": str(connection_id)})
        result = self._connection_projection(row)
        await self.store.complete_connection_operation(
            connection_id,
            operation="reconnect",
            idempotency_key=request.idempotency_key,
            result=result,
        )
        await self._delete_secret_ref(rotation_ref)
        return result

    async def refresh_connection(self, connection_id: UUID | str) -> LocalLiliesConnection:
        self.require_enabled()
        row = await self.store.get_connection(connection_id)
        token = await self._resolve_secret(str(row["access_token_secret_ref"]))
        try:
            status = DaemonStatus.model_validate(
                await self.client.status(str(row["base_url"]), token)
            )
        except LocalLiliesClientError as error:
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_unavailable",
                error_message="local Lilies status check failed",
            )
            raise LocalLiliesBridgeUnavailable(
                "local Lilies daemon is unavailable",
                details={
                    "connection_id": str(connection_id),
                    "status": row["status"],
                },
            ) from error
        except ValueError as error:
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_status_protocol_error",
                error_message="local Lilies returned an invalid status receipt",
            )
            raise LocalLiliesBridgeSecurityError(
                "local Lilies returned an invalid authenticated status receipt",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            ) from error
        if status.daemon_fingerprint != row["daemon_fingerprint"]:
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_fingerprint_mismatch",
                error_message="daemon identity no longer matches the paired connection",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon identity no longer matches the paired connection",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            )
        if row.get("client_id") is None or status.client_id != UUID(str(row["client_id"])):
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_client_mismatch",
                error_message="daemon bearer no longer matches the paired client",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon bearer no longer matches the paired client",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            )
        if set(status.client_scopes) != set(REQUIRED_DAEMON_SCOPES):
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_scope_mismatch",
                error_message="daemon bearer no longer has the exact platform scopes",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon bearer no longer has the exact platform scopes",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            )
        persisted_expiry = _parse_time(row.get("expires_at"))
        if status.client_expires_at is not None and status.client_expires_at <= _now():
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.expired,
                error_code="daemon_bearer_expired",
                error_message="daemon bearer has expired",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon bearer has expired",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            )
        if status.client_expires_at != persisted_expiry:
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_bearer_expiry_mismatch",
                error_message="daemon bearer expiry changed outside pairing",
            )
            raise LocalLiliesBridgeSecurityError(
                "daemon bearer expiry changed outside pairing",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            )
        row = await self.store.set_connection_state(
            connection_id,
            status=BridgeConnectionStatus.connected,
            seen=True,
        )
        return self._connection_projection(row)

    async def usage(
        self,
        connection_id: UUID | str,
        *,
        group_by: tuple[Literal["session", "stage", "model"], ...] = (
            "session",
            "stage",
            "model",
        ),
        page: int = 1,
        page_size: int = 100,
    ) -> LocalLiliesUsagePage:
        """Read standalone usage through the authenticated public HTTP contract.

        The platform deliberately never opens the sibling daemon's SQLite
        database or bootstrap credential.  Only the encrypted bearer created
        by an explicit pairing exchange crosses this adapter.
        """

        self.require_enabled()
        if (
            not group_by
            or len(group_by) > 3
            or len(set(group_by)) != len(group_by)
            or any(value not in {"session", "stage", "model"} for value in group_by)
            or isinstance(page, bool)
            or not 1 <= page <= 1_000
            or isinstance(page_size, bool)
            or not 1 <= page_size <= 100
        ):
            raise LocalLiliesBridgeSecurityError("local Lilies usage query is outside safe bounds")
        canonical_group_by = tuple(
            dimension
            for dimension in ("session", "stage", "model")
            if dimension in group_by
        )
        row = await self.store.get_connection(connection_id)
        if row["status"] != BridgeConnectionStatus.connected.value:
            raise LocalLiliesBridgeUnavailable(
                "local Lilies connection is not available",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            )
        token = await self._connection_token(row)
        try:
            payload = await self.client.usage(
                str(row["base_url"]),
                token,
                group_by=canonical_group_by,
                page=page,
                page_size=page_size,
            )
        except LocalLiliesUnavailable as error:
            await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_unavailable",
                error_message="local Lilies usage endpoint is unavailable",
            )
            raise LocalLiliesBridgeUnavailable(
                "local Lilies usage endpoint is unavailable",
                details={
                    "connection_id": str(connection_id),
                    "status": BridgeConnectionStatus.unavailable.value,
                },
            ) from error
        except LocalLiliesRemoteError as error:
            authentication_rejected = error.status_code in {401, 403}
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code=(
                    "daemon_usage_authentication_rejected"
                    if authentication_rejected
                    else "daemon_usage_rejected"
                ),
                error_message=(
                    "local Lilies rejected usage authentication"
                    if authentication_rejected
                    else "local Lilies rejected the usage request"
                ),
            )
            details = {
                "connection_id": str(connection_id),
                "status": str(row["status"]),
            }
            if authentication_rejected:
                raise LocalLiliesBridgeSecurityError(
                    "local Lilies rejected usage authentication",
                    details=details,
                ) from error
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies rejected the usage request",
                details=details,
            ) from error
        except LocalLiliesClientError as error:
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_usage_protocol_error",
                error_message="local Lilies returned an invalid usage response",
            )
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies returned an invalid usage response",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            ) from error
        try:
            result = LocalLiliesUsagePage.model_validate_json(
                json.dumps(payload, allow_nan=False, separators=(",", ":")),
                strict=True,
            )
        except (TypeError, ValueError) as error:
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_usage_receipt_invalid",
                error_message="local Lilies returned an invalid usage receipt",
            )
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies returned an invalid authenticated usage receipt",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            ) from error
        if (
            result.group_by != list(canonical_group_by)
            or result.page != page
            or result.page_size != page_size
        ):
            row = await self.store.set_connection_state(
                connection_id,
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_usage_receipt_mismatch",
                error_message="local Lilies usage receipt did not match the request",
            )
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies usage receipt does not match the requested page",
                details={
                    "connection_id": str(connection_id),
                    "status": str(row["status"]),
                },
            )
        await self.store.set_connection_state(
            connection_id,
            status=BridgeConnectionStatus.connected,
            seen=True,
        )
        return result

    async def get_connection(self, connection_id: UUID | str) -> LocalLiliesConnection:
        self.require_enabled()
        await self._recover_connection_outboxes(connection_id=connection_id)
        return self._connection_projection(await self.store.get_connection(connection_id))

    async def list_connections(self) -> list[LocalLiliesConnection]:
        self.require_enabled()
        await self._recover_connection_outboxes()
        return [self._connection_projection(row) for row in await self.store.list_connections()]

    async def start_build(
        self,
        application_id: UUID | str,
        request: StartLocalLiliesBuildRequest,
    ) -> LocalLiliesAssignment:
        self.require_enabled()
        application_uuid = UUID(str(application_id))
        connection = await self.store.get_connection(request.connection_id)
        if connection["status"] != BridgeConnectionStatus.connected.value:
            raise LocalLiliesBridgeUnavailable(
                "local Lilies connection is not available",
                details={
                    "application_id": str(application_uuid),
                    "connection_id": str(request.connection_id),
                    "status": str(connection["status"]),
                },
            )
        if connection["client_id"] is None:
            raise LocalLiliesBridgeSecurityError("paired connection has no client identity")
        effective_constraints = self._effective_constraints(request.constraints)

        stable = f"{application_uuid}:{request.idempotency_key}"
        assignment_id = uuid5(NAMESPACE_URL, f"lilies:assignment:{stable}")
        build_id = uuid5(NAMESPACE_URL, f"lilies:build:{stable}")
        session_key = self._session_idempotency_key(assignment_id)
        session_id = uuid5(
            NAMESPACE_URL,
            f"lilies:session:{connection['client_id']}:{session_key}",
        )
        request_payload = request.model_dump(mode="json", exclude_none=True)
        request_digest = _digest(
            {"application_id": str(application_uuid), "request": request_payload}
        )
        owner_id = self._assignment_secret_owner(assignment_id)
        row, replayed = await self.store.reserve_assignment(
            assignment_id=assignment_id,
            application_id=application_uuid,
            build_id=build_id,
            session_id=session_id,
            request=request,
            request_digest=request_digest,
            request_json=_canonical_json(request_payload),
            task_token_secret_ref=f"secret://{owner_id}/platform-task-token",
        )
        await self._fault("assignment.reserved", self._safe_assignment_ids(row))
        if not replayed:
            try:
                await self._validate_empty_application(str(application_uuid))
                await self.workflow_storage.create_build(
                    str(build_id),
                    str(application_uuid),
                    request.requirement,
                    request.auto_publish,
                    effective_constraints.max_turns,
                    4,
                    max_elapsed_seconds=self._remaining_seconds(effective_constraints),
                    planning_mode="disabled",
                )
            except Exception as error:
                await self.store.update_assignment(
                    assignment_id,
                    phase=BridgeAssignmentStep.error,
                    status="error",
                    last_error_code="application_not_empty",
                    last_error_message="local Lilies requires a newly created empty application",
                )
                raise LocalLiliesBridgeConflict(
                    "local Lilies can only start from an empty application",
                    details={
                        "assignment_id": str(assignment_id),
                        "application_id": str(application_uuid),
                        "build_id": str(build_id),
                        "session_id": str(session_id),
                    },
                ) from error
        else:
            # A retry of an already accepted start request is a receipt replay,
            # not implicit authority to resume a model error or a human wait.
            # Only pre-submission crash windows continue automatically here;
            # established sessions require the explicit resume endpoint.
            if row["phase"] in {
                BridgeAssignmentStep.submitted.value,
                BridgeAssignmentStep.running.value,
                BridgeAssignmentStep.interrupted.value,
                BridgeAssignmentStep.completed.value,
                BridgeAssignmentStep.cancelled.value,
                BridgeAssignmentStep.error.value,
            }:
                await self._ensure_build_record(row)
                return self._assignment_projection(row)
        return await self.resume_assignment(assignment_id)

    async def start_formal_build(
        self,
        application_id: UUID | str,
        request: StartFormalLocalLiliesBuildRequest,
    ) -> LocalLiliesAssignment:
        """Start only the sealed revision selected by a narrow formal request."""

        self.require_enabled()
        if self.formal_assignment_broker is None:
            raise LocalLiliesBridgeSecurityError("formal task-package assignment is not configured")
        if self.formal_credential_secret_provider is None:
            raise LocalLiliesBridgeSecurityError(
                "formal collaboration credential provisioning is not configured"
            )
        if self.formal_channel_close_provider is None:
            raise LocalLiliesBridgeSecurityError(
                "formal collaboration authority closure is not configured"
            )
        application_uuid = UUID(str(application_id))
        connection = await self.store.get_connection(request.connection_id)
        if connection["status"] != BridgeConnectionStatus.connected.value:
            raise LocalLiliesBridgeUnavailable(
                "local Lilies connection is not available",
                details={
                    "application_id": str(application_uuid),
                    "connection_id": str(request.connection_id),
                    "status": str(connection["status"]),
                },
            )
        if connection["client_id"] is None:
            raise LocalLiliesBridgeSecurityError("paired connection has no client identity")

        stable = (
            f"{application_uuid}:{request.task_id}:{request.revision}:{request.idempotency_key}"
        )
        assignment_id = uuid5(NAMESPACE_URL, f"lilies:formal-assignment:{stable}")
        build_id = uuid5(NAMESPACE_URL, f"lilies:formal-build:{stable}")
        session_key = self._session_idempotency_key(assignment_id)
        session_id = uuid5(
            NAMESPACE_URL,
            f"lilies:session:{connection['client_id']}:{session_key}",
        )
        request_payload = request.model_dump(mode="json")
        request_digest = _digest(
            {"application_id": str(application_uuid), "request": request_payload}
        )
        owner_id = self._assignment_secret_owner(assignment_id)
        row, replayed = await self.store.reserve_assignment(
            assignment_id=assignment_id,
            application_id=application_uuid,
            build_id=build_id,
            session_id=session_id,
            request=request,
            request_digest=request_digest,
            request_json=_canonical_json(request_payload),
            task_token_secret_ref=f"secret://{owner_id}/platform-task-token",
            assignment_mode=AssignmentMode.formal_experiment,
        )
        if row.get("assignment_mode") != AssignmentMode.formal_experiment.value:
            raise LocalLiliesBridgeSecurityError(
                "formal start replay resolved to a customer assignment",
                details=self._safe_assignment_ids(row),
            )
        await self.workflow_storage.begin_formal_draft_provenance(
            assignment_id=str(assignment_id),
            session_id=str(session_id),
            application_id=str(application_uuid),
        )
        await self._fault("formal.assignment.reserved", self._safe_assignment_ids(row))
        if not replayed:
            try:
                await self._validate_empty_application(str(application_uuid))
            except Exception as error:
                await self._seal_formal_pre_submission_rejection(
                    row,
                    error_code="application_not_empty",
                    error_message=(
                        "formal local Lilies requires a newly created empty application"
                    ),
                )
                raise LocalLiliesBridgeConflict(
                    "formal local Lilies can only start from an empty application",
                    details=self._safe_assignment_ids(row),
                ) from error
            row, _ = await self._prepare_formal_assignment(row)
            await self._ensure_build_record(row)
        elif row["phase"] in {
            BridgeAssignmentStep.submitted.value,
            BridgeAssignmentStep.running.value,
            BridgeAssignmentStep.interrupted.value,
            BridgeAssignmentStep.completed.value,
            BridgeAssignmentStep.cancelled.value,
            BridgeAssignmentStep.error.value,
        }:
            await self._ensure_build_record(row)
            return self._assignment_projection(row)
        return await self.resume_assignment(assignment_id)

    async def resume_assignment(self, assignment_id: UUID | str) -> LocalLiliesAssignment:
        async with self._assignment_lock(assignment_id):
            try:
                return await self._resume_assignment_locked(assignment_id)
            except _AssignmentCancellationWon as outcome:
                return outcome.assignment

    async def _resume_assignment_locked(self, assignment_id: UUID | str) -> LocalLiliesAssignment:
        self.require_enabled()
        row = await self.store.get_assignment(assignment_id)
        if row["desired_state"] == BridgeDesiredState.cancelled.value:
            return await self._cancel_assignment_locked(
                assignment_id,
                idempotency_key=f"cancel.resume.{str(assignment_id).replace('-', '')}",
                reason="persisted cancellation request",
            )
        formal = self._is_formal_assignment(row)
        if row["phase"] == BridgeAssignmentStep.completed.value:
            row, _, _, _ = await self._drain_completed_terminal_events_locked(row)
            row = await self._archive_formal_success(row)
            return self._assignment_projection(row)
        if row["phase"] == BridgeAssignmentStep.cancelled.value:
            return self._assignment_projection(row)
        if (
            formal
            and row["phase"] == BridgeAssignmentStep.error.value
        ):
            row, _, _, _ = await self._drain_error_terminal_events_locked(row)
            await self._archive_formal_terminal(row)
            return self._assignment_projection(row)
        prepared: PreparedFormalAssignment | None = None
        if formal and row["phase"] in {
            BridgeAssignmentStep.recorded.value,
            BridgeAssignmentStep.session_created.value,
            BridgeAssignmentStep.credential_issuing.value,
            BridgeAssignmentStep.credential_issued.value,
            BridgeAssignmentStep.credential_provisioned.value,
            BridgeAssignmentStep.collaboration_provisioning.value,
            BridgeAssignmentStep.collaboration_provisioned.value,
            BridgeAssignmentStep.workspace_staging.value,
            BridgeAssignmentStep.workspace_staged.value,
            BridgeAssignmentStep.submitting.value,
        }:
            row, prepared = await self._prepare_formal_assignment(row)
        row = await self._recheck_assignment_active(row, checkpoint="assignment resume")
        if row["phase"] in {
            BridgeAssignmentStep.recorded.value,
            BridgeAssignmentStep.session_created.value,
            BridgeAssignmentStep.credential_issuing.value,
            BridgeAssignmentStep.credential_issued.value,
            BridgeAssignmentStep.credential_provisioned.value,
            BridgeAssignmentStep.collaboration_provisioning.value,
            BridgeAssignmentStep.collaboration_provisioned.value,
            BridgeAssignmentStep.workspace_staging.value,
            BridgeAssignmentStep.workspace_staged.value,
        }:
            await self._validate_reserved_empty_application(row)
        await self._ensure_build_record(row)
        connection = await self.store.get_connection(row["connection_id"])
        token = await self._connection_token(connection, assignment=row)

        if row["phase"] == BridgeAssignmentStep.recorded.value:
            row = await self._recheck_assignment_active(row, checkpoint="daemon session creation")
            row = await self._update_active_assignment(
                row,
                checkpoint="daemon session creation intent",
                daemon_session_creation_started_at=(
                    row.get("daemon_session_creation_started_at")
                    or _now().isoformat()
                ),
            )
            await self._fault(
                "session.creation_started",
                self._safe_assignment_ids(row),
            )
            try:
                raw = await self.client.create_session(
                    str(connection["base_url"]),
                    token,
                    {
                        "schema_version": "1.0",
                        "idempotency_key": self._session_idempotency_key(
                            UUID(row["assignment_id"])
                        ),
                        "kind": "platform",
                        "title": f"Lilies build {row['application_id']}",
                    },
                )
                session = self._validate_session_receipt(
                    raw,
                    row=row,
                    require_assignment_binding=False,
                )
            except LocalLiliesClientError as error:
                await self._recheck_assignment_active(
                    row, checkpoint="daemon session creation failure"
                )
                await self._raise_assignment_unavailable(row, error)
            row = await self._recheck_assignment_active(
                row, checkpoint="daemon session creation commit"
            )
            await self._fault("session.created", self._safe_assignment_ids(row))
            row = await self._update_active_assignment(
                row,
                checkpoint="session-created state commit",
                phase=BridgeAssignmentStep.session_created,
                status="provisioning",
                daemon_status=session.status,
                last_error_code=None,
                last_error_message=None,
            )

        if row["phase"] in {
            BridgeAssignmentStep.session_created.value,
            BridgeAssignmentStep.credential_issuing.value,
        }:
            row = await self._ensure_task_credential(
                row,
                formal_assignment=(prepared.assignment if prepared is not None else None),
            )

        if row["phase"] == BridgeAssignmentStep.credential_issued.value:
            row = await self._provision_task_credential(
                row,
                connection,
                token,
                formal_assignment=(prepared.assignment if prepared is not None else None),
            )

        if row["phase"] == BridgeAssignmentStep.credential_provisioned.value:
            if formal:
                if prepared is None:  # pragma: no cover - guarded above
                    raise LocalLiliesBridgeSecurityError(
                        "formal assignment preparation is unavailable"
                    )
                row = await self._provision_collaboration_credential(
                    row,
                    connection,
                    token,
                    prepared.assignment,
                )
            else:
                row = await self._submit_assignment(row, connection, token)

        if formal and row["phase"] in {
            BridgeAssignmentStep.collaboration_provisioning.value,
            BridgeAssignmentStep.collaboration_provisioned.value,
        }:
            if prepared is None:  # pragma: no cover - guarded above
                raise LocalLiliesBridgeSecurityError("formal assignment preparation is unavailable")
            if row["phase"] == BridgeAssignmentStep.collaboration_provisioning.value:
                row = await self._provision_collaboration_credential(
                    row,
                    connection,
                    token,
                    prepared.assignment,
                )
            if row["phase"] == BridgeAssignmentStep.collaboration_provisioned.value:
                row = await self._stage_formal_workspace(
                    row,
                    connection,
                    token,
                    prepared,
                )

        if formal and row["phase"] == BridgeAssignmentStep.workspace_staging.value:
            if prepared is None:  # pragma: no cover - guarded above
                raise LocalLiliesBridgeSecurityError("formal assignment preparation is unavailable")
            row = await self._stage_formal_workspace(
                row,
                connection,
                token,
                prepared,
            )

        if row["phase"] in {
            BridgeAssignmentStep.workspace_staged.value,
            BridgeAssignmentStep.submitting.value,
        }:
            row = await self._submit_assignment(row, connection, token)

        if row["phase"] in {
            BridgeAssignmentStep.submitted.value,
            BridgeAssignmentStep.running.value,
            BridgeAssignmentStep.interrupted.value,
            BridgeAssignmentStep.error.value,
        }:
            row = await self._recheck_assignment_active(
                row, checkpoint="daemon session status read"
            )
            try:
                session = self._validate_session_receipt(
                    await self.client.get_session(
                        str(connection["base_url"]), token, row["session_id"]
                    ),
                    row=row,
                    require_assignment_binding=True,
                )
            except LocalLiliesClientError as error:
                await self._recheck_assignment_active(
                    row, checkpoint="daemon session status failure"
                )
                await self._raise_assignment_unavailable(row, error)
            row = await self._recheck_assignment_active(
                row, checkpoint="daemon session status projection"
            )
            if session.status == SessionStatus.interrupted or (
                session.status == SessionStatus.error and not formal
            ):
                row = await self._recheck_assignment_active(row, checkpoint="daemon session resume")
                try:
                    self._validate_session_operation_receipt(
                        await self.client.resume_session(
                            str(connection["base_url"]),
                            token,
                            row["session_id"],
                            {
                                "idempotency_key": self._resume_key(row, session),
                                "expected_status": session.status.value,
                                "reason": "platform requested explicit assignment resume",
                            },
                        ),
                        row=row,
                        operation="resume",
                    )
                    row = await self._recheck_assignment_active(
                        row, checkpoint="daemon session resume commit"
                    )
                    session = self._validate_session_receipt(
                        await self.client.get_session(
                            str(connection["base_url"]), token, row["session_id"]
                        ),
                        row=row,
                        require_assignment_binding=True,
                    )
                except LocalLiliesClientError as error:
                    await self._recheck_assignment_active(
                        row, checkpoint="daemon session resume failure"
                    )
                    await self._raise_assignment_unavailable(row, error)
            row = await self._recheck_assignment_active(
                row, checkpoint="daemon session state commit"
            )
            phase, status = self._phase_for_daemon_status(session.status)
            row = await self._update_active_assignment(
                row,
                checkpoint="assignment state commit",
                phase=phase,
                status=status,
                daemon_status=session.status,
                last_error_code=(
                    "daemon_session_error" if session.status is SessionStatus.error else None
                ),
                last_error_message=(
                    "local Lilies session ended in error"
                    if session.status is SessionStatus.error
                    else None
                ),
            )
            await self._fault("resume.assignment_state_committed", self._safe_assignment_ids(row))
            if phase is BridgeAssignmentStep.completed:
                row, _, _, _ = await self._drain_completed_terminal_events_locked(row)
                row = await self._archive_formal_success(row)
                phase = BridgeAssignmentStep(str(row["phase"]))
                status = str(row["status"])
            elif phase is BridgeAssignmentStep.error and formal:
                row, _, _, _ = await self._drain_error_terminal_events_locked(row)
            await self.workflow_storage.update_build(
                row["build_id"],
                status=self._build_status(status, formal=formal),
            )
            if phase is BridgeAssignmentStep.error:
                await self._archive_formal_terminal(row)
            row = await self._recheck_assignment_active(
                row, checkpoint="workflow build state commit"
            )
        row = await self._recheck_assignment_active(row, checkpoint="connection state refresh")
        await self.store.set_connection_state(
            row["connection_id"],
            status=BridgeConnectionStatus.connected,
            seen=True,
        )
        return self._assignment_projection(row)

    async def cancel_assignment(
        self,
        assignment_id: UUID | str,
        *,
        idempotency_key: str,
        reason: str = "requested_by_user",
    ) -> LocalLiliesAssignment:
        async with self._assignment_lock(assignment_id):
            return await self._cancel_assignment_locked(
                assignment_id,
                idempotency_key=idempotency_key,
                reason=reason,
            )

    async def resolve_assignment_permission(
        self,
        assignment_id: UUID | str,
        request_id: UUID | str,
        request: PermissionDecisionRequest,
    ) -> dict[str, Any]:
        """Resolve one daemon permission without mixing it with capability approval."""

        async with self._assignment_lock(assignment_id):
            self.require_enabled()
            row = await self.store.get_assignment(assignment_id)
            row = await self._recheck_assignment_active(
                row,
                checkpoint="permission decision",
            )
            connection = await self.store.get_connection(row["connection_id"])
            token = await self._connection_token(connection, assignment=row)
            try:
                raw = await self.client.resolve_permission(
                    str(connection["base_url"]),
                    token,
                    row["session_id"],
                    str(request_id),
                    request.model_dump(mode="json", exclude_none=True),
                )
                decision = PermissionDecisionResult.model_validate(raw)
            except LocalLiliesClientError as error:
                await self._raise_assignment_unavailable(row, error)
                raise AssertionError("unreachable permission error projection")
            except ValueError as error:
                raise LocalLiliesBridgeSecurityError(
                    "daemon returned an invalid permission decision receipt",
                    details=self._safe_assignment_ids(row),
                ) from error
            if decision.request_id != UUID(str(request_id)):
                raise LocalLiliesBridgeSecurityError(
                    "daemon permission receipt belongs to another request",
                    details=self._safe_assignment_ids(row),
                )
            await self.store.set_connection_state(
                row["connection_id"],
                status=BridgeConnectionStatus.connected,
                seen=True,
            )
            relay = await self._relay_events_locked(assignment_id, max_events=100)
            return {
                "permission": decision.model_dump(mode="json", exclude_none=True),
                "assignment": relay.assignment.model_dump(
                    mode="json",
                    exclude_none=True,
                ),
            }

    async def _cancel_assignment_locked(
        self,
        assignment_id: UUID | str,
        *,
        idempotency_key: str,
        reason: str,
    ) -> LocalLiliesAssignment:
        self.require_enabled()
        if len(idempotency_key) < 16:
            raise ValueError("cancel idempotency_key must contain at least 16 characters")
        row = await self.store.get_assignment(assignment_id)
        if row["phase"] == BridgeAssignmentStep.cancelled.value:
            row = await self._close_formal_collaboration_authority(row)
            if row["status"] != "cancelled" or (
                row["desired_state"] != BridgeDesiredState.cancelled.value
            ):
                row = await self.store.update_assignment(
                    assignment_id,
                    status="cancelled",
                    desired_state=BridgeDesiredState.cancelled,
                    daemon_status=SessionStatus.cancelled,
                    last_error_code=None,
                    last_error_message=None,
                )
            row, _, _, drain_authenticated = await self._drain_cancelled_terminal_events_locked(row)
            await self._archive_formal_terminal(row)
            try:
                await self.workflow_storage.update_build(row["build_id"], status="cancelled")
            except KeyError:
                pass
            if drain_authenticated:
                await self.store.set_connection_state(
                    row["connection_id"],
                    status=BridgeConnectionStatus.connected,
                    seen=True,
                )
            return self._assignment_projection(row)
        if row["phase"] == BridgeAssignmentStep.completed.value:
            raise LocalLiliesBridgeConflict(
                "completed local Lilies assignments cannot be cancelled",
                details=self._safe_assignment_ids(row),
            )
        row = await self.store.update_assignment(
            assignment_id,
            desired_state=BridgeDesiredState.cancelled,
            status="cancelling",
            last_error_code=None,
            last_error_message=None,
        )
        connection = await self.store.get_connection(row["connection_id"])
        daemon_error: LocalLiliesClientError | LocalLiliesBridgeError | None = None
        daemon_session_status: SessionStatus | None = None
        daemon_terminal_winner: SessionResult | None = None
        try:
            token = await self._connection_token(connection, assignment=row)
            try:
                cancel_receipt = self._validate_session_operation_receipt(
                    await self.client.cancel_session(
                        str(connection["base_url"]),
                        token,
                        row["session_id"],
                        {
                            "idempotency_key": idempotency_key,
                            "reason": reason[:1_000],
                        },
                    ),
                    row=row,
                    operation="cancellation",
                )
            except LocalLiliesRemoteError as error:
                if error.status_code == 409:
                    # Cancellation can lose a race to an immutable daemon
                    # terminal state after the platform persisted its intent.
                    # A 409 alone is not proof: bind a fresh session receipt to
                    # both reserved IDs before changing the platform projection.
                    terminal = self._validate_session_receipt(
                        await self.client.get_session(
                            str(connection["base_url"]), token, row["session_id"]
                        ),
                        row=row,
                        require_assignment_binding=True,
                    )
                    if terminal.status not in {
                        SessionStatus.cancelled,
                        SessionStatus.completed,
                        SessionStatus.closed,
                    }:
                        raise error
                    daemon_terminal_winner = terminal
                    daemon_session_status = terminal.status
                # The deterministic session ID may never have reached the daemon,
                # or create_session may have committed immediately before the
                # platform crashed.  A daemon 404 proves the former; every other
                # response must still be treated as an uncertain cleanup.
                elif error.status_code != 404:
                    raise
            else:
                if cancel_receipt.status != SessionStatus.cancelled:
                    raise LocalLiliesBridgeSecurityError(
                        "daemon cancellation receipt did not confirm this session",
                        details=self._safe_assignment_ids(row),
                    )
                daemon_session_status = SessionStatus.cancelled
            credential_targets: list[tuple[str, str]] = []
            credential_ref = row.get("credential_ref")
            if credential_ref:
                credential_targets.append(("platform", str(credential_ref)))
            if self._is_formal_assignment(row) and row.get("submission_json"):
                formal_assignment = self._persisted_formal_assignment(row)
                collaboration = formal_assignment.collaboration
                collaboration_ref = row.get("collaboration_credential_ref") or (
                    collaboration.credential_ref if collaboration is not None else None
                )
                if collaboration_ref:
                    credential_targets.append(("collaboration", str(collaboration_ref)))
            if daemon_terminal_winner is None or daemon_terminal_winner.status in {
                SessionStatus.cancelled,
                SessionStatus.closed,
            }:
                for credential_kind, target_ref in credential_targets:
                    suffix = "" if credential_kind == "platform" else f".{credential_kind}"
                    try:
                        raw_revoke_receipt = await self.client.revoke_credential(
                            str(connection["base_url"]),
                            token,
                            {
                                "idempotency_key": (
                                    "credential.revoke."
                                    f"{row['assignment_id'].replace('-', '')}"
                                    f"{suffix}"
                                ),
                                "credential_ref": target_ref,
                                "reason": DAEMON_CREDENTIAL_REVOCATION_REASON,
                            },
                        )
                    except LocalLiliesRemoteError as error:
                        platform_not_yet_provisioned = credential_kind == "platform" and row[
                            "phase"
                        ] in {
                            BridgeAssignmentStep.recorded.value,
                            BridgeAssignmentStep.error.value,
                            BridgeAssignmentStep.credential_issuing.value,
                            BridgeAssignmentStep.credential_issued.value,
                        }
                        collaboration_not_yet_provisioned = (
                            credential_kind == "collaboration"
                            and row["phase"]
                            in {
                                BridgeAssignmentStep.recorded.value,
                                BridgeAssignmentStep.error.value,
                                BridgeAssignmentStep.session_created.value,
                                BridgeAssignmentStep.credential_issuing.value,
                                BridgeAssignmentStep.credential_issued.value,
                                BridgeAssignmentStep.credential_provisioned.value,
                            }
                        )
                        if error.status_code != 404 or not (
                            platform_not_yet_provisioned or collaboration_not_yet_provisioned
                        ):
                            raise
                    else:
                        try:
                            revoke_receipt = CredentialRevokeResult.model_validate(
                                raw_revoke_receipt
                            )
                        except ValueError as error:
                            raise LocalLiliesBridgeSecurityError(
                                "daemon returned an invalid credential revocation receipt",
                                details=self._safe_assignment_ids(row),
                            ) from error
                        if (
                            str(revoke_receipt.credential_ref) != target_ref
                            or not revoke_receipt.revoked
                        ):
                            raise LocalLiliesBridgeSecurityError(
                                "daemon did not confirm assignment credential revocation",
                                details=self._safe_assignment_ids(row),
                            )
        except (LocalLiliesClientError, LocalLiliesBridgeError) as error:
            daemon_error = error
            cleanup_error_code = (
                "daemon_cancel_security"
                if isinstance(error, LocalLiliesBridgeSecurityError)
                else "daemon_cancel_pending"
            )
            await self.store.set_connection_state(
                row["connection_id"],
                status=BridgeConnectionStatus.unavailable,
                error_code=cleanup_error_code,
                error_message="daemon cancellation cleanup is pending",
            )
            row = await self.store.update_assignment(
                assignment_id,
                status="unavailable",
                last_error_code=cleanup_error_code,
                last_error_message="daemon cancellation cleanup is pending",
            )
        if (
            daemon_error is None
            and daemon_terminal_winner is not None
            and daemon_terminal_winner.status == SessionStatus.completed
        ):
            # Completion is immutable and therefore outranks the later cancel
            # intent.  Restore the authoritative desired state before exposing
            # the stable conflict so restart recovery cannot retry cancellation
            # and overwrite a successful build.
            row = await self.store.update_assignment(
                assignment_id,
                desired_state=BridgeDesiredState.active,
                phase=BridgeAssignmentStep.completed,
                status="completed",
                daemon_status=SessionStatus.completed,
                last_error_code=None,
                last_error_message=None,
            )
            row, _, _, _ = await self._drain_completed_terminal_events_locked(row)
            row = await self._archive_formal_success(row)
            await self.workflow_storage.update_build(
                row["build_id"],
                status=self._build_status(
                    str(row["status"]),
                    formal=self._is_formal_assignment(row),
                ),
            )
            await self.store.set_connection_state(
                row["connection_id"],
                status=BridgeConnectionStatus.connected,
                seen=True,
            )
            raise LocalLiliesBridgeConflict(
                "completed local Lilies assignments cannot be cancelled",
                details=self._safe_assignment_ids(row),
            )
        platform_credential_ref = row.get("credential_ref")
        if platform_credential_ref is None and row["phase"] != BridgeAssignmentStep.recorded.value:
            platform_credential_ref = self._task_credential_ref(UUID(str(row["assignment_id"])))
        if platform_credential_ref:
            try:
                await self.auth_store.revoke_credential(
                    str(platform_credential_ref), reason=reason[:1_000]
                )
            except (KeyError, ValueError, PlatformBlackboxNotFound):
                pass
        await self._delete_secret_ref(str(row["task_token_secret_ref"]))
        row = await self._close_formal_collaboration_authority(row)
        if daemon_error is not None:
            if isinstance(
                daemon_error,
                (LocalLiliesUnavailable, LocalLiliesBridgeUnavailable),
            ):
                raise LocalLiliesBridgeUnavailable(
                    "local Lilies daemon is unavailable; cancellation remains pending",
                    details={
                        **self._safe_assignment_ids(row),
                        "status": "unavailable",
                        "desired_state": BridgeDesiredState.cancelled.value,
                    },
                ) from daemon_error
            if isinstance(daemon_error, LocalLiliesBridgeSecurityError):
                raise LocalLiliesBridgeSecurityError(
                    f"local Lilies cancellation cleanup failed security validation: {daemon_error}",
                    details={
                        **self._safe_assignment_ids(row),
                        "desired_state": BridgeDesiredState.cancelled.value,
                    },
                ) from daemon_error
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies rejected cancellation; platform credential is revoked",
                details={
                    **self._safe_assignment_ids(row),
                    "desired_state": BridgeDesiredState.cancelled.value,
                },
            ) from daemon_error
        row = await self.store.update_assignment(
            assignment_id,
            phase=BridgeAssignmentStep.cancelled,
            status="cancelled",
            daemon_status=daemon_session_status,
            last_error_code=None,
            last_error_message=None,
        )
        await self._fault("cancel.assignment_state_committed", self._safe_assignment_ids(row))
        try:
            await self.workflow_storage.update_build(row["build_id"], status="cancelled")
        except KeyError:
            # A cancellation may race the crash window after bridge reservation
            # but before the workflow build row is created.
            pass
        row, _, _, _ = await self._drain_cancelled_terminal_events_locked(row)
        await self._archive_formal_terminal(row)
        await self.store.set_connection_state(
            row["connection_id"],
            status=BridgeConnectionStatus.connected,
            seen=True,
        )
        return self._assignment_projection(row)

    async def relay_events(
        self,
        assignment_id: UUID | str,
        *,
        max_events: int = 100,
    ) -> LocalLiliesRelayResult:
        async with self._assignment_lock(assignment_id):
            try:
                return await self._relay_events_locked(
                    assignment_id,
                    max_events=max_events,
                )
            except _AssignmentCancellationWon as outcome:
                return LocalLiliesRelayResult(
                    assignment=outcome.assignment,
                    inserted=0,
                    replayed=0,
                    relay_cursor=outcome.assignment.relay_cursor,
                    ack_cursor=outcome.assignment.ack_cursor,
                )

    async def _relay_events_locked(
        self,
        assignment_id: UUID | str,
        *,
        max_events: int,
    ) -> LocalLiliesRelayResult:
        self.require_enabled()
        row = await self.store.get_assignment(assignment_id)
        if row["desired_state"] == BridgeDesiredState.cancelled.value:
            relay_cursor_before = int(row["relay_cursor"])
            if row["phase"] == BridgeAssignmentStep.cancelled.value:
                (
                    row,
                    inserted,
                    replayed,
                    drain_authenticated,
                ) = await self._drain_cancelled_terminal_events_locked(
                    row,
                    max_events=max_events,
                )
                if drain_authenticated:
                    await self.store.set_connection_state(
                        row["connection_id"],
                        status=BridgeConnectionStatus.connected,
                        seen=True,
                    )
                return LocalLiliesRelayResult(
                    assignment=self._assignment_projection(row),
                    inserted=inserted,
                    replayed=replayed,
                    relay_cursor=int(row["relay_cursor"]),
                    ack_cursor=int(row["ack_cursor"]),
                )
            projection = await self._cancel_assignment_locked(
                assignment_id,
                idempotency_key=(f"cancel.relay.{str(row['assignment_id']).replace('-', '')}"),
                reason="relay observed a persisted cancellation request",
            )
            return LocalLiliesRelayResult(
                assignment=projection,
                inserted=max(0, projection.relay_cursor - relay_cursor_before),
                replayed=0,
                relay_cursor=projection.relay_cursor,
                ack_cursor=projection.ack_cursor,
            )
        if row["phase"] == BridgeAssignmentStep.completed.value:
            (
                row,
                inserted,
                replayed,
                drain_authenticated,
            ) = await self._drain_completed_terminal_events_locked(
                row,
                max_events=max_events,
            )
            if drain_authenticated:
                await self.store.set_connection_state(
                    row["connection_id"],
                    status=BridgeConnectionStatus.connected,
                    seen=True,
                )
            row = await self._archive_formal_success(row)
            return LocalLiliesRelayResult(
                assignment=self._assignment_projection(row),
                inserted=inserted,
                replayed=replayed,
                relay_cursor=int(row["relay_cursor"]),
                ack_cursor=int(row["ack_cursor"]),
            )
        row = await self._recheck_assignment_active(row, checkpoint="relay start")
        connection = await self.store.get_connection(row["connection_id"])
        token = await self._connection_token(connection, assignment=row)
        relay_cursor = int(row["relay_cursor"])
        ack_cursor = int(row["ack_cursor"])
        if relay_cursor > ack_cursor:
            row = await self._recheck_assignment_active(
                row, checkpoint="relay acknowledgement recovery"
            )
            try:
                self._validate_ack_receipt(
                    await self.client.acknowledge_events(
                        str(connection["base_url"]),
                        token,
                        row["session_id"],
                        {
                            "idempotency_key": self._ack_key(row["assignment_id"], relay_cursor),
                            "cursor": relay_cursor,
                        },
                    ),
                    row=row,
                    connection=connection,
                    expected_cursor=relay_cursor,
                )
            except LocalLiliesClientError as error:
                await self._recheck_assignment_active(
                    row, checkpoint="relay acknowledgement recovery failure"
                )
                await self._raise_assignment_unavailable(row, error)
            row = await self._recheck_assignment_active(
                row, checkpoint="relay acknowledgement recovery commit"
            )
            row = await self._update_active_assignment(
                row,
                checkpoint="relay acknowledgement cursor commit",
                ack_cursor=relay_cursor,
                last_error_code=None,
                last_error_message=None,
            )
            ack_cursor = relay_cursor
        row = await self._recheck_assignment_active(row, checkpoint="relay event fetch")
        try:
            events = await self.client.fetch_events(
                str(connection["base_url"]),
                token,
                row["session_id"],
                after=relay_cursor,
                max_events=max(1, min(max_events, 1_000)),
            )
        except LocalLiliesClientError as error:
            await self._recheck_assignment_active(row, checkpoint="relay event fetch failure")
            await self._raise_assignment_unavailable(row, error)
        row = await self._recheck_assignment_active(row, checkpoint="relay event commit")
        self._validate_relay_batch(events, after=relay_cursor)
        try:
            row, inserted, replayed = await self.store.commit_relay_events(
                assignment_id, row["session_id"], events
            )
        except _AssignmentDesiredStateChanged:
            await self._recheck_assignment_active(row, checkpoint="relay event compare-and-set")
            raise AssertionError("active relay compare-and-set did not resolve")
        relay_cursor = int(row["relay_cursor"])
        await self._fault(
            "relay.committed_before_ack",
            {**self._safe_assignment_ids(row), "relay_cursor": str(relay_cursor)},
        )
        if relay_cursor > int(row["ack_cursor"]):
            row = await self._recheck_assignment_active(row, checkpoint="relay acknowledgement")
            try:
                self._validate_ack_receipt(
                    await self.client.acknowledge_events(
                        str(connection["base_url"]),
                        token,
                        row["session_id"],
                        {
                            "idempotency_key": self._ack_key(row["assignment_id"], relay_cursor),
                            "cursor": relay_cursor,
                        },
                    ),
                    row=row,
                    connection=connection,
                    expected_cursor=relay_cursor,
                )
            except LocalLiliesClientError as error:
                await self._recheck_assignment_active(
                    row, checkpoint="relay acknowledgement failure"
                )
                await self._raise_assignment_unavailable(row, error)
            row = await self._recheck_assignment_active(
                row, checkpoint="relay acknowledgement commit"
            )
            row = await self._update_active_assignment(
                row,
                checkpoint="relay acknowledged cursor commit",
                ack_cursor=relay_cursor,
                last_error_code=None,
                last_error_message=None,
            )
        row = await self._recheck_assignment_active(row, checkpoint="relay session status read")
        try:
            session = self._validate_session_receipt(
                await self.client.get_session(
                    str(connection["base_url"]), token, row["session_id"]
                ),
                row=row,
                require_assignment_binding=True,
            )
        except LocalLiliesClientError as error:
            await self._recheck_assignment_active(row, checkpoint="relay session status failure")
            await self._raise_assignment_unavailable(row, error)
        row = await self._recheck_assignment_active(row, checkpoint="relay session state commit")
        step, status = self._phase_for_daemon_status(session.status)
        row = await self._update_active_assignment(
            row,
            checkpoint="relay assignment state commit",
            phase=step,
            status=status,
            daemon_status=session.status,
            last_error_code=(
                "daemon_session_error" if session.status is SessionStatus.error else None
            ),
            last_error_message=(
                "local Lilies session ended in error"
                if session.status is SessionStatus.error
                else None
            ),
        )
        await self._fault("relay.assignment_state_committed", self._safe_assignment_ids(row))
        formal_error = (
            step is BridgeAssignmentStep.error
            and self._is_formal_assignment(row)
            and row.get("submission_json") is not None
        )
        if step is BridgeAssignmentStep.completed:
            (
                row,
                terminal_inserted,
                terminal_replayed,
                _,
            ) = await self._drain_completed_terminal_events_locked(row)
            inserted += terminal_inserted
            replayed += terminal_replayed
            row = await self._archive_formal_success(row)
            step = BridgeAssignmentStep(str(row["phase"]))
            status = str(row["status"])
        elif formal_error:
            (
                row,
                terminal_inserted,
                terminal_replayed,
                _,
            ) = await self._drain_error_terminal_events_locked(row)
            inserted += terminal_inserted
            replayed += terminal_replayed
        await self.workflow_storage.update_build(
            row["build_id"],
            status=self._build_status(
                status,
                formal=self._is_formal_assignment(row),
            ),
        )
        if formal_error:
            await self._archive_formal_terminal(row)
        row = await self._recheck_assignment_active(
            row, checkpoint="relay workflow build state commit"
        )
        await self.store.set_connection_state(
            row["connection_id"],
            status=BridgeConnectionStatus.connected,
            seen=True,
        )
        return LocalLiliesRelayResult(
            assignment=self._assignment_projection(row),
            inserted=inserted,
            replayed=replayed,
            relay_cursor=int(row["relay_cursor"]),
            ack_cursor=int(row["ack_cursor"]),
        )

    async def _drain_cancelled_terminal_events_locked(
        self,
        row: dict[str, Any],
        *,
        max_events: int = 1_000,
    ) -> tuple[dict[str, Any], int, int, bool]:
        return await self._drain_terminal_events_locked(
            row,
            expected_phase=BridgeAssignmentStep.cancelled,
            expected_desired_state=BridgeDesiredState.cancelled,
            accepted_session_statuses=frozenset(
                {
                    SessionStatus.cancelled,
                    SessionStatus.closed,
                }
            ),
            terminal_status="cancelled",
            require_cancel_event=True,
            allow_missing_daemon_status=True,
            max_events=max_events,
        )

    async def _drain_completed_terminal_events_locked(
        self,
        row: dict[str, Any],
        *,
        max_events: int = 1_000,
    ) -> tuple[dict[str, Any], int, int, bool]:
        return await self._drain_terminal_events_locked(
            row,
            expected_phase=BridgeAssignmentStep.completed,
            expected_desired_state=BridgeDesiredState.active,
            accepted_session_statuses=frozenset({SessionStatus.completed}),
            terminal_status="completed",
            require_cancel_event=False,
            allow_missing_daemon_status=False,
            max_events=max_events,
        )

    async def _drain_error_terminal_events_locked(
        self,
        row: dict[str, Any],
        *,
        max_events: int = 1_000,
    ) -> tuple[dict[str, Any], int, int, bool]:
        return await self._drain_terminal_events_locked(
            row,
            expected_phase=BridgeAssignmentStep.error,
            expected_desired_state=BridgeDesiredState.active,
            accepted_session_statuses=frozenset({SessionStatus.error}),
            terminal_status="failed",
            require_cancel_event=False,
            allow_missing_daemon_status=False,
            max_events=max_events,
        )

    async def _drain_terminal_events_locked(
        self,
        row: dict[str, Any],
        *,
        expected_phase: BridgeAssignmentStep,
        expected_desired_state: BridgeDesiredState,
        accepted_session_statuses: frozenset[SessionStatus],
        terminal_status: str,
        require_cancel_event: bool,
        allow_missing_daemon_status: bool,
        max_events: int,
    ) -> tuple[dict[str, Any], int, int, bool]:
        """Persist and acknowledge the immutable tail of a terminal session.

        The platform connection bearer remains usable after task credential
        cleanup, so a restart can finish the terminal audit stream. Successful
        formal runs are not archiveable until an empty tail and the bound
        completed session have both been observed.
        """

        if (
            row["desired_state"] != expected_desired_state.value
            or row["phase"] != expected_phase.value
        ):
            raise LocalLiliesBridgeConflict(
                f"terminal event drain requires a {terminal_status} assignment",
                details=self._safe_assignment_ids(row),
            )
        if row.get("terminal_events_drained_at") is not None:
            return row, 0, 0, False
        preserve_terminal_error = expected_phase is BridgeAssignmentStep.error
        clear_error_fields = (
            {} if preserve_terminal_error else {"last_error_code": None, "last_error_message": None}
        )

        # A missing daemon status is durable proof that cancellation received a
        # 404 while the bridge was still in the pre-session recorded phase.
        if allow_missing_daemon_status and row.get("daemon_status") is None:
            row = await self.store.update_assignment(
                row["assignment_id"],
                expected_desired_state=expected_desired_state,
                terminal_events_drained_at=_now().isoformat(),
                **clear_error_fields,
            )
            return row, 0, 0, False

        connection = await self.store.get_connection(row["connection_id"])
        token = await self._connection_token(connection, assignment=row)
        inserted_total = 0
        replayed_total = 0
        authenticated = False
        remaining = max(1, min(max_events, 1_000))
        try:
            while True:
                relay_cursor = int(row["relay_cursor"])
                ack_cursor = int(row["ack_cursor"])
                if relay_cursor > ack_cursor:
                    raw_ack = await self.client.acknowledge_events(
                        str(connection["base_url"]),
                        token,
                        row["session_id"],
                        {
                            "idempotency_key": self._ack_key(row["assignment_id"], relay_cursor),
                            "cursor": relay_cursor,
                        },
                    )
                    authenticated = True
                    self._validate_ack_receipt(
                        raw_ack,
                        row=row,
                        connection=connection,
                        expected_cursor=relay_cursor,
                    )
                    row = await self.store.update_assignment(
                        row["assignment_id"],
                        expected_desired_state=expected_desired_state,
                        ack_cursor=relay_cursor,
                        **clear_error_fields,
                    )

                # The caller's event limit is an invocation budget, not merely
                # a page size. A full page cannot prove end-of-stream, so keep
                # the durable drain checkpoint open for the next relay/recovery.
                if remaining <= 0:
                    return row, inserted_total, replayed_total, authenticated

                events = await self.client.fetch_events(
                    str(connection["base_url"]),
                    token,
                    row["session_id"],
                    after=int(row["relay_cursor"]),
                    max_events=remaining,
                )
                authenticated = True
                self._validate_relay_batch(events, after=int(row["relay_cursor"]))
                self._validate_terminal_assignment_events(events, row=row)
                if events:
                    remaining -= len(events)
                    row, inserted, replayed = await self.store.commit_relay_events(
                        row["assignment_id"], row["session_id"], events
                    )
                    inserted_total += inserted
                    replayed_total += replayed
                    await self._fault(
                        "terminal_relay.committed_before_ack",
                        {
                            **self._safe_assignment_ids(row),
                            "relay_cursor": str(row["relay_cursor"]),
                        },
                    )
                    continue

                session = self._validate_session_receipt(
                    await self.client.get_session(
                        str(connection["base_url"]), token, row["session_id"]
                    ),
                    row=row,
                    require_assignment_binding=(
                        expected_phase is BridgeAssignmentStep.completed
                        or row.get("submission_json") is not None
                    ),
                )
                if session.status not in accepted_session_statuses:
                    raise LocalLiliesBridgeSecurityError(
                        f"daemon terminal event drain did not confirm {terminal_status}",
                        details=self._safe_assignment_ids(row),
                    )
                if (
                    require_cancel_event
                    and session.status == SessionStatus.cancelled
                    and row.get("submission_json") is not None
                ):
                    cancellation_events = await self.store.get_assignment_event_data(
                        row["assignment_id"], "assignment.cancelled"
                    )
                    if not cancellation_events or any(
                        str(event.get("assignment_id")) != str(row["assignment_id"])
                        for event in cancellation_events
                    ):
                        raise LocalLiliesBridgeSecurityError(
                            "daemon terminal stream omitted the bound assignment cancellation",
                            details=self._safe_assignment_ids(row),
                        )
                row = await self.store.update_assignment(
                    row["assignment_id"],
                    expected_desired_state=expected_desired_state,
                    daemon_status=session.status,
                    terminal_events_drained_at=_now().isoformat(),
                    **clear_error_fields,
                )
                return row, inserted_total, replayed_total, authenticated
        except (LocalLiliesClientError, LocalLiliesBridgeError) as error:
            error_code = (
                "terminal_event_drain_security"
                if isinstance(error, LocalLiliesBridgeSecurityError)
                else "terminal_event_drain_pending"
            )
            error_message = f"{terminal_status} assignment terminal event drain is pending"
            await self.store.set_connection_state(
                row["connection_id"],
                status=BridgeConnectionStatus.unavailable,
                error_code=error_code,
                error_message=error_message,
            )
            if not preserve_terminal_error:
                await self.store.update_assignment(
                    row["assignment_id"],
                    expected_desired_state=expected_desired_state,
                    status=terminal_status,
                    last_error_code=error_code,
                    last_error_message=error_message,
                )
            if isinstance(error, LocalLiliesBridgeSecurityError):
                raise
            if isinstance(error, LocalLiliesUnavailable):
                raise LocalLiliesBridgeUnavailable(
                    "local Lilies terminal event drain is unavailable",
                    details=self._safe_assignment_ids(row),
                ) from error
            raise LocalLiliesBridgeDaemonRejected(
                f"local Lilies rejected {terminal_status} assignment terminal event drain",
                details=self._safe_assignment_ids(row),
            ) from error

    async def freeze_formal_run_archive_intent(
        self,
        *,
        channel: CollaborationChannel,
        request: FormalRunArchivePreparationRequest,
        actor_id: str,
    ) -> FormalRunArchiveIntentReceipt:
        """Persist Lilies' final evidence selection before its turn can end.

        No archive or success claim is created here.  The authenticated daemon
        completion and terminal event drain remain the only trigger for that
        platform-owned work.
        """

        self.require_enabled()
        async with self._assignment_lock(channel.assignment_id):
            row = await self.store.get_assignment(channel.assignment_id)
            if not self._is_formal_assignment(row):
                raise LocalLiliesBridgeSecurityError(
                    "formal archive intent belongs to a non-formal assignment",
                    details=self._safe_assignment_ids(row),
                )
            assignment = self._persisted_formal_assignment(row)
            task = assignment.task_package
            access = assignment.collaboration
            if (
                task is None
                or access is None
                or channel.channel_id != access.channel_id
                or channel.assignment_id != assignment.assignment_id
                or channel.lilies_session_id != UUID(str(row["session_id"]))
                or channel.task_id != task.task_id
                or channel.task_revision != task.revision
                or channel.application_ids != assignment.platform.application_ids
                or request.expected_channel_revision != channel.revision
                or channel.status is not ChannelStatus.active
            ):
                raise LocalLiliesBridgeSecurityError(
                    "formal archive intent changed its frozen assignment binding",
                    details=self._safe_assignment_ids(row),
                )
            validator = self.formal_archive_intent_validator
            if validator is not None:
                validation = validator(channel.channel_id, request)
                if inspect.isawaitable(validation):
                    await validation
            intent = {
                "schema_version": "1.0",
                "assignment_id": str(assignment.assignment_id),
                "session_id": str(row["session_id"]),
                "channel_id": str(channel.channel_id),
                "task_id": task.task_id,
                "revision": task.revision,
                "run_id": task.run_id,
                "actor_id": actor_id,
                "request": request.model_dump(mode="json", exclude_none=True),
            }
            intent_json = _canonical_json(intent)
            intent_digest = _digest(intent)
            row, _ = await self.store.reserve_formal_archive_intent(
                assignment.assignment_id,
                intent_json=intent_json,
                intent_digest=intent_digest,
            )
            accepted_at = _parse_time(row.get("formal_archive_intent_accepted_at"))
            if accepted_at is None:  # pragma: no cover - guarded by the store
                raise LocalLiliesBridgeSecurityError(
                    "formal archive intent has no durable acceptance time",
                    details=self._safe_assignment_ids(row),
                )
            return FormalRunArchiveIntentReceipt(
                task_id=task.task_id,
                revision=task.revision,
                run_id=task.run_id,
                assignment_id=assignment.assignment_id,
                channel_id=channel.channel_id,
                claim_id=request.claim_id,
                intent_digest=intent_digest,
                # This is the immutable receipt for accepting the intent, not
                # a live status projection. Keeping it byte-stable means a
                # response-loss retry cannot change after terminal processing.
                state="awaiting_daemon_completion",
                accepted_at=accepted_at,
                replayed=False,
            )

    async def get_assignment(self, assignment_id: UUID | str) -> LocalLiliesAssignment:
        self.require_enabled()
        return self._assignment_projection(await self.store.get_assignment(assignment_id))

    async def get_assignment_session(
        self,
        assignment_id: UUID | str,
    ) -> SessionResult:
        """Read the assignment-bound daemon session through its paired capability."""

        self.require_enabled()
        row = await self.store.get_assignment(assignment_id)
        connection = await self.store.get_connection(row["connection_id"])
        token = await self._connection_token(connection, assignment=row)
        try:
            raw = await self.client.get_session(
                str(connection["base_url"]),
                token,
                row["session_id"],
            )
        except LocalLiliesClientError as error:
            await self._raise_assignment_unavailable(row, error)
            raise AssertionError("unreachable daemon session projection")
        session = self._validate_session_receipt(
            raw,
            row=row,
            require_assignment_binding=True,
        )
        await self.store.set_connection_state(
            row["connection_id"],
            status=BridgeConnectionStatus.connected,
            seen=True,
        )
        return session

    async def get_assignment_by_build(self, build_id: UUID | str) -> LocalLiliesAssignment:
        self.require_enabled()
        return self._assignment_projection(await self.store.get_assignment_by_build(build_id))

    async def get_assignment_by_session(self, session_id: UUID | str) -> LocalLiliesAssignment:
        self.require_enabled()
        return self._assignment_projection(await self.store.get_assignment_by_session(session_id))

    async def list_assignments_for_application(
        self, application_id: UUID | str
    ) -> list[LocalLiliesAssignment]:
        self.require_enabled()
        return [
            self._assignment_projection(row)
            for row in await self.store.list_assignments_for_application(application_id)
        ]

    async def list_events(
        self,
        assignment_id: UUID | str,
        *,
        after: int = 0,
    ) -> list[LocalLiliesRelayEvent]:
        self.require_enabled()
        await self.store.get_assignment(assignment_id)
        return [
            LocalLiliesRelayEvent(
                assignment_id=UUID(row["assignment_id"]),
                session_id=UUID(row["session_id"]),
                daemon_seq=int(row["daemon_seq"]),
                event_type=row["event_type"],
                data=json.loads(row["data_json"]),
                received_at=_parse_time(row["received_at"]),
            )
            for row in await self.store.list_events(assignment_id, after=after)
        ]

    async def recover_pending_assignments(self) -> LocalLiliesRecoverySummary:
        """Resume bridge-owned work without allowing one daemon failure to block startup."""

        self.require_enabled()
        await self._reconcile_terminal_builds()
        rows = await self.store.list_recoverable_assignments()
        items: list[LocalLiliesRecoveryItem] = []
        counters = {
            "recovered": 0,
            "waiting": 0,
            "cancelled": 0,
            "unavailable": 0,
            "failed": 0,
        }
        for persisted in rows:
            error_code: str | None = None
            try:
                if persisted["desired_state"] == BridgeDesiredState.cancelled.value:
                    projection = await self.cancel_assignment(
                        persisted["assignment_id"],
                        idempotency_key=(
                            f"cancel.recovery.{persisted['assignment_id'].replace('-', '')}"
                        ),
                        reason="platform restart recovered a pending cancellation",
                    )
                else:
                    projection = await self.resume_assignment(persisted["assignment_id"])
                if projection.phase == BridgeAssignmentPhase.waiting:
                    outcome = "waiting"
                elif projection.phase == BridgeAssignmentPhase.cancelled:
                    outcome = "cancelled"
                elif projection.phase == BridgeAssignmentPhase.unavailable:
                    outcome = "unavailable"
                elif projection.phase == BridgeAssignmentPhase.failed:
                    outcome = "failed"
                else:
                    outcome = "recovered"
            except LocalLiliesBridgeUnavailable as error:
                projection = self._assignment_projection(
                    await self.store.get_assignment(persisted["assignment_id"])
                )
                outcome = "unavailable"
                error_code = error.code
            except LocalLiliesBridgeError as error:
                projection = self._assignment_projection(
                    await self.store.get_assignment(persisted["assignment_id"])
                )
                outcome = "failed"
                error_code = error.code
            except Exception:
                # Startup recovery must be total: an implementation defect is recorded
                # as a safe generic result while later assignments still recover.
                projection = self._assignment_projection(
                    await self.store.get_assignment(persisted["assignment_id"])
                )
                outcome = "failed"
                error_code = "recovery_failed"
            counters[outcome] += 1
            items.append(
                LocalLiliesRecoveryItem(
                    assignment_id=projection.assignment_id,
                    application_id=projection.application_id,
                    build_id=projection.build_id,
                    session_id=projection.session_id,
                    connection_id=projection.connection_id,
                    outcome=outcome,
                    phase=projection.phase,
                    error_code=error_code,
                )
            )
        return LocalLiliesRecoverySummary(
            scanned=len(rows),
            assignments=items,
            **counters,
        )

    async def _reconcile_terminal_builds(self) -> None:
        """Repair the cross-database assignment/build projection after a crash.

        Assignment state is the durable bridge authority.  Workflow builds live
        in a separate SQLite store, so the write pair cannot be one transaction;
        startup recovery therefore reapplies every terminal projection.
        """

        for row in await self.store.list_terminal_assignments():
            phase = BridgeAssignmentStep(str(row["phase"]))
            build_status = {
                BridgeAssignmentStep.completed: (
                    "verification_pending" if self._is_formal_assignment(row) else "succeeded"
                ),
                BridgeAssignmentStep.cancelled: "cancelled",
                BridgeAssignmentStep.error: (
                    "invalid"
                    if str(row.get("status")) == "invalid"
                    else "failed"
                ),
            }[phase]
            try:
                await self.workflow_storage.update_build(str(row["build_id"]), status=build_status)
            except KeyError:
                # A pre-build validation failure legitimately has a reserved ID
                # but no workflow build record to reconcile.
                pass

    @staticmethod
    def _is_formal_assignment(row: Mapping[str, Any]) -> bool:
        mode = str(row.get("assignment_mode") or AssignmentMode.customer.value)
        if mode not in {item.value for item in AssignmentMode}:
            raise LocalLiliesBridgeSecurityError(
                "persisted local Lilies assignment has an unknown mode",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )
        return mode == AssignmentMode.formal_experiment.value

    @staticmethod
    def _persisted_formal_assignment(row: Mapping[str, Any]) -> BuildAssignment:
        encoded = row.get("submission_json")
        if not isinstance(encoded, str) or not encoded:
            raise LocalLiliesBridgeSecurityError(
                "formal assignment has no durable prepared assignment",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )
        try:
            assignment = BuildAssignment.model_validate_json(encoded)
        except ValueError as error:
            raise LocalLiliesBridgeSecurityError(
                "persisted formal assignment is invalid",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            ) from error
        if assignment.mode is not AssignmentMode.formal_experiment:
            raise LocalLiliesBridgeSecurityError(
                "persisted formal assignment changed mode",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )
        return assignment

    async def _prepare_formal_assignment(
        self,
        row: dict[str, Any],
    ) -> tuple[dict[str, Any], PreparedFormalAssignment]:
        broker = self.formal_assignment_broker
        if broker is None:
            raise LocalLiliesBridgeSecurityError(
                "formal task-package assignment is not configured",
                details=self._safe_assignment_ids(row),
            )
        try:
            request = StartFormalLocalLiliesBuildRequest.model_validate_json(row["request_json"])
            broker_request = PrepareFormalAssignmentRequest(
                task_id=request.task_id,
                revision=request.revision,
                assignment_id=UUID(row["assignment_id"]),
                application_id=UUID(row["application_id"]),
                build_id=UUID(row["build_id"]),
                session_id=UUID(row["session_id"]),
                connection_id=request.connection_id,
                environment_instance_id=request.environment_instance_id,
                idempotency_key=request.idempotency_key,
            )
            prepare_async = getattr(broker, "prepare_async", None)
            if callable(prepare_async):
                prepared = prepare_async(broker_request)
                if inspect.isawaitable(prepared):
                    prepared = await prepared
            else:
                prepared = await asyncio.to_thread(broker.prepare, broker_request)
            prepared = PreparedFormalAssignment.model_validate(prepared)
            self._validate_prepared_formal_assignment(
                row,
                request=request,
                prepared=prepared,
            )
        except (TaskPackageNotReady, FormalAssignmentRuntimeError) as error:
            unavailable_code = (
                "formal_environment_not_ready"
                if isinstance(error, TaskPackageNotReady)
                else "formal_provider_unavailable"
            )
            unavailable_status = (
                "environment_failed"
                if isinstance(error, TaskPackageNotReady)
                else "provider_unavailable"
            )
            try:
                await self._update_active_assignment(
                    row,
                    checkpoint="formal assignment temporary preparation failure",
                    phase=BridgeAssignmentStep.recorded,
                    status=unavailable_status,
                    last_error_code=unavailable_code,
                    last_error_message=("sealed formal assignment preparation can be re-probed"),
                )
            except _AssignmentCancellationWon:
                raise
            raise LocalLiliesBridgeUnavailable(
                "sealed formal assignment environment is not ready; "
                "the same assignment may be re-probed",
                details={
                    **self._safe_assignment_ids(row),
                    "status": unavailable_status,
                },
            ) from error
        except Exception as error:
            try:
                if self._is_formal_assignment(row):
                    await self._seal_formal_pre_submission_rejection(
                        row,
                        error_code="formal_preparation_rejected",
                        error_message=(
                            "sealed formal assignment preparation was rejected"
                        ),
                    )
                else:  # pragma: no cover - formal broker is formal-only
                    await self._update_active_assignment(
                        row,
                        checkpoint="formal assignment preparation rejection",
                        phase=BridgeAssignmentStep.error,
                        status="failed",
                        last_error_code="formal_preparation_rejected",
                        last_error_message=(
                            "sealed formal assignment preparation was rejected"
                        ),
                    )
            except _AssignmentCancellationWon:
                raise
            raise LocalLiliesBridgeSecurityError(
                "sealed formal assignment preparation was rejected",
                details=self._safe_assignment_ids(row),
            ) from error
        encoded = prepared.assignment.model_dump_json(exclude_none=True)
        persisted = row.get("submission_json")
        if persisted:
            if self._persisted_formal_assignment(row) != prepared.assignment:
                raise LocalLiliesBridgeSecurityError(
                    "formal broker replay changed the prepared assignment",
                    details=self._safe_assignment_ids(row),
                )
            return row, prepared
        await self._fault("formal.assignment.prepared", self._safe_assignment_ids(row))
        row = await self._update_active_assignment(
            row,
            checkpoint="formal assignment preparation commit",
            submission_json=encoded,
            status="queued",
            last_error_code=None,
            last_error_message=None,
        )
        await self._fault(
            "formal.assignment.prepare_committed",
            self._safe_assignment_ids(row),
        )
        return row, prepared

    async def _seal_formal_pre_submission_rejection(
        self,
        row: Mapping[str, Any],
        *,
        error_code: Literal[
            "application_not_empty",
            "formal_preparation_rejected",
        ],
        error_message: str,
    ) -> dict[str, Any]:
        """Seal the provably empty terminal tail of a rejected formal reservation.

        A prepared assignment may already be persisted because preparation is
        still pre-daemon. The store instead requires the durable session-
        creation intent to be absent and repeats the remaining proof in one
        SQLite compare-and-set. A concurrent replay therefore cannot begin
        creating a session between checking the row and sealing its tail.
        Persisted session intent or daemon footprints fail closed: their
        terminal stream must still be authenticated and drained normally.
        """

        if not self._is_formal_assignment(row):
            raise LocalLiliesBridgeSecurityError(
                "pre-submission rejection belongs to a non-formal assignment",
                details=self._safe_assignment_ids(row),
            )
        try:
            sealed, tail_sealed = await self.store.seal_formal_pre_submission_rejection(
                row["assignment_id"],
                error_code=error_code,
                error_message=error_message,
            )
        except _AssignmentDesiredStateChanged:
            return await self._recheck_assignment_active(
                row,
                checkpoint="formal pre-submission rejection",
            )
        if tail_sealed or sealed.get("phase") == BridgeAssignmentStep.error.value:
            return sealed
        return await self._update_active_assignment(
            sealed,
            checkpoint="formal rejection with a possible daemon session",
            phase=BridgeAssignmentStep.error,
            status="failed",
            last_error_code=error_code,
            last_error_message=error_message,
        )

    def _validate_prepared_formal_assignment(
        self,
        row: Mapping[str, Any],
        *,
        request: StartFormalLocalLiliesBuildRequest,
        prepared: PreparedFormalAssignment,
    ) -> None:
        assignment = prepared.assignment
        task_ref = assignment.task_package
        collaboration = assignment.collaboration
        expected_assignment_id = UUID(str(row["assignment_id"]))
        expected_application_id = UUID(str(row["application_id"]))
        expected_run_id = f"formal-run:{row['build_id']}"
        if (
            assignment.mode is not AssignmentMode.formal_experiment
            or assignment.assignment_id != expected_assignment_id
            or assignment.idempotency_key != request.idempotency_key
            or assignment.target.mode is not ApplicationTargetMode.existing
            or assignment.target.application_id != expected_application_id
            or assignment.platform.application_ids != [expected_application_id]
            or assignment.platform.credential_ref
            != self._task_credential_ref(expected_assignment_id)
            or str(assignment.platform.base_url).rstrip("/") != self.platform_base_url
            or task_ref is None
            or task_ref.task_id != request.task_id
            or task_ref.revision != request.revision
            or task_ref.run_id != expected_run_id
            or task_ref.environment_instance_id != request.environment_instance_id
            or prepared.run_id != expected_run_id
            or collaboration is None
            or collaboration.credential_ref == assignment.platform.credential_ref
            or collaboration.expires_at != assignment.constraints.deadline_at
        ):
            raise LocalLiliesBridgeSecurityError(
                "formal broker returned an assignment outside the reserved identity",
                details=self._safe_assignment_ids(row),
            )
        if (
            prepared.workspace.manifest_digest != task_ref.workspace_mount_digest
            or prepared.workspace.policy_digest != task_ref.workspace_policy_digest
            or prepared.digests.public_summary_digest != task_ref.public_summary_digest
            or prepared.digests.environment_ready_digest != task_ref.environment_ready_digest
            or prepared.digests.environment_lock_digest != task_ref.environment_lock_digest
            or prepared.digests.allowed_actions_digest != task_ref.allowed_actions_digest
            or prepared.digests.budget_digest != task_ref.budget_digest
        ):
            raise LocalLiliesBridgeSecurityError(
                "formal broker returned inconsistent public workspace digests",
                details=self._safe_assignment_ids(row),
            )

    async def _close_formal_collaboration_authority(
        self,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._is_formal_assignment(row) or not row.get("submission_json"):
            return row
        provider = self.formal_channel_close_provider
        if provider is None:
            raise LocalLiliesBridgeSecurityError(
                "formal collaboration authority closure is not configured",
                details=self._safe_assignment_ids(row),
            )
        assignment = self._persisted_formal_assignment(row)
        access = assignment.collaboration
        task_ref = assignment.task_package
        if access is None or task_ref is None:
            raise LocalLiliesBridgeSecurityError(
                "formal collaboration close has no frozen authority",
                details=self._safe_assignment_ids(row),
            )
        try:
            result = provider(assignment, UUID(str(row["session_id"])))
            raw_receipt = await result if inspect.isawaitable(result) else result
            receipt = CollaborationChannel.model_validate(raw_receipt)
        except Exception as error:
            await self.store.update_assignment(
                row["assignment_id"],
                status="unavailable",
                last_error_code="formal_collaboration_close_pending",
                last_error_message=("formal collaboration authority closure is pending"),
            )
            raise LocalLiliesBridgeUnavailable(
                "formal collaboration authority closure is pending",
                details={
                    **self._safe_assignment_ids(row),
                    "desired_state": BridgeDesiredState.cancelled.value,
                },
            ) from error
        if (
            receipt.channel_id != access.channel_id
            or receipt.task_id != task_ref.task_id
            or receipt.task_revision != task_ref.revision
            or receipt.assignment_id != assignment.assignment_id
            or receipt.lilies_session_id != UUID(str(row["session_id"]))
            or receipt.application_ids != assignment.platform.application_ids
            or receipt.status is not ChannelStatus.closed
            or receipt.closed_at is None
        ):
            await self.store.update_assignment(
                row["assignment_id"],
                status="unavailable",
                last_error_code="formal_collaboration_close_security",
                last_error_message=("formal collaboration close receipt changed identity"),
            )
            raise LocalLiliesBridgeSecurityError(
                "formal collaboration close receipt changed its frozen identity",
                details=self._safe_assignment_ids(row),
            )
        encoded = receipt.model_dump_json(exclude_none=True)
        persisted = row.get("formal_channel_close_receipt_json")
        if persisted:
            try:
                previous = CollaborationChannel.model_validate_json(persisted)
            except ValueError as error:
                raise LocalLiliesBridgeSecurityError(
                    "persisted formal collaboration close receipt is invalid",
                    details=self._safe_assignment_ids(row),
                ) from error
            if previous != receipt:
                raise LocalLiliesBridgeSecurityError(
                    "formal collaboration close replay changed its receipt",
                    details=self._safe_assignment_ids(row),
                )
            return row
        await self._fault(
            "formal.collaboration.closed",
            self._safe_assignment_ids(row),
        )
        row = await self.store.update_assignment(
            row["assignment_id"],
            formal_channel_close_receipt_json=encoded,
            last_error_code=None,
            last_error_message=None,
        )
        await self._fault(
            "formal.collaboration.close_committed",
            self._safe_assignment_ids(row),
        )
        return row

    async def _provision_collaboration_credential(
        self,
        row: dict[str, Any],
        connection: dict[str, Any],
        daemon_token: str,
        assignment: BuildAssignment,
    ) -> dict[str, Any]:
        access = assignment.collaboration
        provider = self.formal_credential_secret_provider
        if access is None or provider is None:
            raise LocalLiliesBridgeSecurityError(
                "formal collaboration authority is unavailable",
                details=self._safe_assignment_ids(row),
            )
        if (
            assignment.mode is not AssignmentMode.formal_experiment
            or access.expires_at != assignment.constraints.deadline_at
        ):
            raise LocalLiliesBridgeSecurityError(
                "formal collaboration authority changed its assignment binding",
                details=self._safe_assignment_ids(row),
            )
        row = await self._recheck_assignment_active(
            row,
            checkpoint="formal collaboration credential provision",
        )
        if row["phase"] != BridgeAssignmentStep.collaboration_provisioning.value:
            row = await self._update_active_assignment(
                row,
                checkpoint="formal collaboration provisioning state commit",
                phase=BridgeAssignmentStep.collaboration_provisioning,
                status="provisioning",
            )
        result = provider(assignment, UUID(str(row["session_id"])))
        raw_secret = await result if inspect.isawaitable(result) else result
        collaboration_token = (
            raw_secret.get_secret_value() if isinstance(raw_secret, SecretStr) else raw_secret
        )
        if not isinstance(collaboration_token, str) or not (
            16 <= len(collaboration_token) <= 16_384
        ):
            raise LocalLiliesBridgeSecurityError(
                "formal collaboration provider returned an invalid bearer",
                details=self._safe_assignment_ids(row),
            )
        task_token = await self._resolve_secret(str(row["task_token_secret_ref"]))
        if secrets.compare_digest(task_token, collaboration_token):
            raise LocalLiliesBridgeSecurityError(
                "platform and collaboration bearers must be distinct",
                details=self._safe_assignment_ids(row),
            )
        try:
            provision = CredentialProvisionResult.model_validate(
                await self.client.provision_credential(
                    str(connection["base_url"]),
                    daemon_token,
                    {
                        "idempotency_key": (
                            "formal.collaboration.provision."
                            f"{row['assignment_id'].replace('-', '')}"
                        ),
                        "credential_ref": access.credential_ref,
                        "assignment_id": row["assignment_id"],
                        "kind": "collaboration_channel",
                        "secret": collaboration_token,
                        "scopes": [scope.value for scope in access.scopes],
                        "expires_at": access.expires_at.isoformat(),
                    },
                )
            )
        except LocalLiliesClientError as error:
            await self._recheck_assignment_active(
                row,
                checkpoint="formal collaboration credential provision failure",
            )
            await self._raise_assignment_unavailable(row, error)
        if (
            str(provision.assignment_id) != row["assignment_id"]
            or provision.credential_ref != access.credential_ref
            or provision.kind.value != "collaboration_channel"
            or [scope.value for scope in provision.scopes]
            != [scope.value for scope in access.scopes]
            or provision.expires_at != access.expires_at
        ):
            raise LocalLiliesBridgeSecurityError(
                "daemon collaboration credential receipt changed its exact authority",
                details=self._safe_assignment_ids(row),
            )
        await self._fault(
            "formal.collaboration.provisioned",
            self._safe_assignment_ids(row),
        )
        row = await self._recheck_assignment_active(
            row,
            checkpoint="formal collaboration credential provision commit",
        )
        row = await self._update_active_assignment(
            row,
            checkpoint="formal collaboration-provisioned state commit",
            phase=BridgeAssignmentStep.collaboration_provisioned,
            status="submitting",
            collaboration_credential_ref=access.credential_ref,
            last_error_code=None,
            last_error_message=None,
        )
        await self._fault(
            "formal.collaboration.provision_committed",
            self._safe_assignment_ids(row),
        )
        await self._delete_secret_ref(str(row["task_token_secret_ref"]))
        return row

    @staticmethod
    def _read_formal_workspace_file(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise LocalLiliesBridgeSecurityError(
                "formal public workspace file is not safely readable"
            ) from error
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise LocalLiliesBridgeSecurityError(
                    "formal public workspace requires isolated regular files"
                )
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(
                descriptor,
                min(1024 * 1024, MAX_FORMAL_WORKSPACE_FILE_BYTES + 1 - size),
            ):
                size += len(chunk)
                if size > MAX_FORMAL_WORKSPACE_FILE_BYTES:
                    raise LocalLiliesBridgeSecurityError(
                        "formal public workspace file exceeds the staging limit"
                    )
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise LocalLiliesBridgeSecurityError(
                    "formal public workspace changed while being staged"
                )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @classmethod
    def _formal_workspace_staging_request(
        cls,
        row: Mapping[str, Any],
        prepared: PreparedFormalAssignment,
    ) -> FormalWorkspaceStagingRequest:
        root = Path(prepared.workspace.path)
        try:
            root_metadata = root.lstat()
        except OSError as error:
            raise LocalLiliesBridgeSecurityError(
                "formal public workspace is unavailable",
                details=cls._safe_assignment_ids(row),
            ) from error
        if not stat.S_ISDIR(root_metadata.st_mode) or root.is_symlink():
            raise LocalLiliesBridgeSecurityError(
                "formal public workspace must be a real directory",
                details=cls._safe_assignment_ids(row),
            )
        paths: list[Path] = []
        for current, directory_names, file_names in os.walk(
            root,
            followlinks=False,
        ):
            current_path = Path(current)
            for name in directory_names:
                child = current_path / name
                metadata = child.lstat()
                if not stat.S_ISDIR(metadata.st_mode) or child.is_symlink():
                    raise LocalLiliesBridgeSecurityError(
                        "formal public workspace contains a linked directory",
                        details=cls._safe_assignment_ids(row),
                    )
            for name in file_names:
                paths.append(current_path / name)
        if not paths or len(paths) > MAX_FORMAL_WORKSPACE_FILES:
            raise LocalLiliesBridgeSecurityError(
                "formal public workspace file count is outside the staging limit",
                details=cls._safe_assignment_ids(row),
            )
        entries: list[FormalWorkspaceFileEntry] = []
        total_bytes = 0
        for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
            payload = cls._read_formal_workspace_file(path)
            total_bytes += len(payload)
            if total_bytes > MAX_FORMAL_WORKSPACE_TOTAL_BYTES:
                raise LocalLiliesBridgeSecurityError(
                    "formal public workspace exceeds the staging byte limit",
                    details=cls._safe_assignment_ids(row),
                )
            entries.append(
                FormalWorkspaceFileEntry(
                    path=path.relative_to(root).as_posix(),
                    digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
                    size_bytes=len(payload),
                    content_base64=base64.b64encode(payload).decode("ascii"),
                )
            )
        bundle = FormalWorkspaceBundle(
            entries=entries,
            bundle_digest=FormalWorkspaceBundle.digest_entries(entries),
        )
        assignment = prepared.assignment
        task_ref = assignment.task_package
        if (
            task_ref is None
            or task_ref.workspace_mount_digest is None
            or task_ref.workspace_policy_digest is None
        ):
            raise LocalLiliesBridgeSecurityError(
                "formal assignment lacks workspace staging authority",
                details=cls._safe_assignment_ids(row),
            )
        return FormalWorkspaceStagingRequest(
            idempotency_key=(
                f"formal.workspace.stage.{str(row['assignment_id']).replace('-', '')}"
            ),
            assignment_id=assignment.assignment_id,
            assignment_digest=formal_assignment_digest(assignment),
            task_package_digest=task_ref.public_summary_digest,
            workspace_mount_digest=task_ref.workspace_mount_digest,
            workspace_policy_digest=task_ref.workspace_policy_digest,
            bundle=bundle,
        )

    async def _stage_formal_workspace(
        self,
        row: dict[str, Any],
        connection: dict[str, Any],
        daemon_token: str,
        prepared: PreparedFormalAssignment,
    ) -> dict[str, Any]:
        request = self._formal_workspace_staging_request(row, prepared)
        persisted = row.get("formal_workspace_receipt_json")
        if persisted:
            try:
                self._validate_formal_workspace_receipt(
                    FormalWorkspaceStagingReceipt.model_validate_json(persisted),
                    request=request,
                    row=row,
                )
            except ValueError as error:
                raise LocalLiliesBridgeSecurityError(
                    "persisted formal workspace receipt is invalid",
                    details=self._safe_assignment_ids(row),
                ) from error
        row = await self._recheck_assignment_active(
            row,
            checkpoint="formal workspace staging",
        )
        if row["phase"] != BridgeAssignmentStep.workspace_staging.value:
            row = await self._update_active_assignment(
                row,
                checkpoint="formal workspace-staging state commit",
                phase=BridgeAssignmentStep.workspace_staging,
                status="submitting",
            )
        try:
            receipt = FormalWorkspaceStagingReceipt.model_validate(
                await self.client.stage_formal_workspace(
                    str(connection["base_url"]),
                    daemon_token,
                    row["session_id"],
                    request.model_dump(mode="json"),
                )
            )
        except LocalLiliesClientError as error:
            await self._recheck_assignment_active(
                row,
                checkpoint="formal workspace staging failure",
            )
            await self._raise_assignment_unavailable(row, error)
        self._validate_formal_workspace_receipt(
            receipt,
            request=request,
            row=row,
        )
        await self._fault(
            "formal.workspace.staged",
            self._safe_assignment_ids(row),
        )
        row = await self._recheck_assignment_active(
            row,
            checkpoint="formal workspace staging receipt commit",
        )
        row = await self._update_active_assignment(
            row,
            checkpoint="formal workspace-staged state commit",
            phase=BridgeAssignmentStep.workspace_staged,
            status="submitting",
            formal_workspace_receipt_json=receipt.model_dump_json(),
            last_error_code=None,
            last_error_message=None,
        )
        await self._fault(
            "formal.workspace.stage_committed",
            self._safe_assignment_ids(row),
        )
        return row

    @staticmethod
    def _validate_formal_workspace_receipt(
        receipt: FormalWorkspaceStagingReceipt,
        *,
        request: FormalWorkspaceStagingRequest,
        row: Mapping[str, Any],
    ) -> None:
        if (
            receipt.session_id != UUID(str(row["session_id"]))
            or receipt.idempotency_key != request.idempotency_key
            or receipt.assignment_id != request.assignment_id
            or receipt.assignment_digest != request.assignment_digest
            or receipt.task_package_digest != request.task_package_digest
            or receipt.workspace_mount_digest != request.workspace_mount_digest
            or receipt.workspace_policy_digest != request.workspace_policy_digest
            or receipt.bundle_digest != request.bundle.bundle_digest
            or receipt.file_count != len(request.bundle.entries)
            or receipt.total_bytes != sum(entry.size_bytes for entry in request.bundle.entries)
        ):
            raise LocalLiliesBridgeSecurityError(
                "daemon workspace receipt changed the exact formal bundle binding",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )

    async def _ensure_task_credential(
        self,
        row: dict[str, Any],
        *,
        formal_assignment: BuildAssignment | None = None,
    ) -> dict[str, Any]:
        assignment_id = UUID(row["assignment_id"])
        credential_id = self._task_credential_id(assignment_id)
        row = await self._recheck_assignment_active(row, checkpoint="platform credential issue")
        row = await self._update_active_assignment(
            row,
            checkpoint="credential-issuing state commit",
            phase=BridgeAssignmentStep.credential_issuing,
            status="provisioning",
        )
        secret_ref = str(row["task_token_secret_ref"])
        if await self._secret_exists(secret_ref):
            access_token = await self._resolve_secret(secret_ref)
        else:
            access_token = f"lpt_{credential_id.hex}_{secrets.token_urlsafe(32)}"
            await self._save_encrypted_secret(
                owner_id=self._assignment_secret_owner(assignment_id),
                name="platform-task-token",
                value=access_token,
                description="Crash-safe one-time task bearer outbox",
            )
        await self._fault("credential.outbox_saved", self._safe_assignment_ids(row))
        row = await self._recheck_assignment_active(
            row, checkpoint="platform credential issue request"
        )
        if self._is_formal_assignment(row):
            assignment = formal_assignment or self._persisted_formal_assignment(row)
            scopes = tuple(
                PlatformBlackboxScope(scope.value) for scope in assignment.platform.scopes
            )
            deadline = assignment.constraints.deadline_at
            task_ref = assignment.task_package
            constraints = assignment.constraints
            if (
                task_ref is None
                or constraints.model_access is None
                or constraints.file_access is None
                or constraints.connector_access is None
                or constraints.max_write_count is None
                or constraints.max_payload_bytes is None
                or constraints.max_report_evidence_rounds is None
                or constraints.stable_hidden_runs is None
            ):
                raise LocalLiliesBridgeSecurityError(
                    "formal task policy is incomplete",
                    details=self._safe_assignment_ids(row),
                )
            credential_policy: dict[str, Any] = {
                "allowed_operations": [
                    PlatformBlackboxOperation(action.value)
                    for action in constraints.allowed_actions
                ],
                "allowed_actions_digest": task_ref.allowed_actions_digest,
                "budget_digest": task_ref.budget_digest,
                "allowed_network_hosts": list(constraints.allowed_hosts),
                "model_access": constraints.model_access,
                "file_access": constraints.file_access,
                "connector_access": constraints.connector_access,
                "readable_host_objects": list(constraints.readable_host_objects),
                "writable_host_operations": list(
                    constraints.writable_host_operations
                ),
                "permission_required_actions": list(
                    constraints.permission_required_actions
                ),
                "max_write_count": constraints.max_write_count,
                "max_payload_bytes": constraints.max_payload_bytes,
                "compensation_actions": list(constraints.compensation_actions),
                "max_report_evidence_rounds": (
                    constraints.max_report_evidence_rounds
                ),
                "stable_hidden_runs": constraints.stable_hidden_runs,
            }
            if assignment.platform.credential_ref != self._task_credential_ref(
                assignment_id
            ) or assignment.platform.application_ids != [UUID(row["application_id"])]:
                raise LocalLiliesBridgeSecurityError(
                    "prepared formal platform authority escaped its deterministic "
                    "assignment binding",
                    details=self._safe_assignment_ids(row),
                )
        else:
            request = StartLocalLiliesBuildRequest.model_validate_json(row["request_json"])
            scopes = self._platform_scopes(request.auto_publish)
            deadline = self._assignment_deadline(row, request)
            credential_policy = {}
        issued = await self.auth_store.issue_credential(
            TaskCredentialGrant(
                assignment_id=assignment_id,
                session_id=UUID(row["session_id"]),
                scopes=list(scopes),
                application_ids=[UUID(row["application_id"])],
                expires_at=deadline,
                **credential_policy,
            ),
            idempotency_key=f"credential.issue.{assignment_id.hex}",
            prepared_access_token=SecretStr(access_token),
            credential_id=credential_id,
        )
        await self._fault("credential.issued", self._safe_assignment_ids(row))
        row = await self._recheck_assignment_active(
            row, checkpoint="platform credential issue commit"
        )
        if issued.access_token.get_secret_value() != access_token:
            raise LocalLiliesBridgeSecurityError(
                "idempotent task credential replay changed its prepared bearer",
                details=self._safe_assignment_ids(row),
            )
        expected_ref = f"platform-task-credential:{credential_id}"
        if issued.credential.credential_ref != expected_ref:
            raise LocalLiliesBridgeSecurityError(
                "task credential identity escaped its deterministic assignment binding",
                details=self._safe_assignment_ids(row),
            )
        row = await self._update_active_assignment(
            row,
            checkpoint="credential-issued state commit",
            phase=BridgeAssignmentStep.credential_issued,
            status="provisioning",
            credential_ref=issued.credential.credential_ref,
            last_error_code=None,
            last_error_message=None,
        )
        await self._fault("credential.issue_committed", self._safe_assignment_ids(row))
        return row

    async def _provision_task_credential(
        self,
        row: dict[str, Any],
        connection: dict[str, Any],
        daemon_token: str,
        *,
        formal_assignment: BuildAssignment | None = None,
    ) -> dict[str, Any]:
        row = await self._recheck_assignment_active(row, checkpoint="daemon credential provision")
        task_token = await self._resolve_secret(str(row["task_token_secret_ref"]))
        formal = self._is_formal_assignment(row)
        if formal:
            assignment = formal_assignment or self._persisted_formal_assignment(row)
            scopes = tuple(
                PlatformBlackboxScope(scope.value) for scope in assignment.platform.scopes
            )
            deadline = assignment.constraints.deadline_at
        else:
            request = StartLocalLiliesBuildRequest.model_validate_json(row["request_json"])
            scopes = self._platform_scopes(request.auto_publish)
            deadline = self._assignment_deadline(row, request)
        try:
            provision = CredentialProvisionResult.model_validate(
                await self.client.provision_credential(
                    str(connection["base_url"]),
                    daemon_token,
                    {
                        "idempotency_key": (
                            f"credential.provision.{row['assignment_id'].replace('-', '')}"
                        ),
                        "credential_ref": row["credential_ref"],
                        "assignment_id": row["assignment_id"],
                        "kind": "platform_assignment",
                        "secret": task_token,
                        "scopes": [scope.value for scope in scopes],
                        "expires_at": deadline.isoformat(),
                    },
                )
            )
        except LocalLiliesClientError as error:
            await self._recheck_assignment_active(
                row, checkpoint="daemon credential provision failure"
            )
            await self._raise_assignment_unavailable(row, error)
        if str(provision.assignment_id) != row["assignment_id"] or (
            provision.credential_ref != row["credential_ref"]
        ):
            raise LocalLiliesBridgeSecurityError(
                "daemon credential receipt did not match the reserved assignment",
                details=self._safe_assignment_ids(row),
            )
        if formal and (
            provision.kind.value != "platform_assignment"
            or [scope.value for scope in provision.scopes] != [scope.value for scope in scopes]
            or provision.expires_at != deadline
        ):
            raise LocalLiliesBridgeSecurityError(
                "daemon formal platform credential receipt changed its exact authority",
                details=self._safe_assignment_ids(row),
            )
        await self._fault("credential.provisioned", self._safe_assignment_ids(row))
        row = await self._recheck_assignment_active(
            row, checkpoint="daemon credential provision commit"
        )
        row = await self._update_active_assignment(
            row,
            checkpoint="credential-provisioned state commit",
            phase=BridgeAssignmentStep.credential_provisioned,
            status="submitting",
            last_error_code=None,
            last_error_message=None,
        )
        await self._fault("credential.provision_committed", self._safe_assignment_ids(row))
        if not formal:
            await self._delete_secret_ref(str(row["task_token_secret_ref"]))
        return row

    async def _submit_assignment(
        self,
        row: dict[str, Any],
        connection: dict[str, Any],
        daemon_token: str,
    ) -> dict[str, Any]:
        row = await self._recheck_assignment_active(row, checkpoint="daemon assignment submission")
        await self._delete_secret_ref(str(row["task_token_secret_ref"]))
        if row.get("submission_json"):
            assignment = BuildAssignment.model_validate_json(row["submission_json"])
        else:
            await self._validate_reserved_empty_application(row)
            assignment = await self._build_assignment(row)
            encoded = assignment.model_dump_json(exclude_none=True)
            row = await self._update_active_assignment(
                row,
                checkpoint="assignment-submitting state commit",
                phase=BridgeAssignmentStep.submitting,
                status="submitting",
                submission_json=encoded,
            )
        try:
            receipt = AssignmentSubmissionResult.model_validate(
                await self.client.submit_assignment(
                    str(connection["base_url"]),
                    daemon_token,
                    row["session_id"],
                    assignment.model_dump(mode="json", exclude_none=True),
                )
            )
        except LocalLiliesClientError as error:
            await self._recheck_assignment_active(
                row, checkpoint="daemon assignment submission failure"
            )
            await self._raise_assignment_unavailable(row, error)
        if (
            str(receipt.assignment_id) != row["assignment_id"]
            or str(receipt.session_id) != row["session_id"]
        ):
            raise LocalLiliesBridgeSecurityError(
                "daemon assignment receipt did not match the reserved four-ID binding",
                details=self._safe_assignment_ids(row),
            )
        await self._fault("assignment.submitted", self._safe_assignment_ids(row))
        row = await self._recheck_assignment_active(
            row, checkpoint="daemon assignment submission commit"
        )
        row = await self._update_active_assignment(
            row,
            checkpoint="assignment-submitted state commit",
            phase=BridgeAssignmentStep.submitted,
            status="running",
            daemon_status=receipt.status,
            last_error_code=None,
            last_error_message=None,
        )
        await self.workflow_storage.update_build(row["build_id"], status="running")
        row = await self._recheck_assignment_active(
            row, checkpoint="submitted workflow build state commit"
        )
        return row

    async def _build_assignment(self, row: dict[str, Any]) -> BuildAssignment:
        request = StartLocalLiliesBuildRequest.model_validate_json(row["request_json"])
        effective = self._effective_constraints(request.constraints)
        scopes = self._platform_scopes(request.auto_publish)
        digest_result = self.contract_digest_provider(scopes, (UUID(row["application_id"]),))
        contract_digest = (
            await digest_result if inspect.isawaitable(digest_result) else digest_result
        )
        if (
            not isinstance(contract_digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", contract_digest) is None
        ):
            raise LocalLiliesBridgeSecurityError("platform contract provider returned bad digest")
        allowed_actions = [
            AllowedAction.platform_contract_get,
            AllowedAction.platform_block_search,
            AllowedAction.platform_block_get,
            AllowedAction.platform_tool_catalog,
            AllowedAction.platform_application_get,
            AllowedAction.platform_draft_inspect,
            AllowedAction.platform_draft_apply,
            AllowedAction.platform_tests_run,
            AllowedAction.platform_run_start,
            AllowedAction.platform_run_get,
            AllowedAction.platform_run_resume,
            AllowedAction.platform_run_cancel,
            AllowedAction.platform_trace_get,
            AllowedAction.platform_artifact_read,
        ]
        if request.auto_publish:
            allowed_actions.append(AllowedAction.platform_publish)
        return BuildAssignment(
            schema_version="1.0",
            assignment_id=UUID(row["assignment_id"]),
            idempotency_key=f"assignment.submit.{row['assignment_id'].replace('-', '')}",
            mode=AssignmentMode.customer,
            requirement=request.requirement,
            business_context=request.business_context,
            target=ApplicationTarget(
                mode=ApplicationTargetMode.existing,
                application_id=UUID(row["application_id"]),
            ),
            platform=PlatformAccess(
                base_url=self.platform_base_url,
                contract_url="/api/v1/lilies/platform-contract",
                contract_digest=contract_digest,
                credential_ref=row["credential_ref"],
                scopes=[PlatformScope(scope.value) for scope in scopes],
                application_ids=[UUID(row["application_id"])],
            ),
            constraints=AssignmentConstraints(
                deadline_at=self._assignment_deadline(row, request),
                max_turns=effective.max_turns,
                max_budget_usd=effective.max_budget_usd,
                max_tool_calls=effective.max_tool_calls,
                network_policy=AssignmentNetworkPolicy.allowlist,
                allowed_hosts=effective.allowed_hosts,
                allowed_actions=allowed_actions,
                prohibited_actions=list(ProhibitedAction),
                no_substitute_validation=False,
            ),
            deliverables=request.deliverables,
            created_at=_parse_time(row["created_at"]),
        )

    async def _ensure_build_record(self, row: dict[str, Any]) -> None:
        if self._is_formal_assignment(row):
            assignment = self._persisted_formal_assignment(row)
            requirement = assignment.requirement
            auto_publish = AllowedAction.platform_publish in assignment.constraints.allowed_actions
            max_turns = assignment.constraints.max_turns
            deadline_at = assignment.constraints.deadline_at
            effective = None
        else:
            request = StartLocalLiliesBuildRequest.model_validate_json(row["request_json"])
            effective = self._effective_constraints(request.constraints)
            requirement = request.requirement
            auto_publish = request.auto_publish
            max_turns = effective.max_turns
            deadline_at = None
        try:
            build = await self.workflow_storage.get_build(row["build_id"])
        except (KeyError, TypeError):
            if deadline_at is not None:
                remaining_seconds = (deadline_at - _now()).total_seconds()
                if remaining_seconds <= 0:
                    raise LocalLiliesBridgeConflict(
                        "formal assignment deadline has already elapsed",
                        details=self._safe_assignment_ids(row),
                    )
            else:
                assert effective is not None
                remaining_seconds = self._remaining_seconds(effective)
            await self.workflow_storage.create_build(
                row["build_id"],
                row["application_id"],
                requirement,
                auto_publish,
                max_turns,
                4,
                max_elapsed_seconds=remaining_seconds,
                planning_mode="disabled",
            )
            return
        if build["application_id"] != row["application_id"]:
            raise LocalLiliesBridgeSecurityError(
                "persisted build escaped its assignment application",
                details=self._safe_assignment_ids(row),
            )

    async def _validate_empty_application(self, application_id: str) -> None:
        await self.workflow_storage.get_application(application_id)
        draft = await self.workflow_storage.get_draft(application_id)
        snapshot = draft["snapshot"]
        workflow = snapshot.workflow
        if (
            int(draft["revision"]) != 0
            or workflow.nodes
            or workflow.edges
            or snapshot.agents
            or snapshot.tests
        ):
            raise LocalLiliesBridgeConflict(
                "application draft must be empty before assigning local Lilies"
            )

    async def _validate_reserved_empty_application(self, row: Mapping[str, Any]) -> None:
        try:
            await self._validate_empty_application(str(row["application_id"]))
        except Exception as error:
            if self._is_formal_assignment(row):
                await self._seal_formal_pre_submission_rejection(
                    row,
                    error_code="application_not_empty",
                    error_message=(
                        "formal local Lilies requires an unchanged empty application "
                        "before submission"
                    ),
                )
            else:
                await self._update_active_assignment(
                    row,
                    checkpoint="empty-application validation failure",
                    phase=BridgeAssignmentStep.error,
                    status="error",
                    last_error_code="application_not_empty",
                    last_error_message=(
                        "local Lilies requires an unchanged empty application before submission"
                    ),
                )
            raise LocalLiliesBridgeConflict(
                "local Lilies can only resume a reserved assignment while its "
                "application is still empty",
                details=self._safe_assignment_ids(row),
            ) from error

    async def _recover_pairing_secret(self, row: dict[str, Any]) -> LocalLiliesConnection | None:
        secret_ref = str(row["access_token_secret_ref"])
        if not await self._secret_exists(secret_ref):
            return None
        token = await self._resolve_secret(secret_ref)
        connection_id = UUID(str(row["id"]))
        client_id = self._paired_client_id(connection_id)
        daemon_status = await self._read_recovery_daemon_status(
            row=row,
            token=token,
            expected_client_id=client_id,
        )
        if daemon_status is None:
            return None
        row = await self.store.complete_connection(
            connection_id,
            client_id=daemon_status.client_id,
            scopes=daemon_status.client_scopes,
            expires_at=daemon_status.client_expires_at,
            fingerprint=daemon_status.daemon_fingerprint,
        )
        return self._connection_projection(row)

    async def _recover_reconnect_operation(
        self,
        operation: Mapping[str, Any],
        row: dict[str, Any],
    ) -> LocalLiliesConnection | None:
        if row.get("client_id") is None:
            raise LocalLiliesBridgeSecurityError(
                "pending reconnect has no durable client identity",
                details={"connection_id": str(row["id"])},
            )
        connection_id = UUID(str(row["id"]))
        owner_id = self._connection_secret_owner(connection_id)
        idempotency_key = str(operation["idempotency_key"])
        rotation_name = self._rotation_secret_name(idempotency_key)
        rotation_ref = f"secret://{owner_id}/{rotation_name}"
        if await self._secret_exists(rotation_ref):
            token = await self._resolve_secret(rotation_ref)
        elif row["status"] == BridgeConnectionStatus.connected.value:
            token = await self._resolve_secret(str(row["access_token_secret_ref"]))
        else:
            return None
        expected_client_id = UUID(str(row["client_id"]))
        daemon_status = await self._read_recovery_daemon_status(
            row=row,
            token=token,
            expected_client_id=expected_client_id,
        )
        if daemon_status is None:
            return None
        await self._save_encrypted_secret(
            owner_id=owner_id,
            name="daemon-access-token",
            value=token,
            description="Recovered rotated Local Lilies daemon bearer",
        )
        row = await self.store.complete_connection(
            connection_id,
            client_id=daemon_status.client_id,
            scopes=daemon_status.client_scopes,
            expires_at=daemon_status.client_expires_at,
            fingerprint=daemon_status.daemon_fingerprint,
        )
        result = self._connection_projection(row)
        await self.store.complete_connection_operation(
            connection_id,
            operation="reconnect",
            idempotency_key=idempotency_key,
            result=result,
        )
        await self._delete_secret_ref(rotation_ref)
        return result

    async def _read_recovery_daemon_status(
        self,
        *,
        row: Mapping[str, Any],
        token: str,
        expected_client_id: UUID,
    ) -> DaemonStatus | None:
        connection_id = str(row["id"])
        if self._token_client_id(token) != expected_client_id:
            raise LocalLiliesBridgeSecurityError(
                "encrypted daemon bearer escaped its client binding",
                details={"connection_id": connection_id},
            )
        try:
            raw_status = await self.client.status(str(row["base_url"]), token)
        except LocalLiliesRemoteError as error:
            if error.status_code in {401, 403}:
                return None
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies rejected connection recovery",
                details={"connection_id": connection_id},
            ) from error
        except LocalLiliesUnavailable as error:
            raise LocalLiliesBridgeUnavailable(
                "local Lilies daemon is unavailable",
                details={"connection_id": connection_id, "status": "unavailable"},
            ) from error
        except LocalLiliesClientError as error:
            raise LocalLiliesBridgeDaemonRejected(
                "local Lilies returned an invalid recovery response",
                details={"connection_id": connection_id},
            ) from error
        try:
            daemon_status = DaemonStatus.model_validate(raw_status)
        except ValueError as error:
            raise LocalLiliesBridgeSecurityError(
                "local Lilies returned an invalid authenticated status receipt",
                details={"connection_id": connection_id},
            ) from error
        if daemon_status.daemon_fingerprint != row["daemon_fingerprint"]:
            raise LocalLiliesBridgeSecurityError(
                "recovered daemon bearer belongs to another local identity",
                details={"connection_id": connection_id},
            )
        if daemon_status.client_id != expected_client_id:
            raise LocalLiliesBridgeSecurityError(
                "recovered daemon bearer belongs to another client",
                details={"connection_id": connection_id},
            )
        if set(daemon_status.client_scopes) != set(REQUIRED_DAEMON_SCOPES):
            raise LocalLiliesBridgeSecurityError(
                "recovered daemon bearer does not have the exact platform scopes",
                details={"connection_id": connection_id},
            )
        if (
            daemon_status.client_expires_at is not None
            and daemon_status.client_expires_at <= _now()
        ):
            raise LocalLiliesBridgeSecurityError(
                "recovered daemon bearer is expired",
                details={"connection_id": connection_id},
            )
        return daemon_status

    async def _recover_connection_outboxes(
        self, *, connection_id: UUID | str | None = None
    ) -> None:
        requested_id = str(connection_id) if connection_id is not None else None
        async with self._connection_recovery_lock:
            connections = await self.store.list_connections()
            by_id = {str(row["id"]): row for row in connections}
            for row in connections:
                if requested_id is not None and str(row["id"]) != requested_id:
                    continue
                if row.get("client_id") is not None or row["status"] not in {
                    BridgeConnectionStatus.pairing.value,
                    BridgeConnectionStatus.unavailable.value,
                }:
                    continue
                try:
                    recovered = await self._recover_pairing_secret(row)
                except LocalLiliesBridgeUnavailable:
                    await self._record_connection_recovery_failure(
                        row,
                        code="daemon_unavailable",
                        message="local Lilies pairing recovery is unavailable",
                    )
                except (LocalLiliesBridgeDaemonRejected, LocalLiliesBridgeSecurityError):
                    await self._record_connection_recovery_failure(
                        row,
                        code="daemon_pairing_recovery_rejected",
                        message="local Lilies pairing recovery failed closed",
                    )
                else:
                    if recovered is not None:
                        by_id[str(row["id"])] = await self.store.get_connection(row["id"])

            operations = await self.store.list_pending_connection_operations(operation="reconnect")
            for operation in operations:
                operation_connection_id = str(operation["connection_id"])
                if requested_id is not None and operation_connection_id != requested_id:
                    continue
                row = by_id.get(operation_connection_id)
                if row is None:
                    row = await self.store.get_connection(operation_connection_id)
                try:
                    await self._recover_reconnect_operation(operation, row)
                except LocalLiliesBridgeUnavailable:
                    await self._record_connection_recovery_failure(
                        row,
                        code="daemon_unavailable",
                        message="local Lilies reconnect recovery is unavailable",
                    )
                except (LocalLiliesBridgeDaemonRejected, LocalLiliesBridgeSecurityError):
                    await self._record_connection_recovery_failure(
                        row,
                        code="daemon_reconnect_recovery_rejected",
                        message="local Lilies reconnect recovery failed closed",
                    )

    async def _record_connection_recovery_failure(
        self, row: Mapping[str, Any], *, code: str, message: str
    ) -> None:
        await self.store.set_connection_state(
            row["id"],
            status=BridgeConnectionStatus.unavailable,
            error_code=code,
            error_message=message,
            expected_statuses=(
                BridgeConnectionStatus.pairing,
                BridgeConnectionStatus.reconnecting,
                BridgeConnectionStatus.unavailable,
            ),
        )

    def _assignment_lock(self, assignment_id: UUID | str) -> asyncio.Lock:
        key = str(assignment_id)
        lock = self._assignment_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._assignment_locks[key] = lock
        return lock

    async def _recheck_assignment_active(
        self,
        row: Mapping[str, Any],
        *,
        checkpoint: str,
    ) -> dict[str, Any]:
        latest = await self.store.get_assignment(str(row["assignment_id"]))
        if latest["desired_state"] == BridgeDesiredState.active.value:
            return latest
        if latest["desired_state"] != BridgeDesiredState.cancelled.value:
            raise LocalLiliesBridgeSecurityError(
                "assignment has an unknown desired state",
                details=self._safe_assignment_ids(latest),
            )
        assignment = await self._cancel_assignment_locked(
            latest["assignment_id"],
            idempotency_key=(f"cancel.race.{str(latest['assignment_id']).replace('-', '')}"),
            reason=f"persisted cancellation won before {checkpoint}"[:1_000],
        )
        raise _AssignmentCancellationWon(assignment)

    async def _update_active_assignment(
        self,
        row: Mapping[str, Any],
        *,
        checkpoint: str,
        **changes: Any,
    ) -> dict[str, Any]:
        try:
            return await self.store.update_assignment(
                str(row["assignment_id"]),
                expected_desired_state=BridgeDesiredState.active,
                **changes,
            )
        except _AssignmentDesiredStateChanged:
            return await self._recheck_assignment_active(row, checkpoint=checkpoint)

    async def _mark_formal_archive_pending(
        self,
        row: Mapping[str, Any],
        *,
        message: str,
    ) -> dict[str, Any]:
        updated = await self.store.update_assignment(
            row["assignment_id"],
            status="verification_pending",
            last_error_code="formal_archive_pending",
            last_error_message=message,
        )
        try:
            await self.workflow_storage.update_build(
                str(row["build_id"]),
                status="verification_pending",
            )
        except KeyError:
            pass
        return updated

    async def _fail_formal_archive_permanently(
        self,
        row: Mapping[str, Any],
        *,
        error_code: str,
        message: str,
        assignment_status: str = "failed",
        sealed_terminal_result: FormalTerminalArchiveResult | None = None,
    ) -> dict[str, Any]:
        """Stop success retries for an invalid frozen request or projection."""

        failed = await self.store.update_assignment(
            row["assignment_id"],
            phase=BridgeAssignmentStep.error,
            status=assignment_status,
            last_error_code=error_code,
            last_error_message=message,
        )
        try:
            await self.workflow_storage.update_build(
                str(row["build_id"]),
                status=assignment_status,
            )
        except KeyError:
            pass
        if sealed_terminal_result is not None:
            assignment = self._persisted_formal_assignment(failed)
            task = assignment.task_package
            if (
                sealed_terminal_result.status is not ArchiveStatus.invalid
                or task is None
                or sealed_terminal_result.assignment_id
                != assignment.assignment_id
                or sealed_terminal_result.task_id != task.task_id
                or sealed_terminal_result.revision != task.revision
                or sealed_terminal_result.run_id != task.run_id
            ):
                raise LocalLiliesBridgeSecurityError(
                    "sealed invalid archive changed its frozen binding",
                    details=self._safe_assignment_ids(failed),
                )
            await self._fault(
                "formal.invalid_success_archive.before_checkpoint",
                self._safe_assignment_ids(failed),
            )
            return await self.store.commit_formal_terminal_archive_result(
                failed["assignment_id"],
                result_json=_canonical_json(
                    sealed_terminal_result.model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                ),
                manifest_digest=sealed_terminal_result.archive_manifest_digest,
            )
        try:
            await self._archive_formal_terminal(failed)
        except Exception:
            # The assignment failure is authoritative. Terminal archive
            # reconciliation remains restart-idempotent and must not revive
            # the rejected success outbox.
            pass
        return failed

    @staticmethod
    def _validated_formal_archive_claim(
        raw_claim: Any,
        *,
        channel_id: UUID,
        assignment: BuildAssignment,
        request: FormalRunArchivePreparationRequest,
        result: FormalRunArchivePreparationResult,
    ) -> VerificationClaim:
        claim = VerificationClaim.model_validate(raw_claim)
        claim_payload = VerificationClaimPayload.model_validate(
            {
                key: value
                for key, value in claim.model_dump(
                    mode="json",
                    exclude_none=True,
                ).items()
                if key in VerificationClaimPayload.model_fields
            }
        )
        if (
            claim.channel_id != channel_id
            or claim.assignment_id != assignment.assignment_id
            or claim.claim_id != request.claim_id
            or claim.status is not ClaimStatus.frozen
            or claim_payload != result.verification_claim
        ):
            raise ValueError("persisted collaboration claim changed the archive binding")
        return claim

    async def _archive_formal_success(
        self,
        row: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Consume a frozen completion intent after authenticated terminal drain.

        Archive creation, claim persistence, and the final bridge checkpoint are
        separate durable outbox steps.  Each step is idempotent, so a process
        loss after either external commit resumes without asking the completed
        daemon to call another tool.
        """

        if (
            not self._is_formal_assignment(row)
            or str(row.get("phase")) != BridgeAssignmentStep.completed.value
            or row.get("terminal_events_drained_at") is None
            or int(row.get("relay_cursor") or 0) != int(row.get("ack_cursor") or 0)
        ):
            return dict(row)
        if row.get("formal_archive_completed_at") is not None:
            return dict(row)
        await self._fault(
            "formal.terminal_events_drained_before_archive",
            self._safe_assignment_ids(row),
        )
        encoded_intent = row.get("formal_archive_intent_json")
        intent_digest = str(row.get("formal_archive_intent_digest") or "")
        if not isinstance(encoded_intent, str) or not encoded_intent or not intent_digest:
            return await self._fail_formal_archive_permanently(
                row,
                error_code="formal_archive_intent_missing",
                message=("formal assignment completed without freezing its archive intent"),
            )
        try:
            intent = json.loads(encoded_intent)
            if not isinstance(intent, dict) or not hmac.compare_digest(
                _digest(intent), intent_digest
            ):
                raise ValueError("formal archive intent digest changed")
            request = FormalRunArchivePreparationRequest.model_validate(intent["request"])
            assignment = self._persisted_formal_assignment(row)
            task = assignment.task_package
            access = assignment.collaboration
            channel_id = UUID(str(intent["channel_id"]))
            actor_id = str(intent["actor_id"])
            if (
                task is None
                or access is None
                or str(intent.get("assignment_id")) != str(assignment.assignment_id)
                or str(intent.get("session_id")) != str(row["session_id"])
                or channel_id != access.channel_id
                or str(intent.get("task_id")) != task.task_id
                or int(intent.get("revision", -1)) != task.revision
                or str(intent.get("run_id")) != task.run_id
                or not actor_id
            ):
                raise ValueError("formal archive intent binding changed")
        except (KeyError, TypeError, ValueError):
            return await self._fail_formal_archive_permanently(
                row,
                error_code="formal_archive_intent_invalid",
                message="formal archive intent failed durable binding validation",
            )

        encoded_result = row.get("formal_archive_result_json")
        if encoded_result is None:
            provider = self.formal_success_archive_provider
            if provider is None:
                pending = await self._mark_formal_archive_pending(
                    row,
                    message="formal success archive provider is unavailable",
                )
                raise LocalLiliesBridgeUnavailable(
                    "formal success archive is pending",
                    details=self._safe_assignment_ids(pending),
                )
            try:
                raw_result = provider(channel_id, request)
                raw_result = await raw_result if inspect.isawaitable(raw_result) else raw_result
            except FormalRunArchiveUnavailable as error:
                pending = await self._mark_formal_archive_pending(
                    row,
                    message="formal success archive creation is pending",
                )
                raise LocalLiliesBridgeUnavailable(
                    "formal success archive is pending",
                    details=self._safe_assignment_ids(pending),
                ) from error
            except FormalRunArchiveInvalid as error:
                return await self._fail_formal_archive_permanently(
                    row,
                    error_code="formal_archive_invalid",
                    message=(
                        "the frozen formal success archive was sealed as invalid"
                    ),
                    assignment_status="invalid",
                    sealed_terminal_result=error.result,
                )
            except FormalRunArchiveError:
                return await self._fail_formal_archive_permanently(
                    row,
                    error_code="formal_archive_rejected",
                    message=(
                        "the frozen formal archive request was rejected by durable "
                        "platform evidence"
                    ),
                )
            except Exception as error:
                pending = await self._mark_formal_archive_pending(
                    row,
                    message="formal success archive creation is pending",
                )
                raise LocalLiliesBridgeUnavailable(
                    "formal success archive is pending",
                    details=self._safe_assignment_ids(pending),
                ) from error
            try:
                result = FormalRunArchivePreparationResult.model_validate(raw_result)
                if (
                    result.assignment_id != assignment.assignment_id
                    or result.channel_id != channel_id
                    or result.task_id != task.task_id
                    or result.revision != task.revision
                    or result.run_id != task.run_id
                    or result.claim_binding.claim_id != request.claim_id
                    or result.verification_claim.claim_id != request.claim_id
                    or result.verification_claim.archive_manifest_digest
                    != result.archive_manifest_digest
                ):
                    raise ValueError("formal success archive result changed binding")
            except (TypeError, ValueError):
                return await self._fail_formal_archive_permanently(
                    row,
                    error_code="formal_archive_result_invalid",
                    message="formal success archive returned an invalid frozen binding",
                )
            await self._fault(
                "formal.success_archive.created_before_commit",
                self._safe_assignment_ids(row),
            )
            encoded_result = _canonical_json(result.model_dump(mode="json", exclude_none=True))
            row = await self.store.commit_formal_archive_result(
                row["assignment_id"],
                intent_digest=intent_digest,
                result_json=encoded_result,
            )
        else:
            try:
                result = FormalRunArchivePreparationResult.model_validate_json(str(encoded_result))
                if (
                    result.assignment_id != assignment.assignment_id
                    or result.channel_id != channel_id
                    or result.task_id != task.task_id
                    or result.revision != task.revision
                    or result.run_id != task.run_id
                    or result.claim_binding.claim_id != request.claim_id
                    or result.verification_claim.claim_id != request.claim_id
                    or result.verification_claim.archive_manifest_digest
                    != result.archive_manifest_digest
                ):
                    raise ValueError("persisted formal archive result changed binding")
            except ValueError:
                return await self._fail_formal_archive_permanently(
                    row,
                    error_code="formal_archive_result_invalid",
                    message="persisted formal archive result is invalid",
                )

        encoded_claim = row.get("formal_claim_result_json")
        claim_committed = False
        if encoded_claim is None:
            claim_provider = self.formal_verification_claim_provider
            if claim_provider is None:
                pending = await self._mark_formal_archive_pending(
                    row,
                    message="formal verification claim provider is unavailable",
                )
                raise LocalLiliesBridgeUnavailable(
                    "formal verification claim is pending",
                    details=self._safe_assignment_ids(pending),
                )
            try:
                raw_claim = claim_provider(channel_id, actor_id, request, result)
                raw_claim = await raw_claim if inspect.isawaitable(raw_claim) else raw_claim
            except CollaborationError:
                return await self._fail_formal_archive_permanently(
                    row,
                    error_code="formal_archive_claim_rejected",
                    message=(
                        "the archived verification claim was permanently rejected "
                        "by its frozen collaboration revision"
                    ),
                )
            except Exception as error:
                pending = await self._mark_formal_archive_pending(
                    row,
                    message="formal verification claim persistence is pending",
                )
                raise LocalLiliesBridgeUnavailable(
                    "formal verification claim is pending",
                    details=self._safe_assignment_ids(pending),
                ) from error
            try:
                claim = self._validated_formal_archive_claim(
                    raw_claim,
                    channel_id=channel_id,
                    assignment=assignment,
                    request=request,
                    result=result,
                )
            except (TypeError, ValueError):
                return await self._fail_formal_archive_permanently(
                    row,
                    error_code="formal_archive_claim_invalid",
                    message=("formal verification claim returned an invalid frozen binding"),
                )
            await self._fault(
                "formal.verification_claim.created_before_commit",
                self._safe_assignment_ids(row),
            )
            encoded_claim = _canonical_json(claim.model_dump(mode="json", exclude_none=True))
            row = await self.store.complete_formal_archive_claim(
                row["assignment_id"],
                intent_digest=intent_digest,
                claim_result_json=encoded_claim,
            )
            claim_committed = True
        else:
            try:
                self._validated_formal_archive_claim(
                    json.loads(str(encoded_claim)),
                    channel_id=channel_id,
                    assignment=assignment,
                    request=request,
                    result=result,
                )
            except (TypeError, ValueError):
                return await self._fail_formal_archive_permanently(
                    row,
                    error_code="formal_archive_claim_invalid",
                    message="persisted formal verification claim is invalid",
                )
        if claim_committed:
            await self._fault(
                "formal.verification_claim.committed_before_build_projection",
                self._safe_assignment_ids(row),
            )
        try:
            await self.workflow_storage.update_build(
                str(row["build_id"]),
                status="verification_pending",
            )
        except KeyError:
            return await self._fail_formal_archive_permanently(
                row,
                error_code="formal_archive_build_missing",
                message="formal archive cannot project its missing workflow build",
            )
        await self._fault(
            "formal.verification_pending_build_projected_before_checkpoint",
            self._safe_assignment_ids(row),
        )
        row = await self.store.mark_formal_archive_completed(
            row["assignment_id"],
            intent_digest=intent_digest,
        )
        return dict(row)

    async def _archive_formal_terminal(
        self,
        row: Mapping[str, Any],
    ) -> None:
        if (
            not self._is_formal_assignment(row)
            or str(row.get("phase"))
            not in {
                BridgeAssignmentStep.cancelled.value,
                BridgeAssignmentStep.error.value,
            }
        ):
            return
        if row.get("terminal_events_drained_at") is None or int(
            row.get("relay_cursor") or 0
        ) != int(row.get("ack_cursor") or 0):
            raise LocalLiliesBridgeUnavailable(
                "formal terminal run archive is waiting for the sealed daemon event stream",
                details=self._safe_assignment_ids(row),
            )
        if row.get("formal_terminal_archive_completed_at") is not None:
            return
        provider = self.formal_terminal_archive_provider
        if provider is None:
            return
        try:
            raw_result = provider(UUID(str(row["assignment_id"])))
            if inspect.isawaitable(raw_result):
                raw_result = await raw_result
        except Exception as error:
            raise LocalLiliesBridgeUnavailable(
                "formal terminal run archive is pending",
                details=self._safe_assignment_ids(row),
            ) from error
        try:
            result = FormalTerminalArchiveResult.model_validate(raw_result)
            expected_status = (
                ArchiveStatus.cancelled
                if str(row["phase"]) == BridgeAssignmentStep.cancelled.value
                else ArchiveStatus.invalid
                if str(row.get("status")) == "invalid"
                else ArchiveStatus.environment_failed
                if str(row.get("status")) == "environment_failed"
                else ArchiveStatus.failed
            )
            if row.get("submission_json"):
                assignment = self._persisted_formal_assignment(row)
                task = assignment.task_package
                valid_binding = (
                    task is not None
                    and result.assignment_id == assignment.assignment_id
                    and result.task_id == task.task_id
                    and result.revision == task.revision
                    and result.run_id == task.run_id
                )
            else:
                request = StartFormalLocalLiliesBuildRequest.model_validate_json(
                    row["request_json"]
                )
                valid_binding = (
                    result.assignment_id == UUID(str(row["assignment_id"]))
                    and result.task_id == request.task_id
                    and result.revision == request.revision
                    and result.run_id == f"formal-run:{row['build_id']}"
                )
            if not valid_binding or result.status is not expected_status:
                raise ValueError("formal terminal archive changed its frozen binding")
        except (TypeError, ValueError) as error:
            raise LocalLiliesBridgeSecurityError(
                "formal terminal archive returned an invalid frozen binding",
                details=self._safe_assignment_ids(row),
            ) from error
        await self._fault(
            "formal.terminal_archive.created_before_commit",
            self._safe_assignment_ids(row),
        )
        await self.store.commit_formal_terminal_archive_result(
            row["assignment_id"],
            result_json=_canonical_json(result.model_dump(mode="json", exclude_none=True)),
            manifest_digest=result.archive_manifest_digest,
        )

    async def _connection_token(
        self,
        connection: dict[str, Any],
        *,
        assignment: dict[str, Any] | None = None,
    ) -> str:
        expires_at = _parse_time(connection.get("expires_at"))
        if expires_at is not None and expires_at <= _now():
            await self.store.set_connection_state(
                connection["id"],
                status=BridgeConnectionStatus.expired,
                error_code="pairing_expired",
                error_message="local Lilies pairing expired",
            )
            details = {"connection_id": connection["id"], "status": "expired"}
            if assignment is not None:
                details.update(self._safe_assignment_ids(assignment))
            raise LocalLiliesBridgeUnavailable("local Lilies pairing expired", details=details)
        try:
            return await self._resolve_secret(str(connection["access_token_secret_ref"]))
        except PlatformHarnessViolation as error:
            details = {"connection_id": connection["id"]}
            if assignment is not None:
                details.update(self._safe_assignment_ids(assignment))
            raise LocalLiliesBridgeSecurityError(
                "local Lilies pairing secret is unavailable", details=details
            ) from error

    async def _raise_assignment_unavailable(
        self, row: dict[str, Any], error: LocalLiliesClientError
    ) -> None:
        if isinstance(error, LocalLiliesUnavailable):
            await self.store.set_connection_state(
                row["connection_id"],
                status=BridgeConnectionStatus.unavailable,
                error_code="daemon_unavailable",
                error_message="local Lilies daemon is unavailable",
            )
            await self.store.update_assignment(
                row["assignment_id"],
                status="unavailable",
                last_error_code="daemon_unavailable",
                last_error_message="local Lilies daemon is unavailable",
            )
            raise LocalLiliesBridgeUnavailable(
                "local Lilies daemon is unavailable",
                details={**self._safe_assignment_ids(row), "status": "unavailable"},
            ) from error
        terminal = await self.store.update_assignment(
            row["assignment_id"],
            phase=BridgeAssignmentStep.error,
            status="failed",
            last_error_code="daemon_rejected",
            last_error_message="local Lilies rejected the persisted operation",
        )
        await self.workflow_storage.update_build(row["build_id"], status="failed")
        if terminal.get("terminal_events_drained_at") is not None:
            await self._archive_formal_terminal(terminal)
        raise LocalLiliesBridgeDaemonRejected(
            "local Lilies rejected the persisted operation",
            details=self._safe_assignment_ids(row),
        ) from error

    async def _save_encrypted_secret(
        self,
        *,
        owner_id: str,
        name: str,
        value: str,
        description: str,
    ) -> str:
        result = await self.harness.save_secret(
            owner_id=owner_id,
            name=name,
            value=value,
            description=description,
        )
        if not result.get("encrypted"):
            await self.harness.delete_secret(owner_id=owner_id, name=name)
            raise LocalLiliesBridgeSecurityError(
                "PlatformHarness encryption is required for local Lilies credentials"
            )
        return str(result["secret_ref"])

    async def _resolve_secret(self, secret_ref: str) -> str:
        owner_id, _ = self._split_secret_ref(secret_ref)
        resolved = await self.harness.inject_secret_references(
            owner_id=owner_id,
            payload={"value": {"$secret": secret_ref}},
        )
        value = resolved.get("value") if isinstance(resolved, dict) else None
        if not isinstance(value, str) or not value:
            raise PlatformHarnessViolation("platform secret did not resolve to a string")
        return value

    async def _secret_exists(self, secret_ref: str) -> bool:
        owner_id, name = self._split_secret_ref(secret_ref)
        return any(
            item.get("name") == name for item in await self.harness.list_secrets(owner_id=owner_id)
        )

    async def _delete_secret_ref(self, secret_ref: str) -> bool:
        owner_id, name = self._split_secret_ref(secret_ref)
        return await self.harness.delete_secret(owner_id=owner_id, name=name)

    @staticmethod
    def _split_secret_ref(secret_ref: str) -> tuple[str, str]:
        normalized = secret_ref.removeprefix("secret://")
        owner_id, separator, name = normalized.partition("/")
        if not separator or not owner_id or not name:
            raise PlatformHarnessViolation("invalid local Lilies secret reference")
        return owner_id, name

    def _effective_constraints(
        self, supplied: LocalLiliesBuildConstraints | None
    ) -> LocalLiliesBuildConstraints:
        platform_host = urlsplit(self.platform_base_url).hostname
        if platform_host is None:
            raise LocalLiliesBridgeSecurityError("platform base URL has no host")
        if supplied is None:
            return LocalLiliesBuildConstraints(
                network_policy=AssignmentNetworkPolicy.allowlist,
                allowed_hosts=[platform_host],
            )
        if supplied.network_policy != AssignmentNetworkPolicy.allowlist:
            raise LocalLiliesBridgeSecurityError(
                "local Lilies assignments require an explicit platform-origin allowlist"
            )
        if not supplied.allowed_hosts:
            return supplied.model_copy(update={"allowed_hosts": [platform_host]})
        if platform_host not in supplied.allowed_hosts:
            raise LocalLiliesBridgeSecurityError(
                "assignment allowlist must include the platform origin host"
            )
        return supplied

    def _assignment_deadline(
        self,
        row: dict[str, Any],
        request: StartLocalLiliesBuildRequest,
    ) -> datetime:
        effective = self._effective_constraints(request.constraints)
        created_at = _parse_time(row["created_at"])
        if created_at is None:
            raise LocalLiliesBridgeSecurityError("assignment has no creation time")
        return effective.deadline_at or (
            created_at + timedelta(seconds=self.default_deadline_seconds)
        )

    def _remaining_seconds(self, constraints: LocalLiliesBuildConstraints) -> float:
        deadline = constraints.deadline_at or (
            _now() + timedelta(seconds=self.default_deadline_seconds)
        )
        remaining = (deadline - _now()).total_seconds()
        if remaining <= 0:
            raise LocalLiliesBridgeConflict("assignment deadline has already elapsed")
        return remaining

    @staticmethod
    def _platform_scopes(
        auto_publish: bool,
    ) -> tuple[PlatformBlackboxScope, ...]:
        if auto_publish:
            return (*DEFAULT_PLATFORM_SCOPES, PlatformBlackboxScope.application_publish)
        return DEFAULT_PLATFORM_SCOPES

    @staticmethod
    def _phase_for_daemon_status(
        status: SessionStatus,
    ) -> tuple[BridgeAssignmentStep, str]:
        if status == SessionStatus.completed:
            return BridgeAssignmentStep.completed, "completed"
        if status in {SessionStatus.cancelled, SessionStatus.closed}:
            return BridgeAssignmentStep.cancelled, "cancelled"
        if status == SessionStatus.error:
            return BridgeAssignmentStep.error, "failed"
        if status in {
            SessionStatus.interrupted,
            SessionStatus.waiting_permission,
            SessionStatus.waiting_collaboration,
        }:
            return BridgeAssignmentStep.interrupted, "waiting"
        return BridgeAssignmentStep.running, status.value

    @staticmethod
    def _build_status(status: str, *, formal: bool = False) -> str:
        if status == "verification_pending":
            return "verification_pending"
        if status == "completed":
            return "verification_pending" if formal else "succeeded"
        if status == "cancelled":
            return "cancelled"
        if status == "failed":
            return "failed"
        if status == "invalid":
            return "invalid"
        return "running"

    @staticmethod
    def _session_idempotency_key(assignment_id: UUID) -> str:
        return f"assignment.session.{assignment_id.hex}"

    @staticmethod
    def _paired_client_id(connection_id: UUID) -> UUID:
        return uuid5(NAMESPACE_URL, f"lilies:daemon-client:{connection_id}")

    @staticmethod
    def _pairing_nonce(connection_id: UUID, operation: str, idempotency_key: str) -> str:
        return hashlib.sha256(
            f"lilies:{connection_id}:{operation}:{idempotency_key}".encode()
        ).hexdigest()

    @staticmethod
    def _rotation_secret_name(idempotency_key: str) -> str:
        suffix = hashlib.sha256(idempotency_key.encode()).hexdigest()[:24]
        return f"daemon-access-token-rotation-{suffix}"

    @staticmethod
    def _new_daemon_token(client_id: UUID) -> str:
        return f"{client_id}.{secrets.token_urlsafe(32)}"

    @staticmethod
    def _token_client_id(access_token: str) -> UUID:
        try:
            return UUID(access_token.split(".", 1)[0])
        except (ValueError, IndexError) as error:
            raise LocalLiliesBridgeSecurityError(
                "prepared daemon bearer has an invalid client binding"
            ) from error

    @staticmethod
    def _task_credential_id(assignment_id: UUID) -> UUID:
        return uuid5(NAMESPACE_URL, f"lilies:platform-task-credential:{assignment_id}")

    @classmethod
    def _task_credential_ref(cls, assignment_id: UUID) -> str:
        return f"platform-task-credential:{cls._task_credential_id(assignment_id)}"

    @staticmethod
    def _ack_key(assignment_id: str, cursor: int) -> str:
        return f"assignment.ack.{assignment_id.replace('-', '')}.{cursor}"

    @staticmethod
    def _resume_key(row: Mapping[str, Any], session: SessionResult) -> str:
        episode = hashlib.sha256(
            (
                f"{session.status.value}:{session.updated_at.astimezone(timezone.utc).isoformat()}"
            ).encode()
        ).hexdigest()[:24]
        return (
            "assignment.resume."
            f"{str(row['assignment_id']).replace('-', '')}."
            f"{session.status.value}.{episode}"
        )

    @staticmethod
    def _connection_secret_owner(connection_id: UUID) -> str:
        return f"local-lilies-connection:{connection_id}"

    @staticmethod
    def _assignment_secret_owner(assignment_id: UUID) -> str:
        return f"local-lilies-assignment:{assignment_id}"

    @staticmethod
    def _safe_assignment_ids(row: Mapping[str, Any]) -> dict[str, str]:
        return {
            key: str(row[key])
            for key in (
                "assignment_id",
                "application_id",
                "build_id",
                "session_id",
                "connection_id",
            )
            if row.get(key) is not None
        } | {"phase": str(row.get("phase") or "recorded")}

    @staticmethod
    def _normalize_loopback_url(value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "http":
            raise LocalLiliesBridgeSecurityError("local Lilies URLs must use loopback HTTP")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise LocalLiliesBridgeSecurityError(
                "local Lilies URL cannot contain credentials, query, or fragment"
            )
        if parsed.path not in {"", "/"}:
            raise LocalLiliesBridgeSecurityError("local Lilies URL cannot contain a base path")
        host = parsed.hostname
        if not host:
            raise LocalLiliesBridgeSecurityError("local Lilies URL has no host")
        if host.casefold() != "localhost":
            try:
                address = ipaddress.ip_address(host)
            except ValueError as error:
                raise LocalLiliesBridgeSecurityError(
                    "local Lilies URL host must be a literal loopback address or localhost"
                ) from error
            if not address.is_loopback:
                raise LocalLiliesBridgeSecurityError("local Lilies URL must stay on loopback")
        return urlunsplit(("http", parsed.netloc, "", "", "")).rstrip("/")

    def _connection_projection(self, row: Mapping[str, Any]) -> LocalLiliesConnection:
        last_error = None
        if row.get("last_error_code"):
            last_error = {
                "code": str(row["last_error_code"]),
                "message": str(row.get("last_error_message") or "local Lilies error"),
            }
        return LocalLiliesConnection(
            connection_id=UUID(str(row["id"])),
            base_url=str(row["base_url"]),
            daemon_fingerprint=str(row["daemon_fingerprint"]),
            client_id=UUID(str(row["client_id"])) if row.get("client_id") else None,
            granted_scopes=[
                LocalScope(scope)
                for scope in json.loads(str(row.get("granted_scopes_json") or "[]"))
            ],
            expires_at=_parse_time(row.get("expires_at")),
            status=BridgeConnectionStatus(str(row["status"])),
            last_error=last_error,
            last_seen_at=_parse_time(row.get("last_seen_at")),
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
        )

    def _assignment_projection(self, row: Mapping[str, Any]) -> LocalLiliesAssignment:
        public_phase = self._public_phase(row)
        last_error = None
        if row.get("last_error_code"):
            last_error = {
                "code": str(row["last_error_code"]),
                "message": str(row.get("last_error_message") or "local Lilies error"),
            }
        return LocalLiliesAssignment(
            assignment_id=UUID(str(row["assignment_id"])),
            application_id=UUID(str(row["application_id"])),
            build_id=UUID(str(row["build_id"])),
            session_id=UUID(str(row["session_id"])),
            connection_id=UUID(str(row["connection_id"])),
            phase=public_phase,
            status=str(row["status"]),
            desired_state=BridgeDesiredState(str(row["desired_state"])),
            daemon_status=(
                SessionStatus(str(row["daemon_status"])) if row.get("daemon_status") else None
            ),
            relay_cursor=int(row["relay_cursor"]),
            ack_cursor=int(row["ack_cursor"]),
            last_error=last_error,
            created_at=_parse_time(str(row["created_at"])),
            updated_at=_parse_time(str(row["updated_at"])),
        )

    @staticmethod
    def _public_phase(row: Mapping[str, Any]) -> BridgeAssignmentPhase:
        if row.get("status") in {
            "unavailable",
            "environment_failed",
            "provider_unavailable",
        }:
            return BridgeAssignmentPhase.unavailable
        step = BridgeAssignmentStep(str(row["phase"]))
        if step == BridgeAssignmentStep.recorded:
            return BridgeAssignmentPhase.recorded
        if step == BridgeAssignmentStep.session_created:
            return BridgeAssignmentPhase.provisioning_credential
        if step in {
            BridgeAssignmentStep.credential_issuing,
            BridgeAssignmentStep.credential_issued,
        }:
            return BridgeAssignmentPhase.provisioning_credential
        if step in {
            BridgeAssignmentStep.credential_provisioned,
            BridgeAssignmentStep.collaboration_provisioning,
            BridgeAssignmentStep.collaboration_provisioned,
            BridgeAssignmentStep.workspace_staging,
            BridgeAssignmentStep.workspace_staged,
            BridgeAssignmentStep.submitting,
        }:
            return BridgeAssignmentPhase.submitting
        if step in {BridgeAssignmentStep.submitted, BridgeAssignmentStep.running}:
            return BridgeAssignmentPhase.running
        if step == BridgeAssignmentStep.interrupted:
            return BridgeAssignmentPhase.waiting
        if step == BridgeAssignmentStep.completed:
            return BridgeAssignmentPhase.completed
        if step == BridgeAssignmentStep.cancelled:
            return BridgeAssignmentPhase.cancelled
        return BridgeAssignmentPhase.failed

    @classmethod
    def _validate_relay_batch(
        cls,
        events: list[dict[str, Any]],
        *,
        after: int,
    ) -> None:
        previous = after
        for event in events:
            if set(event) != {"seq", "event", "data"}:
                raise LocalLiliesBridgeSecurityError(
                    "daemon relay event used an unknown wire field"
                )
            seq = event["seq"]
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= previous:
                raise LocalLiliesBridgeSecurityError(
                    "daemon relay cursors must be strictly increasing"
                )
            if seq != previous + 1:
                raise LocalLiliesRelayCursorGap(
                    f"daemon relay cursor gap: expected {previous + 1}, received {seq}"
                )
            if not isinstance(event["event"], str) or not event["event"]:
                raise LocalLiliesBridgeSecurityError("daemon relay event type is invalid")
            if not isinstance(event["data"], dict):
                raise LocalLiliesBridgeSecurityError("daemon relay event data is invalid")
            if cls._contains_plaintext_secret(event["data"]):
                raise LocalLiliesBridgeSecurityError(
                    "daemon relay event contained a plaintext credential"
                )
            previous = seq

    @staticmethod
    def _validate_terminal_assignment_events(
        events: list[dict[str, Any]],
        *,
        row: Mapping[str, Any],
    ) -> None:
        """Fail closed when a terminal event claims another assignment."""

        expected_assignment_id = str(row["assignment_id"])
        for event in events:
            event_type = str(event["event"])
            data = event["data"]
            claimed_assignment_id = data.get("assignment_id")
            if claimed_assignment_id is not None and (
                str(claimed_assignment_id) != expected_assignment_id
            ):
                raise LocalLiliesBridgeSecurityError(
                    "daemon terminal event escaped its assignment binding",
                    details=LocalLiliesBridge._safe_assignment_ids(row),
                )
            if event_type == "assignment.cancelled" and claimed_assignment_id is None:
                raise LocalLiliesBridgeSecurityError(
                    "daemon assignment cancellation event omitted its binding",
                    details=LocalLiliesBridge._safe_assignment_ids(row),
                )

    @staticmethod
    def _validate_session_receipt(
        raw: Mapping[str, Any],
        *,
        row: Mapping[str, Any],
        require_assignment_binding: bool,
    ) -> SessionResult:
        try:
            session = SessionResult.model_validate(raw)
        except ValueError as error:
            raise LocalLiliesBridgeSecurityError(
                "daemon returned an invalid session receipt",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            ) from error
        assignment_id = str(session.assignment_id) if session.assignment_id is not None else None
        if str(session.session_id) != str(row["session_id"]):
            raise LocalLiliesBridgeSecurityError(
                "daemon session receipt escaped its reserved session binding",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )
        if assignment_id is not None and assignment_id != str(row["assignment_id"]):
            raise LocalLiliesBridgeSecurityError(
                "daemon session receipt escaped its assignment binding",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )
        if require_assignment_binding and assignment_id is None:
            raise LocalLiliesBridgeSecurityError(
                "daemon session receipt omitted its assignment binding",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )
        return session

    @staticmethod
    def _validate_session_operation_receipt(
        raw: Mapping[str, Any],
        *,
        row: Mapping[str, Any],
        operation: str,
    ) -> SessionOperationResult:
        try:
            receipt = SessionOperationResult.model_validate(raw)
        except ValueError as error:
            raise LocalLiliesBridgeSecurityError(
                f"daemon returned an invalid {operation} receipt",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            ) from error
        if str(receipt.session_id) != str(row["session_id"]):
            raise LocalLiliesBridgeSecurityError(
                f"daemon {operation} receipt escaped its session binding",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )
        return receipt

    @staticmethod
    def _validate_ack_receipt(
        raw: Mapping[str, Any],
        *,
        row: Mapping[str, Any],
        connection: Mapping[str, Any],
        expected_cursor: int,
    ) -> SessionAckResult:
        try:
            receipt = SessionAckResult.model_validate(raw)
        except ValueError as error:
            raise LocalLiliesBridgeSecurityError(
                "daemon returned an invalid relay acknowledgement receipt",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            ) from error
        if (
            str(receipt.client_id) != str(connection.get("client_id"))
            or str(receipt.session_id) != str(row["session_id"])
            or receipt.cursor != expected_cursor
        ):
            raise LocalLiliesBridgeSecurityError(
                "daemon relay acknowledgement escaped its client/session/cursor binding",
                details=LocalLiliesBridge._safe_assignment_ids(row),
            )
        return receipt

    @classmethod
    def _contains_plaintext_secret(cls, value: Any) -> bool:
        forbidden = {
            "access_token",
            "api_key",
            "authorization",
            "password",
            "private_key",
            "secret",
            "token",
        }
        if isinstance(value, dict):
            for key, child in value.items():
                if str(key).casefold().replace("-", "_") in forbidden:
                    return True
                if cls._contains_plaintext_secret(child):
                    return True
            return False
        if isinstance(value, list):
            return any(cls._contains_plaintext_secret(child) for child in value)
        if isinstance(value, str):
            return bool(
                re.search(r"lpt_[0-9a-f]{32}_[A-Za-z0-9_-]{43,}", value)
                or re.search(r"[0-9a-f]{8}-[0-9a-f-]{27}\.[A-Za-z0-9_-]{40,}", value)
            )
        return False

    async def _fault(self, stage: str, context: Mapping[str, str]) -> None:
        if self.fault_hook is None:
            return
        result = self.fault_hook(stage, context)
        if inspect.isawaitable(result):
            await result
