from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)


SCHEMA_VERSION = 4
_TOKEN_PATTERN = re.compile(r"^lpt_([0-9a-f]{32})_([A-Za-z0-9_-]{43,128})$")
_ISSUE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_TOKEN_HASH_ITERATIONS = 120_000
_DUMMY_SALT = bytes.fromhex("9b5f0f0f0ce437f097477c191e280317")

Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
IdempotencyKey = Annotated[
    str,
    StringConstraints(
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]
CorrelationLabel = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    ),
]


def _derive_token_digest(token: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        token.encode("utf-8"),
        salt,
        _TOKEN_HASH_ITERATIONS,
    ).hex()


_DUMMY_TOKEN_DIGEST = _derive_token_digest("invalid-platform-task-token", _DUMMY_SALT)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    if value.utcoffset() != timedelta(0):
        raise ValueError("datetime must use UTC (offset +00:00)")
    return value.astimezone(timezone.utc)


def _parse_utc(value: str) -> datetime:
    return _require_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("value must be finite canonical JSON") from error


def _json_digest(value: Any) -> str:
    payload = _canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _idempotency_fingerprint(
    request: BlackboxAuthorizationRequest,
    *,
    required_scope: PlatformBlackboxScope,
    payload_digest: str,
) -> str:
    """Bind exact-once behavior to operation semantics, not attempt correlation.

    Request, tool-call, and contract identifiers remain durable audit correlation,
    but retries may legitimately carry new values for each of them.  The assignment,
    session, application, operation/scope, and canonical payload define whether an
    idempotency-key use is a replay or a conflict.
    """

    return _json_digest(
        {
            "assignment_id": str(request.assignment_id),
            "session_id": str(request.session_id),
            "application_id": str(request.application_id),
            "operation": request.operation.value,
            "required_scope": required_scope.value,
            "payload_digest": payload_digest,
        }
    )


class StrictBlackboxModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class PlatformBlackboxScope(str, Enum):
    catalog_read = "workflow.catalog:read"
    application_write = "workflow.application:write"
    draft_write = "workflow.draft:write"
    test_execute = "workflow.test:execute"
    run_execute = "workflow.run:execute"
    trace_read = "workflow.trace:read"
    artifact_read = "workflow.artifact:read"
    application_publish = "workflow.application:publish"


class PlatformBlackboxOperation(str, Enum):
    contract_get = "platform_contract_get"
    block_search = "platform_block_search"
    block_get = "platform_block_get"
    tool_catalog = "platform_tool_catalog"
    application_create = "platform_application_create"
    application_get = "platform_application_get"
    draft_inspect = "platform_draft_inspect"
    draft_apply = "platform_draft_apply"
    tests_run = "platform_tests_run"
    run_start = "platform_run_start"
    run_get = "platform_run_get"
    run_resume = "platform_run_resume"
    run_cancel = "platform_run_cancel"
    trace_get = "platform_trace_get"
    artifact_read = "platform_artifact_read"
    publish = "platform_publish"


OPERATION_SCOPES: Mapping[PlatformBlackboxOperation, PlatformBlackboxScope] = MappingProxyType(
    {
        PlatformBlackboxOperation.contract_get: PlatformBlackboxScope.catalog_read,
        PlatformBlackboxOperation.block_search: PlatformBlackboxScope.catalog_read,
        PlatformBlackboxOperation.block_get: PlatformBlackboxScope.catalog_read,
        PlatformBlackboxOperation.tool_catalog: PlatformBlackboxScope.catalog_read,
        PlatformBlackboxOperation.application_create: PlatformBlackboxScope.application_write,
        PlatformBlackboxOperation.application_get: PlatformBlackboxScope.application_write,
        PlatformBlackboxOperation.draft_inspect: PlatformBlackboxScope.draft_write,
        PlatformBlackboxOperation.draft_apply: PlatformBlackboxScope.draft_write,
        PlatformBlackboxOperation.tests_run: PlatformBlackboxScope.test_execute,
        PlatformBlackboxOperation.run_start: PlatformBlackboxScope.run_execute,
        PlatformBlackboxOperation.run_get: PlatformBlackboxScope.run_execute,
        PlatformBlackboxOperation.run_resume: PlatformBlackboxScope.run_execute,
        PlatformBlackboxOperation.run_cancel: PlatformBlackboxScope.run_execute,
        PlatformBlackboxOperation.trace_get: PlatformBlackboxScope.trace_read,
        PlatformBlackboxOperation.artifact_read: PlatformBlackboxScope.artifact_read,
        PlatformBlackboxOperation.publish: PlatformBlackboxScope.application_publish,
    }
)

_APPLICATION_SCOPED_OPERATIONS = frozenset(
    {
        PlatformBlackboxOperation.application_get,
        PlatformBlackboxOperation.draft_inspect,
        PlatformBlackboxOperation.draft_apply,
        PlatformBlackboxOperation.tests_run,
        PlatformBlackboxOperation.run_start,
        PlatformBlackboxOperation.run_get,
        PlatformBlackboxOperation.run_resume,
        PlatformBlackboxOperation.run_cancel,
        PlatformBlackboxOperation.trace_get,
        PlatformBlackboxOperation.artifact_read,
        PlatformBlackboxOperation.publish,
    }
)

_MUTATING_OPERATIONS = frozenset(
    {
        PlatformBlackboxOperation.application_create,
        PlatformBlackboxOperation.draft_apply,
        PlatformBlackboxOperation.tests_run,
        PlatformBlackboxOperation.run_start,
        PlatformBlackboxOperation.run_resume,
        PlatformBlackboxOperation.run_cancel,
        PlatformBlackboxOperation.publish,
    }
)


class TaskCredentialGrant(StrictBlackboxModel):
    schema_version: Literal["1.0"] = "1.0"
    assignment_id: UUID
    session_id: UUID
    scopes: list[PlatformBlackboxScope] = Field(min_length=1, max_length=16)
    application_ids: list[UUID] = Field(default_factory=list, max_length=100)
    allowed_operations: list[PlatformBlackboxOperation] = Field(
        default_factory=lambda: list(PlatformBlackboxOperation),
        min_length=1,
        max_length=100,
    )
    allowed_actions_digest: Digest | None = None
    budget_digest: Digest | None = None
    allowed_network_hosts: list[str] = Field(default_factory=list, max_length=100)
    model_access: bool = True
    file_access: bool = True
    connector_access: bool = False
    readable_host_objects: list[str] = Field(default_factory=list, max_length=500)
    writable_host_operations: list[str] = Field(default_factory=list, max_length=500)
    permission_required_actions: list[str] = Field(default_factory=list, max_length=500)
    max_write_count: int = Field(default=1_000_000, ge=0, le=1_000_000)
    max_payload_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
    )
    compensation_actions: list[str] = Field(default_factory=list, max_length=500)
    max_report_evidence_rounds: int = Field(default=100, ge=1, le=100)
    stable_hidden_runs: int = Field(default=1, ge=1, le=100)
    expires_at: datetime

    @field_validator("scopes")
    @classmethod
    def scopes_are_unique(cls, value: list[PlatformBlackboxScope]) -> list[PlatformBlackboxScope]:
        if len(value) != len(set(value)):
            raise ValueError("scopes must not contain duplicates")
        return value

    @field_validator("application_ids", "allowed_operations")
    @classmethod
    def enum_values_are_unique(cls, value: list[Any]) -> list[Any]:
        if len(value) != len(set(value)):
            raise ValueError("credential policy values must not contain duplicates")
        return value

    @field_validator(
        "allowed_network_hosts",
        "readable_host_objects",
        "writable_host_operations",
        "permission_required_actions",
        "compensation_actions",
    )
    @classmethod
    def string_policy_values_are_unique(cls, value: list[str]) -> list[str]:
        if any(not item for item in value) or len(value) != len(set(value)):
            raise ValueError("credential policy values must be non-empty and unique")
        return value

    @field_validator("expires_at")
    @classmethod
    def expiry_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def policy_is_coherent(self) -> TaskCredentialGrant:
        if (self.allowed_actions_digest is None) != (self.budget_digest is None):
            raise ValueError(
                "formal credential policy and budget digests must be supplied together"
            )
        if not set(self.permission_required_actions).issubset(
            self.writable_host_operations
        ):
            raise ValueError(
                "permission-required actions must be writable host operations"
            )
        if (
            not self.connector_access
            and (
                self.readable_host_objects
                or self.writable_host_operations
                or self.permission_required_actions
                or self.compensation_actions
            )
        ):
            raise ValueError(
                "host object and operation policy requires connector_access"
            )
        return self


class TaskCredentialRecord(StrictBlackboxModel):
    credential_id: UUID
    credential_ref: CorrelationLabel
    assignment_id: UUID
    session_id: UUID
    scopes: list[PlatformBlackboxScope]
    application_ids: list[UUID]
    allowed_operations: list[PlatformBlackboxOperation] = Field(
        default_factory=lambda: list(PlatformBlackboxOperation)
    )
    allowed_actions_digest: Digest | None = None
    budget_digest: Digest | None = None
    allowed_network_hosts: list[str] = Field(default_factory=list)
    model_access: bool = True
    file_access: bool = True
    connector_access: bool = False
    readable_host_objects: list[str] = Field(default_factory=list)
    writable_host_operations: list[str] = Field(default_factory=list)
    permission_required_actions: list[str] = Field(default_factory=list)
    max_write_count: int = Field(default=1_000_000, ge=0, le=1_000_000)
    max_payload_bytes: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        le=100 * 1024 * 1024,
    )
    compensation_actions: list[str] = Field(default_factory=list)
    max_report_evidence_rounds: int = Field(default=100, ge=1, le=100)
    stable_hidden_runs: int = Field(default=1, ge=1, le=100)
    expires_at: datetime
    revoked_at: datetime | None = None
    revoke_reason: str | None = Field(default=None, max_length=1_000)
    created_at: datetime
    updated_at: datetime

    @field_validator("expires_at", "revoked_at", "created_at", "updated_at")
    @classmethod
    def timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class IssuedTaskCredential(StrictBlackboxModel):
    credential: TaskCredentialRecord
    access_token: SecretStr


class BlackboxAuthorizationRequest(StrictBlackboxModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: UUID
    assignment_id: UUID
    session_id: UUID
    tool_call_id: CorrelationLabel
    idempotency_key: IdempotencyKey
    application_id: UUID
    operation: PlatformBlackboxOperation
    contract_digest: Digest
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def payload_is_canonical_json(cls, value: dict[str, Any]) -> dict[str, Any]:
        _canonical_json(value)
        return value


class BlackboxRequestState(str, Enum):
    reserved = "reserved"
    completed = "completed"


class BlackboxAuthorizationDecision(StrictBlackboxModel):
    authorization_id: UUID
    credential_id: UUID
    request_id: UUID
    assignment_id: UUID
    session_id: UUID
    tool_call_id: CorrelationLabel
    idempotency_key: IdempotencyKey
    application_id: UUID
    operation: PlatformBlackboxOperation
    required_scope: PlatformBlackboxScope
    contract_digest: Digest
    payload_digest: Digest
    state: BlackboxRequestState
    replayed: bool
    status_code: int | None = Field(default=None, ge=100, le=599)
    result: dict[str, Any] | None = None
    audit_event_id: UUID
    created_at: datetime
    completed_at: datetime | None = None

    @field_validator("created_at", "completed_at")
    @classmethod
    def decision_timestamps_are_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


class BlackboxAuditEventType(str, Enum):
    authorized = "request.authorized"
    replayed = "request.replayed"
    completed = "request.completed"
    completion_replayed = "request.completion_replayed"
    denied = "request.denied"


class BlackboxAuditRecord(StrictBlackboxModel):
    seq: int = Field(ge=1)
    event_id: UUID
    event_type: BlackboxAuditEventType
    outcome: Literal["authorized", "replayed", "completed", "denied"]
    credential_id: UUID | None = None
    authorization_id: UUID | None = None
    assignment_id: UUID
    session_id: UUID
    tool_call_id: CorrelationLabel
    request_id: UUID
    idempotency_key: IdempotencyKey
    application_id: UUID
    operation: PlatformBlackboxOperation
    required_scope: PlatformBlackboxScope
    contract_digest: Digest
    payload_digest: Digest
    reason_code: str | None = Field(default=None, max_length=100)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def audit_timestamp_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class CredentialSecurityEvent(StrictBlackboxModel):
    seq: int = Field(ge=1)
    event_id: UUID
    event_type: Literal[
        "credential.issued",
        "credential.revoked",
        "credential.application_granted",
    ]
    credential_id: UUID
    assignment_id: UUID
    session_id: UUID
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def security_timestamp_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value)


class PlatformBlackboxAuthError(RuntimeError):
    code = "platform_blackbox_auth_error"


class PlatformBlackboxStoreError(PlatformBlackboxAuthError):
    code = "platform_blackbox_store_error"


class PlatformBlackboxNotFound(PlatformBlackboxAuthError):
    code = "not_found"


class PlatformBlackboxAuthenticationError(PlatformBlackboxAuthError):
    code = "invalid_credential"


class PlatformBlackboxCredentialExpired(PlatformBlackboxAuthenticationError):
    code = "credential_expired"


class PlatformBlackboxCredentialRevoked(PlatformBlackboxAuthenticationError):
    code = "credential_revoked"


class PlatformBlackboxAuthorizationError(PlatformBlackboxAuthError):
    code = "permission_denied"


class PlatformBlackboxScopeDenied(PlatformBlackboxAuthorizationError):
    code = "scope_denied"


class PlatformBlackboxApplicationDenied(PlatformBlackboxAuthorizationError):
    code = "application_denied"


class PlatformBlackboxOperationDenied(PlatformBlackboxAuthorizationError):
    code = "operation_denied"


class PlatformBlackboxPayloadLimitExceeded(PlatformBlackboxAuthorizationError):
    code = "payload_limit_exceeded"


class PlatformBlackboxWriteLimitExceeded(PlatformBlackboxAuthorizationError):
    code = "write_limit_exceeded"


class PlatformBlackboxIdempotencyConflict(PlatformBlackboxAuthError):
    code = "idempotency_conflict"


class PlatformBlackboxRequestConflict(PlatformBlackboxAuthError):
    code = "request_conflict"


class _Failure:
    def __init__(self, error_type: type[PlatformBlackboxAuthError], message: str) -> None:
        self.error_type = error_type
        self.message = message

    @property
    def code(self) -> str:
        return self.error_type.code

    def raise_error(self) -> None:
        raise self.error_type(self.message)


class PlatformBlackboxAuthStore:
    """Durable task credential, request-idempotency, and immutable audit boundary.

    The store only persists a salted verifier for each access token.  Callers first
    reserve an authorized request, execute the public platform operation outside this
    module, then persist its response with :meth:`complete_request`.  A retry with the
    same assignment/idempotency binding returns the original reservation or response;
    it never grants permission to repeat the side effect.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._clock = clock or _utc_now
        self._lock = asyncio.Lock()

    async def initialize(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._initialize_sync)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)
        return conn

    def _now(self) -> datetime:
        return _require_utc(self._clock())

    def _initialize_sync(self) -> dict[str, int]:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_blackbox_auth_schema (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            current = int(
                conn.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM platform_blackbox_auth_schema"
                ).fetchone()["version"]
            )
            if current > SCHEMA_VERSION:
                raise PlatformBlackboxStoreError(
                    f"platform blackbox auth schema {current} is newer than supported "
                    f"{SCHEMA_VERSION}"
                )
            if current < 1:
                self._migrate_v1(conn)
                current = 1
            if current < 2:
                self._migrate_v2(conn)
                current = 2
            if current < 3:
                self._migrate_v3(conn)
                current = 3
            if current < 4:
                self._migrate_v4(conn)
        self._secure_database_files()
        return {"schema_version": SCHEMA_VERSION}

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE platform_task_credentials (
              id TEXT PRIMARY KEY,
              credential_ref TEXT NOT NULL UNIQUE,
              assignment_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              token_salt_hex TEXT NOT NULL,
              token_digest TEXT NOT NULL,
              scopes_json TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              revoked_at TEXT,
              revoke_reason TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE platform_task_credential_applications (
              credential_id TEXT NOT NULL,
              application_id TEXT NOT NULL,
              granted_at TEXT NOT NULL,
              PRIMARY KEY(credential_id, application_id),
              FOREIGN KEY(credential_id) REFERENCES platform_task_credentials(id)
            );
            CREATE TABLE platform_blackbox_requests (
              authorization_id TEXT PRIMARY KEY,
              credential_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              request_id TEXT NOT NULL UNIQUE,
              assignment_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              tool_call_id TEXT NOT NULL,
              application_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              required_scope TEXT NOT NULL,
              contract_digest TEXT NOT NULL,
              payload_digest TEXT NOT NULL,
              request_fingerprint TEXT NOT NULL,
              state TEXT NOT NULL CHECK(state IN ('reserved','completed')),
              status_code INTEGER,
              response_json TEXT,
              response_digest TEXT,
              created_application_id TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              UNIQUE(assignment_id, idempotency_key),
              FOREIGN KEY(credential_id) REFERENCES platform_task_credentials(id)
            );
            CREATE TABLE platform_blackbox_audit (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              event_type TEXT NOT NULL,
              outcome TEXT NOT NULL CHECK(outcome IN ('authorized','replayed','completed','denied')),
              credential_id TEXT,
              authorization_id TEXT,
              assignment_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              tool_call_id TEXT NOT NULL,
              request_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              application_id TEXT NOT NULL,
              operation TEXT NOT NULL,
              required_scope TEXT NOT NULL,
              contract_digest TEXT NOT NULL,
              payload_digest TEXT NOT NULL,
              reason_code TEXT,
              details_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(credential_id) REFERENCES platform_task_credentials(id),
              FOREIGN KEY(authorization_id) REFERENCES platform_blackbox_requests(authorization_id)
            );
            CREATE TABLE platform_task_credential_security_events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              event_type TEXT NOT NULL,
              credential_id TEXT NOT NULL,
              assignment_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              details_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(credential_id) REFERENCES platform_task_credentials(id)
            );
            CREATE INDEX idx_platform_task_credentials_assignment
              ON platform_task_credentials(assignment_id, session_id);
            CREATE INDEX idx_platform_task_credentials_expiry
              ON platform_task_credentials(expires_at);
            CREATE INDEX idx_platform_blackbox_requests_credential_created
              ON platform_blackbox_requests(credential_id, created_at);
            CREATE INDEX idx_platform_blackbox_audit_assignment_seq
              ON platform_blackbox_audit(assignment_id, seq);
            CREATE TRIGGER platform_blackbox_audit_no_update
              BEFORE UPDATE ON platform_blackbox_audit
              BEGIN SELECT RAISE(ABORT, 'platform blackbox audit is immutable'); END;
            CREATE TRIGGER platform_blackbox_audit_no_delete
              BEFORE DELETE ON platform_blackbox_audit
              BEGIN SELECT RAISE(ABORT, 'platform blackbox audit is immutable'); END;
            CREATE TRIGGER platform_task_credential_events_no_update
              BEFORE UPDATE ON platform_task_credential_security_events
              BEGIN SELECT RAISE(ABORT, 'platform credential audit is immutable'); END;
            CREATE TRIGGER platform_task_credential_events_no_delete
              BEFORE DELETE ON platform_task_credential_security_events
              BEGIN SELECT RAISE(ABORT, 'platform credential audit is immutable'); END;
            CREATE TRIGGER platform_blackbox_request_correlation_no_update
              BEFORE UPDATE OF credential_id,idempotency_key,request_id,assignment_id,session_id,
                tool_call_id,application_id,operation,required_scope,contract_digest,payload_digest,
                request_fingerprint,created_at
              ON platform_blackbox_requests
              BEGIN SELECT RAISE(ABORT, 'platform blackbox request correlation is immutable'); END;
            CREATE TRIGGER platform_task_credentials_no_delete
              BEFORE DELETE ON platform_task_credentials
              BEGIN SELECT RAISE(ABORT, 'platform task credentials cannot be deleted'); END;
            """
        )
        conn.execute(
            "INSERT INTO platform_blackbox_auth_schema(version,applied_at) VALUES (?,?)",
            (1, self._now().isoformat()),
        )

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """Make one-time task credential issuance crash-resumable.

        The bridge encrypts a caller-prepared token before asking this store to
        persist its verifier.  A durable issuance key then lets a restarted
        platform replay the exact same grant instead of minting an orphaned
        credential in the return/save-secret crash window.
        """

        conn.execute(
            "ALTER TABLE platform_task_credentials ADD COLUMN issue_idempotency_key TEXT"
        )
        conn.execute(
            "ALTER TABLE platform_task_credentials ADD COLUMN issue_payload_digest TEXT"
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX idx_platform_task_credentials_issue_key
              ON platform_task_credentials(issue_idempotency_key)
              WHERE issue_idempotency_key IS NOT NULL
            """
        )
        conn.execute(
            "INSERT INTO platform_blackbox_auth_schema(version,applied_at) VALUES (?,?)",
            (2, self._now().isoformat()),
        )

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        """Persist the exact canonical request payload for formal replay."""

        conn.execute(
            "ALTER TABLE platform_blackbox_requests ADD COLUMN payload_json TEXT"
        )
        conn.execute("DROP TRIGGER platform_blackbox_request_correlation_no_update")
        conn.executescript(
            """
            CREATE TRIGGER platform_blackbox_request_correlation_no_update
              BEFORE UPDATE OF credential_id,idempotency_key,request_id,assignment_id,
                session_id,tool_call_id,application_id,operation,required_scope,
                contract_digest,payload_digest,payload_json,request_fingerprint,created_at
              ON platform_blackbox_requests
              BEGIN SELECT RAISE(ABORT, 'platform blackbox request correlation is immutable'); END;
            """
        )
        conn.execute(
            "INSERT INTO platform_blackbox_auth_schema(version,applied_at) VALUES (?,?)",
            (3, self._now().isoformat()),
        )

    def _migrate_v4(self, conn: sqlite3.Connection) -> None:
        """Bind formal task policy and durable write/payload budgets to credentials."""

        conn.executescript(
            """
            ALTER TABLE platform_task_credentials
              ADD COLUMN allowed_operations_json TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE platform_task_credentials
              ADD COLUMN allowed_actions_digest TEXT;
            ALTER TABLE platform_task_credentials
              ADD COLUMN budget_digest TEXT;
            ALTER TABLE platform_task_credentials
              ADD COLUMN allowed_network_hosts_json TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE platform_task_credentials
              ADD COLUMN model_access INTEGER NOT NULL DEFAULT 1;
            ALTER TABLE platform_task_credentials
              ADD COLUMN file_access INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE platform_task_credentials
              ADD COLUMN connector_access INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE platform_task_credentials
              ADD COLUMN readable_host_objects_json TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE platform_task_credentials
              ADD COLUMN writable_host_operations_json TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE platform_task_credentials
              ADD COLUMN permission_required_actions_json TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE platform_task_credentials
              ADD COLUMN max_write_count INTEGER NOT NULL DEFAULT 1000000;
            ALTER TABLE platform_task_credentials
              ADD COLUMN max_payload_bytes INTEGER NOT NULL DEFAULT 104857600;
            ALTER TABLE platform_task_credentials
              ADD COLUMN compensation_actions_json TEXT NOT NULL DEFAULT '[]';
            ALTER TABLE platform_task_credentials
              ADD COLUMN max_report_evidence_rounds INTEGER NOT NULL DEFAULT 100;
            ALTER TABLE platform_task_credentials
              ADD COLUMN stable_hidden_runs INTEGER NOT NULL DEFAULT 1;

            CREATE TRIGGER platform_task_credential_policy_no_update
              BEFORE UPDATE OF allowed_operations_json,allowed_actions_digest,budget_digest,
                allowed_network_hosts_json,model_access,file_access,connector_access,
                readable_host_objects_json,writable_host_operations_json,
                permission_required_actions_json,max_write_count,max_payload_bytes,
                compensation_actions_json,max_report_evidence_rounds,stable_hidden_runs
              ON platform_task_credentials
              BEGIN SELECT RAISE(ABORT, 'platform task credential policy is immutable'); END;
            """
        )
        conn.execute(
            "INSERT INTO platform_blackbox_auth_schema(version,applied_at) VALUES (?,?)",
            (4, self._now().isoformat()),
        )

    def _secure_database_files(self) -> None:
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    async def issue_credential(
        self,
        grant: TaskCredentialGrant,
        *,
        idempotency_key: str | None = None,
        prepared_access_token: SecretStr | None = None,
        credential_id: UUID | None = None,
    ) -> IssuedTaskCredential:
        prepared = (
            prepared_access_token.get_secret_value()
            if prepared_access_token is not None
            else None
        )
        async with self._lock:
            return await asyncio.to_thread(
                self._issue_credential_sync,
                grant,
                idempotency_key,
                prepared,
                credential_id,
            )

    def _issue_credential_sync(
        self,
        grant: TaskCredentialGrant,
        idempotency_key: str | None,
        prepared_access_token: str | None,
        credential_id: UUID | None,
    ) -> IssuedTaskCredential:
        now = self._now()
        if grant.expires_at <= now:
            raise ValueError("credential expires_at must be in the future")
        prepared_values = (
            idempotency_key is not None,
            prepared_access_token is not None,
            credential_id is not None,
        )
        if any(prepared_values) and not all(prepared_values):
            raise ValueError(
                "idempotent credential issuance requires key, token, and credential_id"
            )
        if idempotency_key is not None and (
            not 16 <= len(idempotency_key) <= 128
            or _ISSUE_KEY_PATTERN.fullmatch(idempotency_key) is None
        ):
            raise ValueError("invalid credential issuance idempotency key")

        credential_id = credential_id or uuid4()
        credential_ref = f"platform-task-credential:{credential_id}"
        access_token = prepared_access_token or (
            f"lpt_{credential_id.hex}_{secrets.token_urlsafe(32)}"
        )
        token_match = _TOKEN_PATTERN.fullmatch(access_token)
        if token_match is None or token_match.group(1) != credential_id.hex:
            raise ValueError("prepared task token is not bound to credential_id")
        salt = secrets.token_bytes(16)
        token_digest = _derive_token_digest(access_token, salt)
        scopes = sorted(scope.value for scope in grant.scopes)
        applications = sorted({str(application_id) for application_id in grant.application_ids})
        allowed_operations = sorted(operation.value for operation in grant.allowed_operations)
        allowed_network_hosts = sorted(host.casefold() for host in grant.allowed_network_hosts)
        readable_host_objects = sorted(grant.readable_host_objects)
        writable_host_operations = sorted(grant.writable_host_operations)
        permission_required_actions = sorted(grant.permission_required_actions)
        compensation_actions = sorted(grant.compensation_actions)
        issue_payload_digest = _json_digest(
            {
                "credential_id": str(credential_id),
                "assignment_id": str(grant.assignment_id),
                "session_id": str(grant.session_id),
                "scopes": scopes,
                "application_ids": applications,
                "allowed_operations": allowed_operations,
                "allowed_actions_digest": grant.allowed_actions_digest,
                "budget_digest": grant.budget_digest,
                "allowed_network_hosts": allowed_network_hosts,
                "model_access": grant.model_access,
                "file_access": grant.file_access,
                "connector_access": grant.connector_access,
                "readable_host_objects": readable_host_objects,
                "writable_host_operations": writable_host_operations,
                "permission_required_actions": permission_required_actions,
                "max_write_count": grant.max_write_count,
                "max_payload_bytes": grant.max_payload_bytes,
                "compensation_actions": compensation_actions,
                "max_report_evidence_rounds": grant.max_report_evidence_rounds,
                "stable_hidden_runs": grant.stable_hidden_runs,
                "expires_at": grant.expires_at.isoformat(),
            }
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = None
            if idempotency_key is not None:
                existing = conn.execute(
                    """
                    SELECT * FROM platform_task_credentials
                    WHERE issue_idempotency_key=?
                    """,
                    (idempotency_key,),
                ).fetchone()
            if existing is not None:
                supplied_digest = _derive_token_digest(
                    access_token,
                    bytes.fromhex(existing["token_salt_hex"]),
                )
                if (
                    existing["id"] != str(credential_id)
                    or existing["credential_ref"] != credential_ref
                    or existing["issue_payload_digest"] != issue_payload_digest
                    or not hmac.compare_digest(
                        supplied_digest,
                        str(existing["token_digest"]),
                    )
                ):
                    raise PlatformBlackboxIdempotencyConflict(
                        "credential issuance key was reused with a different grant"
                    )
            else:
                try:
                    conn.execute(
                        """
                        INSERT INTO platform_task_credentials(
                          id,credential_ref,assignment_id,session_id,token_salt_hex,
                          token_digest,scopes_json,expires_at,created_at,updated_at,
                          issue_idempotency_key,issue_payload_digest,
                          allowed_operations_json,allowed_actions_digest,budget_digest,
                          allowed_network_hosts_json,model_access,file_access,connector_access,
                          readable_host_objects_json,writable_host_operations_json,
                          permission_required_actions_json,max_write_count,max_payload_bytes,
                          compensation_actions_json,max_report_evidence_rounds,
                          stable_hidden_runs
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            str(credential_id),
                            credential_ref,
                            str(grant.assignment_id),
                            str(grant.session_id),
                            salt.hex(),
                            token_digest,
                            _canonical_json(scopes),
                            grant.expires_at.isoformat(),
                            now.isoformat(),
                            now.isoformat(),
                            idempotency_key,
                            issue_payload_digest if idempotency_key is not None else None,
                            _canonical_json(allowed_operations),
                            grant.allowed_actions_digest,
                            grant.budget_digest,
                            _canonical_json(allowed_network_hosts),
                            int(grant.model_access),
                            int(grant.file_access),
                            int(grant.connector_access),
                            _canonical_json(readable_host_objects),
                            _canonical_json(writable_host_operations),
                            _canonical_json(permission_required_actions),
                            grant.max_write_count,
                            grant.max_payload_bytes,
                            _canonical_json(compensation_actions),
                            grant.max_report_evidence_rounds,
                            grant.stable_hidden_runs,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise PlatformBlackboxIdempotencyConflict(
                        "credential issuance identifiers conflict with an existing grant"
                    ) from error
                conn.executemany(
                    """
                    INSERT INTO platform_task_credential_applications(
                      credential_id,application_id,granted_at
                    ) VALUES (?,?,?)
                    """,
                    [
                        (str(credential_id), application_id, now.isoformat())
                        for application_id in applications
                    ],
                )
                self._append_security_event_conn(
                    conn,
                    event_type="credential.issued",
                    credential_id=str(credential_id),
                    assignment_id=str(grant.assignment_id),
                    session_id=str(grant.session_id),
                    details={
                        "scopes": scopes,
                        "application_ids": applications,
                        "allowed_operations": allowed_operations,
                        "allowed_actions_digest": grant.allowed_actions_digest,
                        "budget_digest": grant.budget_digest,
                        "max_write_count": grant.max_write_count,
                        "max_payload_bytes": grant.max_payload_bytes,
                    },
                    created_at=now,
                )
            row = self._require_credential_conn(conn, credential_ref)
            record = self._credential_from_row(conn, row)
        self._secure_database_files()
        return IssuedTaskCredential(
            credential=record,
            access_token=SecretStr(access_token),
        )

    async def get_credential(self, credential_ref: str) -> TaskCredentialRecord:
        return await asyncio.to_thread(self._get_credential_sync, credential_ref)

    def _get_credential_sync(self, credential_ref: str) -> TaskCredentialRecord:
        with self._connect() as conn:
            row = self._require_credential_conn(conn, credential_ref)
            return self._credential_from_row(conn, row)

    async def authenticate_credential(
        self,
        access_token: str | SecretStr,
    ) -> TaskCredentialRecord:
        """Resolve a valid bearer without exposing its verifier or recording a request.

        The HTTP facade uses this for scope-filtered contract construction and for
        distinguishing a valid task credential on a forbidden legacy endpoint.  An
        actual public operation must still call :meth:`authorize_request` so it is
        reserved, payload-bound, and audited.
        """

        token = (
            access_token.get_secret_value() if isinstance(access_token, SecretStr) else access_token
        )
        record, failure = await asyncio.to_thread(
            self._authenticate_credential_sync,
            token,
        )
        if failure is not None:
            failure.raise_error()
        if record is None:  # pragma: no cover - paired with failure above
            raise PlatformBlackboxStoreError("credential verification produced no record")
        return record

    def _authenticate_credential_sync(
        self,
        access_token: str,
    ) -> tuple[TaskCredentialRecord | None, _Failure | None]:
        with self._connect() as conn:
            row, failure = self._authenticate_conn(conn, access_token, self._now())
            if row is None:
                return None, failure
            if failure is not None:
                return None, failure
            return self._credential_from_row(conn, row), None

    async def revoke_credential(
        self,
        credential_ref: str,
        *,
        reason: str,
    ) -> TaskCredentialRecord:
        if not reason or len(reason) > 1_000:
            raise ValueError("revoke reason must contain 1-1000 characters")
        async with self._lock:
            return await asyncio.to_thread(
                self._revoke_credential_sync,
                credential_ref,
                reason,
            )

    def _revoke_credential_sync(
        self,
        credential_ref: str,
        reason: str,
    ) -> TaskCredentialRecord:
        now = self._now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require_credential_conn(conn, credential_ref)
            if row["revoked_at"] is None:
                conn.execute(
                    """
                    UPDATE platform_task_credentials
                    SET revoked_at=?,revoke_reason=?,updated_at=? WHERE id=?
                    """,
                    (now.isoformat(), reason, now.isoformat(), row["id"]),
                )
                self._append_security_event_conn(
                    conn,
                    event_type="credential.revoked",
                    credential_id=str(row["id"]),
                    assignment_id=str(row["assignment_id"]),
                    session_id=str(row["session_id"]),
                    details={"reason": reason},
                    created_at=now,
                )
                row = self._require_credential_conn(conn, credential_ref)
            return self._credential_from_row(conn, row)

    async def authorize_request(
        self,
        access_token: str | SecretStr,
        request: BlackboxAuthorizationRequest,
    ) -> BlackboxAuthorizationDecision:
        token = (
            access_token.get_secret_value() if isinstance(access_token, SecretStr) else access_token
        )
        async with self._lock:
            decision, failure = await asyncio.to_thread(
                self._authorize_request_sync,
                token,
                request,
            )
        if failure is not None:
            failure.raise_error()
        if decision is None:  # pragma: no cover - defensive invariant
            raise PlatformBlackboxStoreError("authorization produced no decision")
        return decision

    def _authorize_request_sync(
        self,
        access_token: str,
        request: BlackboxAuthorizationRequest,
    ) -> tuple[BlackboxAuthorizationDecision | None, _Failure | None]:
        now = self._now()
        payload_json = _canonical_json(request.payload)
        payload_digest = _json_digest(request.payload)
        required_scope = OPERATION_SCOPES[request.operation]
        fingerprint = _idempotency_fingerprint(
            request,
            required_scope=required_scope,
            payload_digest=payload_digest,
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            credential, failure = self._authenticate_conn(conn, access_token, now)
            if failure is None and credential is not None:
                if credential["assignment_id"] != str(request.assignment_id) or credential[
                    "session_id"
                ] != str(request.session_id):
                    failure = _Failure(
                        PlatformBlackboxAuthorizationError,
                        "credential is not bound to this assignment and session",
                    )
                elif required_scope.value not in set(json.loads(credential["scopes_json"])):
                    failure = _Failure(
                        PlatformBlackboxScopeDenied,
                        f"credential does not grant {required_scope.value}",
                    )
                elif request.operation.value not in set(
                    json.loads(credential["allowed_operations_json"])
                ):
                    failure = _Failure(
                        PlatformBlackboxOperationDenied,
                        "operation is outside the task credential policy",
                    )
            existing = (
                conn.execute(
                    """
                    SELECT * FROM platform_blackbox_requests
                    WHERE assignment_id=? AND idempotency_key=?
                    """,
                    (str(request.assignment_id), request.idempotency_key),
                ).fetchone()
                if failure is None
                else None
            )
            if failure is None and credential is not None:
                if (
                    request.operation is PlatformBlackboxOperation.application_create
                    and existing is None
                ):
                    existing_application = conn.execute(
                        """
                        SELECT 1 FROM platform_task_credential_applications
                        WHERE credential_id=? LIMIT 1
                        """,
                        (credential["id"],),
                    ).fetchone()
                    pending_application_create = conn.execute(
                        """
                        SELECT 1 FROM platform_blackbox_requests
                        WHERE credential_id=? AND operation=? AND state='reserved'
                        LIMIT 1
                        """,
                        (
                            credential["id"],
                            PlatformBlackboxOperation.application_create.value,
                        ),
                    ).fetchone()
                    if existing_application is not None:
                        failure = _Failure(
                            PlatformBlackboxApplicationDenied,
                            "task credential is already bound to its application",
                        )
                    elif pending_application_create is not None:
                        failure = _Failure(
                            PlatformBlackboxApplicationDenied,
                            "task credential already has an application creation in progress",
                        )
                elif request.operation in _APPLICATION_SCOPED_OPERATIONS:
                    allowed = conn.execute(
                        """
                        SELECT 1 FROM platform_task_credential_applications
                        WHERE credential_id=? AND application_id=?
                        """,
                        (credential["id"], str(request.application_id)),
                    ).fetchone()
                    if allowed is None:
                        failure = _Failure(
                            PlatformBlackboxApplicationDenied,
                            "application is outside the task credential whitelist",
                        )
            if failure is not None:
                self._append_audit_conn(
                    conn,
                    request=request,
                    event_type=BlackboxAuditEventType.denied,
                    outcome="denied",
                    credential_id=str(credential["id"]) if credential is not None else None,
                    authorization_id=None,
                    required_scope=required_scope,
                    payload_digest=payload_digest,
                    reason_code=failure.code,
                    details={},
                    created_at=now,
                )
                return None, failure

            if credential is None:  # pragma: no cover - paired with failure above
                raise PlatformBlackboxStoreError("credential verification invariant failed")
            if existing is not None:
                if not hmac.compare_digest(existing["request_fingerprint"], fingerprint):
                    failure = _Failure(
                        PlatformBlackboxIdempotencyConflict,
                        "idempotency key is already bound to a different request payload",
                    )
                    self._append_audit_conn(
                        conn,
                        request=request,
                        event_type=BlackboxAuditEventType.denied,
                        outcome="denied",
                        credential_id=str(credential["id"]),
                        authorization_id=str(existing["authorization_id"]),
                        required_scope=required_scope,
                        payload_digest=payload_digest,
                        reason_code=failure.code,
                        details={"original_request_id": existing["request_id"]},
                        created_at=now,
                    )
                    return None, failure
                audit = self._append_audit_conn(
                    conn,
                    request=request,
                    event_type=BlackboxAuditEventType.replayed,
                    outcome="replayed",
                    credential_id=str(credential["id"]),
                    authorization_id=str(existing["authorization_id"]),
                    required_scope=required_scope,
                    payload_digest=payload_digest,
                    reason_code=None,
                    details={"original_request_id": existing["request_id"]},
                    created_at=now,
                )
                return self._decision_from_request_row(
                    existing, audit.event_id, replayed=True
                ), None

            authorization_id = uuid4()
            conn.execute(
                """
                INSERT INTO platform_blackbox_requests(
                  authorization_id,credential_id,idempotency_key,request_id,assignment_id,
                  session_id,tool_call_id,application_id,operation,required_scope,
                  contract_digest,payload_digest,payload_json,request_fingerprint,state,
                  created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'reserved',?,?)
                """,
                (
                    str(authorization_id),
                    credential["id"],
                    request.idempotency_key,
                    str(request.request_id),
                    str(request.assignment_id),
                    str(request.session_id),
                    request.tool_call_id,
                    str(request.application_id),
                    request.operation.value,
                    required_scope.value,
                    request.contract_digest,
                    payload_digest,
                    payload_json,
                    fingerprint,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            audit = self._append_audit_conn(
                conn,
                request=request,
                event_type=BlackboxAuditEventType.authorized,
                outcome="authorized",
                credential_id=str(credential["id"]),
                authorization_id=str(authorization_id),
                required_scope=required_scope,
                payload_digest=payload_digest,
                reason_code=None,
                details={},
                created_at=now,
            )
            stored = self._require_request_conn(conn, str(authorization_id))
            return self._decision_from_request_row(stored, audit.event_id, replayed=False), None

    def _authenticate_conn(
        self,
        conn: sqlite3.Connection,
        access_token: str,
        now: datetime,
    ) -> tuple[sqlite3.Row | None, _Failure | None]:
        match = _TOKEN_PATTERN.fullmatch(access_token)
        credential_id: str | None = None
        if match is not None:
            credential_id = str(UUID(hex=match.group(1)))
        row = (
            conn.execute(
                "SELECT * FROM platform_task_credentials WHERE id=?",
                (credential_id,),
            ).fetchone()
            if credential_id is not None
            else None
        )
        salt = bytes.fromhex(row["token_salt_hex"]) if row is not None else _DUMMY_SALT
        expected = row["token_digest"] if row is not None else _DUMMY_TOKEN_DIGEST
        candidate = _derive_token_digest(access_token[:512], salt)
        valid = hmac.compare_digest(candidate, expected)
        if row is None or not valid:
            return None, _Failure(
                PlatformBlackboxAuthenticationError,
                "platform task credential is invalid",
            )
        if row["revoked_at"] is not None:
            return row, _Failure(
                PlatformBlackboxCredentialRevoked,
                "platform task credential is revoked",
            )
        if _parse_utc(row["expires_at"]) <= now:
            return row, _Failure(
                PlatformBlackboxCredentialExpired,
                "platform task credential is expired",
            )
        return row, None

    async def complete_request(
        self,
        authorization_id: UUID,
        *,
        status_code: int,
        result: Mapping[str, Any],
        created_application_id: UUID | None = None,
        persist_result: bool = True,
    ) -> BlackboxAuthorizationDecision:
        if status_code < 100 or status_code > 599:
            raise ValueError("status_code must be between 100 and 599")
        result_dict = dict(result)
        _canonical_json(result_dict)
        async with self._lock:
            decision, failure = await asyncio.to_thread(
                self._complete_request_sync,
                str(authorization_id),
                status_code,
                result_dict,
                str(created_application_id) if created_application_id is not None else None,
                persist_result,
            )
        if failure is not None:
            failure.raise_error()
        if decision is None:  # pragma: no cover - defensive invariant
            raise PlatformBlackboxStoreError("request completion produced no decision")
        return decision

    def _complete_request_sync(
        self,
        authorization_id: str,
        status_code: int,
        result: dict[str, Any],
        created_application_id: str | None,
        persist_result: bool,
    ) -> tuple[BlackboxAuthorizationDecision | None, _Failure | None]:
        now = self._now()
        response_json = _canonical_json(result) if persist_result else None
        response_digest = _json_digest(result)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = self._require_request_conn(conn, authorization_id)
            request = self._request_model_from_row(row)
            if row["state"] == BlackboxRequestState.completed.value:
                same_result = (
                    row["status_code"] == status_code
                    and hmac.compare_digest(str(row["response_digest"]), response_digest)
                    and row["created_application_id"] == created_application_id
                )
                if not same_result:
                    failure = _Failure(
                        PlatformBlackboxRequestConflict,
                        "request completion is already bound to a different response",
                    )
                    self._append_audit_conn(
                        conn,
                        request=request,
                        event_type=BlackboxAuditEventType.denied,
                        outcome="denied",
                        credential_id=str(row["credential_id"]),
                        authorization_id=authorization_id,
                        required_scope=PlatformBlackboxScope(row["required_scope"]),
                        payload_digest=str(row["payload_digest"]),
                        reason_code=failure.code,
                        details={"phase": "complete"},
                        created_at=now,
                    )
                    return None, failure
                audit = self._append_audit_conn(
                    conn,
                    request=request,
                    event_type=BlackboxAuditEventType.completion_replayed,
                    outcome="replayed",
                    credential_id=str(row["credential_id"]),
                    authorization_id=authorization_id,
                    required_scope=PlatformBlackboxScope(row["required_scope"]),
                    payload_digest=str(row["payload_digest"]),
                    reason_code=None,
                    details={"phase": "complete"},
                    created_at=now,
                )
                return self._decision_from_request_row(row, audit.event_id, replayed=True), None

            if created_application_id is not None:
                if row["operation"] != PlatformBlackboxOperation.application_create.value:
                    raise ValueError(
                        "created_application_id is only valid for platform_application_create"
                    )
                if status_code >= 400:
                    raise ValueError("a failed application create cannot grant an application")
                conn.execute(
                    """
                    INSERT OR IGNORE INTO platform_task_credential_applications(
                      credential_id,application_id,granted_at
                    ) VALUES (?,?,?)
                    """,
                    (row["credential_id"], created_application_id, now.isoformat()),
                )
                credential = conn.execute(
                    "SELECT * FROM platform_task_credentials WHERE id=?",
                    (row["credential_id"],),
                ).fetchone()
                self._append_security_event_conn(
                    conn,
                    event_type="credential.application_granted",
                    credential_id=str(row["credential_id"]),
                    assignment_id=str(row["assignment_id"]),
                    session_id=str(row["session_id"]),
                    details={"application_id": created_application_id},
                    created_at=now,
                )
                if credential is None:  # pragma: no cover - protected by foreign key
                    raise PlatformBlackboxStoreError("credential disappeared during completion")
            elif (
                row["operation"] == PlatformBlackboxOperation.application_create.value
                and status_code < 400
            ):
                raise ValueError(
                    "a successful platform_application_create must grant created_application_id"
                )
            conn.execute(
                """
                UPDATE platform_blackbox_requests
                SET state='completed',status_code=?,response_json=?,response_digest=?,
                  created_application_id=?,updated_at=?,completed_at=?
                WHERE authorization_id=? AND state='reserved'
                """,
                (
                    status_code,
                    response_json,
                    response_digest,
                    created_application_id,
                    now.isoformat(),
                    now.isoformat(),
                    authorization_id,
                ),
            )
            audit = self._append_audit_conn(
                conn,
                request=request,
                event_type=BlackboxAuditEventType.completed,
                outcome="completed",
                credential_id=str(row["credential_id"]),
                authorization_id=authorization_id,
                required_scope=PlatformBlackboxScope(row["required_scope"]),
                payload_digest=str(row["payload_digest"]),
                reason_code=None,
                details={
                    "status_code": status_code,
                    "response_digest": response_digest,
                    "created_application_id": created_application_id,
                },
                created_at=now,
            )
            updated = self._require_request_conn(conn, authorization_id)
            return self._decision_from_request_row(updated, audit.event_id, replayed=False), None

    async def list_audit(
        self,
        *,
        assignment_id: UUID | None = None,
        session_id: UUID | None = None,
        limit: int = 500,
    ) -> list[BlackboxAuditRecord]:
        return await asyncio.to_thread(
            self._list_audit_sync,
            str(assignment_id) if assignment_id is not None else None,
            str(session_id) if session_id is not None else None,
            max(1, min(limit, 1_000)),
        )

    def _list_audit_sync(
        self,
        assignment_id: str | None,
        session_id: str | None,
        limit: int,
    ) -> list[BlackboxAuditRecord]:
        clauses: list[str] = []
        values: list[Any] = []
        if assignment_id is not None:
            clauses.append("assignment_id=?")
            values.append(assignment_id)
        if session_id is not None:
            clauses.append("session_id=?")
            values.append(session_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM platform_blackbox_audit {where} ORDER BY seq LIMIT ?",  # noqa: S608
                values,
            ).fetchall()
        return [self._audit_from_row(row) for row in rows]

    async def export_assignment_snapshot(
        self,
        *,
        assignment_id: UUID,
        session_id: UUID,
    ) -> dict[str, Any]:
        """Export one complete, secret-free assignment audit snapshot.

        This route is server-internal.  It intentionally excludes token salts
        and verifier digests while preserving every request, audit event, and
        credential lifecycle record needed for independent run reconstruction.
        """

        async with self._lock:
            return await asyncio.to_thread(
                self._export_assignment_snapshot_sync,
                str(assignment_id),
                str(session_id),
            )

    def _export_assignment_snapshot_sync(
        self,
        assignment_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute("BEGIN")
            credentials = conn.execute(
                """
                SELECT id,credential_ref,assignment_id,session_id,scopes_json,
                       allowed_operations_json,allowed_actions_digest,budget_digest,
                       allowed_network_hosts_json,model_access,file_access,
                       connector_access,readable_host_objects_json,
                       writable_host_operations_json,permission_required_actions_json,
                       max_write_count,max_payload_bytes,compensation_actions_json,
                       max_report_evidence_rounds,stable_hidden_runs,
                       expires_at,revoked_at,revoke_reason,created_at,updated_at
                FROM platform_task_credentials
                WHERE assignment_id=? AND session_id=?
                ORDER BY created_at,id
                """,
                (assignment_id, session_id),
            ).fetchall()
            credential_ids = [str(row["id"]) for row in credentials]
            applications: list[sqlite3.Row] = []
            if credential_ids:
                placeholders = ",".join("?" for _ in credential_ids)
                applications = list(
                    conn.execute(
                        f"""
                        SELECT credential_id,application_id,granted_at
                        FROM platform_task_credential_applications
                        WHERE credential_id IN ({placeholders})
                        ORDER BY credential_id,application_id
                        """,  # noqa: S608
                        credential_ids,
                    ).fetchall()
                )
            requests = conn.execute(
                """
                SELECT authorization_id,credential_id,idempotency_key,request_id,
                       assignment_id,session_id,tool_call_id,application_id,
                       operation,required_scope,contract_digest,payload_digest,
                       payload_json,request_fingerprint,state,status_code,response_json,
                       response_digest,created_application_id,created_at,
                       updated_at,completed_at
                FROM platform_blackbox_requests
                WHERE assignment_id=? AND session_id=?
                ORDER BY created_at,authorization_id
                """,
                (assignment_id, session_id),
            ).fetchall()
            audit_rows = conn.execute(
                """
                SELECT * FROM platform_blackbox_audit
                WHERE assignment_id=? AND session_id=?
                ORDER BY seq
                """,
                (assignment_id, session_id),
            ).fetchall()
            security_rows = conn.execute(
                """
                SELECT * FROM platform_task_credential_security_events
                WHERE assignment_id=? AND session_id=?
                ORDER BY seq
                """,
                (assignment_id, session_id),
            ).fetchall()
        if (
            len(requests) > 10_000
            or len(audit_rows) > 50_000
            or len(security_rows) > 10_000
        ):
            raise PlatformBlackboxStoreError(
                "formal assignment audit exceeds the complete export limit"
            )

        def decoded_request(row: sqlite3.Row) -> dict[str, Any]:
            value = dict(row)
            encoded_payload = value.pop("payload_json")
            encoded = value.pop("response_json")
            value["payload"] = (
                json.loads(str(encoded_payload))
                if encoded_payload is not None
                else None
            )
            value["response"] = (
                json.loads(str(encoded)) if encoded is not None else None
            )
            return value

        audits = [
            self._audit_from_row(row).model_dump(mode="json", exclude_none=True)
            for row in audit_rows
        ]
        security_events = [
            self._security_event_from_row(row).model_dump(
                mode="json",
                exclude_none=True,
            )
            for row in security_rows
        ]
        return {
            "schema_version": "1.0",
            "assignment_id": assignment_id,
            "session_id": session_id,
            "complete": all(row["payload_json"] is not None for row in requests),
            "credentials": [
                {
                    **{
                        key: value
                        for key, value in dict(row).items()
                        if key
                        not in {
                            "scopes_json",
                            "allowed_operations_json",
                            "allowed_network_hosts_json",
                            "readable_host_objects_json",
                            "writable_host_operations_json",
                            "permission_required_actions_json",
                            "compensation_actions_json",
                            "model_access",
                            "file_access",
                            "connector_access",
                        }
                    },
                    "scopes": json.loads(str(row["scopes_json"])),
                    "allowed_operations": json.loads(
                        str(row["allowed_operations_json"])
                    ),
                    "allowed_network_hosts": json.loads(
                        str(row["allowed_network_hosts_json"])
                    ),
                    "model_access": bool(row["model_access"]),
                    "file_access": bool(row["file_access"]),
                    "connector_access": bool(row["connector_access"]),
                    "readable_host_objects": json.loads(
                        str(row["readable_host_objects_json"])
                    ),
                    "writable_host_operations": json.loads(
                        str(row["writable_host_operations_json"])
                    ),
                    "permission_required_actions": json.loads(
                        str(row["permission_required_actions_json"])
                    ),
                    "compensation_actions": json.loads(
                        str(row["compensation_actions_json"])
                    ),
                }
                for row in credentials
            ],
            "credential_applications": [dict(row) for row in applications],
            "requests": [decoded_request(row) for row in requests],
            "audit": audits,
            "security_events": security_events,
            "counts": {
                "credentials": len(credentials),
                "credential_applications": len(applications),
                "requests": len(requests),
                "audit": len(audit_rows),
                "security_events": len(security_rows),
            },
            "audit_min_seq": (
                int(audit_rows[0]["seq"]) if audit_rows else None
            ),
            "audit_max_seq": (
                int(audit_rows[-1]["seq"]) if audit_rows else None
            ),
            "security_min_seq": (
                int(security_rows[0]["seq"]) if security_rows else None
            ),
            "security_max_seq": (
                int(security_rows[-1]["seq"]) if security_rows else None
            ),
        }

    async def list_security_events(self, *, limit: int = 500) -> list[CredentialSecurityEvent]:
        return await asyncio.to_thread(
            self._list_security_events_sync,
            max(1, min(limit, 1_000)),
        )

    def _list_security_events_sync(self, limit: int) -> list[CredentialSecurityEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM platform_task_credential_security_events
                ORDER BY seq LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._security_event_from_row(row) for row in rows]

    def _require_credential_conn(
        self,
        conn: sqlite3.Connection,
        credential_ref: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM platform_task_credentials WHERE credential_ref=?",
            (credential_ref,),
        ).fetchone()
        if row is None:
            raise PlatformBlackboxNotFound(f"task credential not found: {credential_ref}")
        return row

    def _require_request_conn(
        self,
        conn: sqlite3.Connection,
        authorization_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM platform_blackbox_requests WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
        if row is None:
            raise PlatformBlackboxNotFound(
                f"blackbox request authorization not found: {authorization_id}"
            )
        return row

    def _credential_from_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> TaskCredentialRecord:
        applications = conn.execute(
            """
            SELECT application_id FROM platform_task_credential_applications
            WHERE credential_id=? ORDER BY application_id
            """,
            (row["id"],),
        ).fetchall()
        return TaskCredentialRecord(
            credential_id=row["id"],
            credential_ref=row["credential_ref"],
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            scopes=json.loads(row["scopes_json"]),
            application_ids=[item["application_id"] for item in applications],
            allowed_operations=json.loads(row["allowed_operations_json"]),
            allowed_actions_digest=row["allowed_actions_digest"],
            budget_digest=row["budget_digest"],
            allowed_network_hosts=json.loads(row["allowed_network_hosts_json"]),
            model_access=bool(row["model_access"]),
            file_access=bool(row["file_access"]),
            connector_access=bool(row["connector_access"]),
            readable_host_objects=json.loads(row["readable_host_objects_json"]),
            writable_host_operations=json.loads(
                row["writable_host_operations_json"]
            ),
            permission_required_actions=json.loads(
                row["permission_required_actions_json"]
            ),
            max_write_count=int(row["max_write_count"]),
            max_payload_bytes=int(row["max_payload_bytes"]),
            compensation_actions=json.loads(row["compensation_actions_json"]),
            max_report_evidence_rounds=int(row["max_report_evidence_rounds"]),
            stable_hidden_runs=int(row["stable_hidden_runs"]),
            expires_at=_parse_utc(row["expires_at"]),
            revoked_at=_parse_utc(row["revoked_at"]) if row["revoked_at"] else None,
            revoke_reason=row["revoke_reason"],
            created_at=_parse_utc(row["created_at"]),
            updated_at=_parse_utc(row["updated_at"]),
        )

    def _request_model_from_row(self, row: sqlite3.Row) -> BlackboxAuthorizationRequest:
        return BlackboxAuthorizationRequest(
            request_id=row["request_id"],
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            tool_call_id=row["tool_call_id"],
            idempotency_key=row["idempotency_key"],
            application_id=row["application_id"],
            operation=row["operation"],
            contract_digest=row["contract_digest"],
            payload={},
        )

    def _decision_from_request_row(
        self,
        row: sqlite3.Row,
        audit_event_id: UUID,
        *,
        replayed: bool,
    ) -> BlackboxAuthorizationDecision:
        return BlackboxAuthorizationDecision(
            authorization_id=row["authorization_id"],
            credential_id=row["credential_id"],
            request_id=row["request_id"],
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            tool_call_id=row["tool_call_id"],
            idempotency_key=row["idempotency_key"],
            application_id=row["application_id"],
            operation=row["operation"],
            required_scope=row["required_scope"],
            contract_digest=row["contract_digest"],
            payload_digest=row["payload_digest"],
            state=row["state"],
            replayed=replayed,
            status_code=row["status_code"],
            result=json.loads(row["response_json"]) if row["response_json"] else None,
            audit_event_id=audit_event_id,
            created_at=_parse_utc(row["created_at"]),
            completed_at=_parse_utc(row["completed_at"]) if row["completed_at"] else None,
        )

    def _append_audit_conn(
        self,
        conn: sqlite3.Connection,
        *,
        request: BlackboxAuthorizationRequest,
        event_type: BlackboxAuditEventType,
        outcome: Literal["authorized", "replayed", "completed", "denied"],
        credential_id: str | None,
        authorization_id: str | None,
        required_scope: PlatformBlackboxScope,
        payload_digest: str,
        reason_code: str | None,
        details: Mapping[str, Any],
        created_at: datetime,
    ) -> BlackboxAuditRecord:
        event_id = uuid4()
        conn.execute(
            """
            INSERT INTO platform_blackbox_audit(
              event_id,event_type,outcome,credential_id,authorization_id,assignment_id,
              session_id,tool_call_id,request_id,idempotency_key,application_id,operation,
              required_scope,contract_digest,payload_digest,reason_code,details_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(event_id),
                event_type.value,
                outcome,
                credential_id,
                authorization_id,
                str(request.assignment_id),
                str(request.session_id),
                request.tool_call_id,
                str(request.request_id),
                request.idempotency_key,
                str(request.application_id),
                request.operation.value,
                required_scope.value,
                request.contract_digest,
                payload_digest,
                reason_code,
                _canonical_json(dict(details)),
                created_at.isoformat(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM platform_blackbox_audit WHERE event_id=?",
            (str(event_id),),
        ).fetchone()
        if row is None:  # pragma: no cover - immediately inserted
            raise PlatformBlackboxStoreError("audit event insert disappeared")
        return self._audit_from_row(row)

    def _append_security_event_conn(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        credential_id: str,
        assignment_id: str,
        session_id: str,
        details: Mapping[str, Any],
        created_at: datetime,
    ) -> None:
        conn.execute(
            """
            INSERT INTO platform_task_credential_security_events(
              event_id,event_type,credential_id,assignment_id,session_id,details_json,created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            (
                str(uuid4()),
                event_type,
                credential_id,
                assignment_id,
                session_id,
                _canonical_json(dict(details)),
                created_at.isoformat(),
            ),
        )

    @staticmethod
    def _audit_from_row(row: sqlite3.Row) -> BlackboxAuditRecord:
        return BlackboxAuditRecord(
            seq=row["seq"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            outcome=row["outcome"],
            credential_id=row["credential_id"],
            authorization_id=row["authorization_id"],
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            tool_call_id=row["tool_call_id"],
            request_id=row["request_id"],
            idempotency_key=row["idempotency_key"],
            application_id=row["application_id"],
            operation=row["operation"],
            required_scope=row["required_scope"],
            contract_digest=row["contract_digest"],
            payload_digest=row["payload_digest"],
            reason_code=row["reason_code"],
            details=json.loads(row["details_json"]),
            created_at=_parse_utc(row["created_at"]),
        )

    @staticmethod
    def _security_event_from_row(row: sqlite3.Row) -> CredentialSecurityEvent:
        return CredentialSecurityEvent(
            seq=row["seq"],
            event_id=row["event_id"],
            event_type=row["event_type"],
            credential_id=row["credential_id"],
            assignment_id=row["assignment_id"],
            session_id=row["session_id"],
            details=json.loads(row["details_json"]),
            created_at=_parse_utc(row["created_at"]),
        )
