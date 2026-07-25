"""Durable storage for platform-neutral collaborative development.

The store owns a separate SQLite schema and intentionally has no dependency on
workflow storage, Builder services, formal task packages, or verification
oracles.  Every state-changing operation is transactional, idempotent, and
compare-and-set where it advances mutable state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, Literal, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .collaboration_models import (
    sanitize_collaboration_payload,
    validate_collaboration_payload_safety,
)
from .collaborative_development_models import (
    AgentRole,
    ApprovalMode,
    AssignmentStatus,
    CommandReceipt,
    DevelopmentAssignment,
    DevelopmentBudget,
    DevelopmentEvent,
    DevelopmentLease,
    DevelopmentResult,
    DevelopmentTaskRole,
    DevelopmentInvocationFence,
    DevelopmentInvocationFenceResult,
    DevelopmentWorkItem,
    DispatchOutboxClaim,
    DispatchOutboxItem,
    ExecutionMode,
    IdempotencyKey,
    LeaseStatus,
    LiliesReview,
    OutboxStatus,
    ReaderCursor,
    ReviewVerdict,
    WorkItemStatus,
    WorkspaceGrant,
    utc_now,
)


SCHEMA_VERSION = 1
ModelT = TypeVar("ModelT", bound=BaseModel)
_FREE_FORM_SECRET_PATTERN = re.compile(
    r"(?:"
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    r"|"
    r"\b(?:access[_-]?token|refresh[_-]?token|api[_-]?token|token)"
    r"\s*[:=]\s*[^\s,;]{8,}"
    r")",
    re.IGNORECASE,
)


class CollaborativeDevelopmentStorageError(RuntimeError):
    pass


class CollaborativeDevelopmentNotFound(CollaborativeDevelopmentStorageError):
    pass


class CollaborativeDevelopmentConflict(CollaborativeDevelopmentStorageError):
    pass


class CollaborativeDevelopmentInvalidState(CollaborativeDevelopmentConflict):
    pass


class CollaborativeDevelopmentAuthorizationError(CollaborativeDevelopmentStorageError):
    pass


class CollaborativeDevelopmentBudgetExceeded(CollaborativeDevelopmentStorageError):
    pass


class DevelopmentToolUsageRecord(BaseModel):
    """One server-owned pre-execution reservation for an actual tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    reservation_id: UUID
    assignment_id: UUID
    actor_role: AgentRole
    usage_id: str = Field(min_length=1, max_length=240)
    tool_name: Literal[
        "workspace_search",
        "workspace_read",
        "workspace_write",
        "workspace_patch",
        "process_run",
        "git_status",
        "git_diff",
    ]
    request_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    tool_calls: Literal[1] = 1
    commands: Literal[0, 1]
    command_argv: tuple[str, ...] | None = None
    command_cwd: str | None = None
    status: Literal["reserved", "completed"] = "reserved"
    response_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    output_digest: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    consumer_type: Literal["result", "review"] | None = None
    consumer_id: UUID | None = None
    reserved_at: datetime
    completed_at: datetime | None = None

    @field_validator("reserved_at", "completed_at")
    @classmethod
    def usage_timestamps_are_utc(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return _as_utc(value) if value is not None else None

    @field_validator("usage_id")
    @classmethod
    def usage_id_is_safe(cls, value: str) -> str:
        candidate = value.strip()
        if not candidate or "\x00" in candidate or "\r" in candidate or "\n" in candidate:
            raise ValueError("usage id must be a bounded opaque identifier")
        return candidate

    @model_validator(mode="after")
    def usage_shape_is_consistent(self) -> DevelopmentToolUsageRecord:
        is_command = self.commands == 1
        if is_command != (self.command_argv is not None):
            raise ValueError("command usage requires command argv")
        if is_command != (self.command_cwd is not None):
            raise ValueError("command usage requires command cwd")
        if self.status == "reserved":
            if (
                self.completed_at is not None
                or self.response_digest is not None
                or self.output_digest is not None
            ):
                raise ValueError("reserved usage cannot contain completion evidence")
        elif self.completed_at is None or self.response_digest is None:
            raise ValueError("completed usage requires completion evidence")
        if (self.consumer_type is None) != (self.consumer_id is None):
            raise ValueError("usage consumer type and id must be set together")
        return self


class TrustedProviderCostReceipt(BaseModel):
    """A provider/control-plane receipt; role adapters cannot self-report it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    receipt_id: str = Field(min_length=1, max_length=500)
    reservation_id: UUID
    assignment_id: UUID
    provider: str = Field(min_length=1, max_length=200)
    provider_request_id: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=500)
    cost_usd: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    issued_at: datetime

    @field_validator("issued_at")
    @classmethod
    def provider_receipt_time_is_utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class TrustedProviderCostAuthorization(BaseModel):
    """Control-plane attestation for a paid provider request's upper bound."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    reservation_id: UUID
    assignment_id: UUID
    provider: str = Field(min_length=1, max_length=200)
    provider_request_id: str = Field(min_length=1, max_length=500)
    model: str = Field(min_length=1, max_length=500)
    worst_case_cost_usd: float = Field(
        gt=0,
        le=1_000_000,
        allow_inf_nan=False,
    )
    evidence_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    authorized_at: datetime

    @field_validator("authorized_at")
    @classmethod
    def provider_authorization_time_is_utc(cls, value: datetime) -> datetime:
        return _as_utc(value)


class TrustedProviderCostReservation(BaseModel):
    """Durable authorization and optional trusted settlement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    cost_cap: TrustedProviderCostAuthorization
    status: Literal["reserved", "settled"] = "reserved"
    receipt: TrustedProviderCostReceipt | None = None
    reserved_at: datetime
    settled_at: datetime | None = None

    @field_validator("reserved_at", "settled_at")
    @classmethod
    def provider_reservation_times_are_utc(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return _as_utc(value) if value is not None else None

    @model_validator(mode="after")
    def provider_reservation_is_consistent(
        self,
    ) -> TrustedProviderCostReservation:
        if self.status == "reserved":
            if self.receipt is not None or self.settled_at is not None:
                raise ValueError("reserved provider cost cannot contain settlement evidence")
            return self
        if self.receipt is None or self.settled_at is None:
            raise ValueError("settled provider cost requires receipt and timestamp")
        authorization = self.cost_cap
        receipt = self.receipt
        if (
            receipt.reservation_id != authorization.reservation_id
            or receipt.assignment_id != authorization.assignment_id
            or receipt.provider != authorization.provider
            or receipt.provider_request_id != authorization.provider_request_id
            or receipt.model != authorization.model
        ):
            raise ValueError("provider settlement must match its frozen authorization")
        return self


def _as_utc(value: datetime | None = None) -> datetime:
    current = value or utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("datetime must include a timezone")
    return current.astimezone(timezone.utc)


_USD_LEDGER_SCALE = Decimal("1000000000")


def _usd_reservation_units(value: float) -> int:
    """Conservatively round a requested/provider cost up to nano-USD."""

    return int((Decimal(str(value)) * _USD_LEDGER_SCALE).to_integral_value(rounding=ROUND_CEILING))


def _usd_budget_units(value: float) -> int:
    """Conservatively round the user budget down to nano-USD."""

    return int((Decimal(str(value)) * _USD_LEDGER_SCALE).to_integral_value(rounding=ROUND_FLOOR))


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(child) for child in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _persisted_json(value: Any) -> str:
    """Serialize only the redacted, protected-evidence-safe projection."""

    sanitized = sanitize_collaboration_payload(_jsonable(value))
    sanitized = _redact_free_form_secrets(sanitized)
    validate_collaboration_payload_safety(sanitized)
    return json.dumps(
        sanitized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _persisted_text(value: str) -> str:
    """Redact a free-form scalar before it reaches a SQLite column."""

    sanitized = sanitize_collaboration_payload(value)
    sanitized = _redact_free_form_secrets(sanitized)
    validate_collaboration_payload_safety(sanitized)
    if not isinstance(sanitized, str):
        raise ValueError("persisted text must remain a string after redaction")
    return sanitized


def _redact_free_form_secrets(value: Any) -> Any:
    """Cover free-form bearers and token labels without requiring a field key."""

    if isinstance(value, dict):
        return {key: _redact_free_form_secrets(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_redact_free_form_secrets(child) for child in value]
    if isinstance(value, str) and _FREE_FORM_SECRET_PATTERN.search(value):
        return "[REDACTED]"
    return value


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


def _decode_model(model: type[ModelT], payload: str) -> ModelT:
    return model.model_validate_json(payload)


class CollaborativeDevelopmentStore:
    """Restart-safe SQLite/WAL state machine.

    The public API is async so API servers and a standalone dispatcher can use
    the same contract without keeping a process-local connection alive.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        trusted_provider_cost_authorizer: (
            Callable[[TrustedProviderCostAuthorization], bool] | None
        ) = None,
        trusted_provider_receipt_verifier: (
            Callable[[TrustedProviderCostReceipt], bool] | None
        ) = None,
    ):
        self.database_path = Path(database_path).expanduser().resolve()
        if self.database_path.exists() and self.database_path.is_dir():
            raise ValueError("collaborative development database path cannot be a directory")
        self._write_lock = threading.RLock()
        self._initialized = False
        self._trusted_provider_cost_authorizer = trusted_provider_cost_authorizer
        self._trusted_provider_receipt_verifier = trusted_provider_receipt_verifier

    def _enforce_storage_permissions(self) -> None:
        """Keep the collaboration data directory and every SQLite file private."""

        parent = self.database_path.parent
        if parent.exists():
            metadata = parent.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise CollaborativeDevelopmentStorageError(
                    "collaborative development data directory is not a safe directory"
                )
            os.chmod(parent, 0o700, follow_symlinks=False)
        for candidate in (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        ):
            try:
                descriptor = os.open(
                    candidate,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
            except FileNotFoundError:
                # WAL and SHM sidecars may disappear after the final SQLite
                # connection closes.  Their absence is safe; checking with
                # exists()/stat()/chmod() would introduce a TOCTOU window.
                continue
            except OSError as error:
                raise CollaborativeDevelopmentStorageError(
                    "collaborative development SQLite path is not safely openable"
                ) from error
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise CollaborativeDevelopmentStorageError(
                        "collaborative development SQLite path is not a regular file"
                    )
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        # On a WAL database the first schema read materializes any shared-memory
        # sidecars.  Trigger it before chmod so a read-only process restart
        # cannot leave a newly-created ``-shm`` file at the ambient umask.
        connection.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
        self._enforce_storage_permissions()
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
                self._enforce_storage_permissions()

    @staticmethod
    def _migrate_zero_cost_provider_receipts(
        connection: sqlite3.Connection,
    ) -> None:
        """Permit verified zero-bill settlements in databases made earlier.

        Subscription providers can return real token usage while billing no
        per-request amount.  Both provider tables must be rebuilt together
        because one references the other and SQLite cannot alter CHECK
        constraints in place.
        """

        rows = connection.execute(
            """
            SELECT name,sql FROM sqlite_master
            WHERE type='table' AND name IN (
              'collaborative_development_provider_cost_reservations',
              'collaborative_development_provider_costs'
            )
            """
        ).fetchall()
        definitions = {str(row["name"]): "".join(str(row["sql"]).split()) for row in rows}
        reservations_sql = definitions.get(
            "collaborative_development_provider_cost_reservations",
            "",
        )
        costs_sql = definitions.get(
            "collaborative_development_provider_costs",
            "",
        )
        if (
            "settled_cost_unitsISNULLORsettled_cost_units>0" not in reservations_sql
            and "CHECK(cost_usd>0)" not in costs_sql
        ):
            return

        connection.executescript(
            """
            BEGIN IMMEDIATE;
            CREATE TABLE collaborative_development_provider_cost_reservations_zero (
              reservation_id TEXT PRIMARY KEY,
              assignment_id TEXT NOT NULL,
              provider TEXT NOT NULL,
              provider_request_id TEXT NOT NULL,
              request_digest TEXT NOT NULL,
              worst_case_cost_units INTEGER NOT NULL
                CHECK(worst_case_cost_units > 0),
              settled_cost_units INTEGER
                CHECK(settled_cost_units IS NULL OR settled_cost_units >= 0),
              status TEXT NOT NULL CHECK(status IN ('reserved','settled')),
              record_json TEXT NOT NULL,
              reserved_at TEXT NOT NULL,
              settled_at TEXT,
              UNIQUE(provider,provider_request_id),
              FOREIGN KEY(assignment_id)
                REFERENCES collaborative_development_assignments(assignment_id)
            );
            CREATE TABLE collaborative_development_provider_costs_zero (
              receipt_id TEXT PRIMARY KEY,
              reservation_id TEXT NOT NULL UNIQUE,
              assignment_id TEXT NOT NULL,
              provider TEXT NOT NULL,
              provider_request_id TEXT NOT NULL,
              request_digest TEXT NOT NULL,
              cost_usd REAL NOT NULL CHECK(cost_usd >= 0),
              payload_json TEXT NOT NULL,
              issued_at TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(provider,provider_request_id),
              FOREIGN KEY(assignment_id)
                REFERENCES collaborative_development_assignments(assignment_id),
              FOREIGN KEY(reservation_id)
                REFERENCES collaborative_development_provider_cost_reservations_zero(
                  reservation_id
                )
            );
            INSERT INTO collaborative_development_provider_cost_reservations_zero
            SELECT * FROM collaborative_development_provider_cost_reservations;
            INSERT INTO collaborative_development_provider_costs_zero
            SELECT * FROM collaborative_development_provider_costs;
            DROP TABLE collaborative_development_provider_costs;
            DROP TABLE collaborative_development_provider_cost_reservations;
            ALTER TABLE collaborative_development_provider_cost_reservations_zero
              RENAME TO collaborative_development_provider_cost_reservations;
            ALTER TABLE collaborative_development_provider_costs_zero
              RENAME TO collaborative_development_provider_costs;
            CREATE INDEX idx_collab_dev_provider_reservation_assignment
              ON collaborative_development_provider_cost_reservations(
                assignment_id,status,reserved_at,reservation_id
              );
            CREATE INDEX idx_collab_dev_provider_cost_assignment
              ON collaborative_development_provider_costs(
                assignment_id,issued_at,receipt_id
              );
            COMMIT;
            """
        )

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._enforce_storage_permissions()
        with self._write_lock:
            connection = self._connect()
            try:
                mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
                if mode.casefold() != "wal":
                    raise CollaborativeDevelopmentStorageError(
                        f"SQLite refused WAL journal mode: {mode}"
                    )
                connection.execute("PRAGMA synchronous=FULL")
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS collaborative_development_metadata (
                      key TEXT PRIMARY KEY,
                      value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS collaborative_development_assignments (
                      assignment_id TEXT PRIMARY KEY,
                      software_id TEXT NOT NULL,
                      baseline_commit TEXT NOT NULL,
                      status TEXT NOT NULL
                        CHECK(status IN ('active','stopped','closed','archived')),
                      approval_mode TEXT NOT NULL
                        CHECK(approval_mode IN ('manual','auto_forward')),
                      execution_mode TEXT NOT NULL
                        CHECK(execution_mode IN ('manual_dispatch','autonomous')),
                      revision INTEGER NOT NULL CHECK(revision >= 1),
                      next_seq INTEGER NOT NULL DEFAULT 1 CHECK(next_seq >= 1),
                      deadline TEXT NOT NULL,
                      payload_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS collaborative_development_work_items (
                      work_item_id TEXT PRIMARY KEY,
                      assignment_id TEXT NOT NULL,
                      kind TEXT NOT NULL,
                      assigned_role TEXT NOT NULL CHECK(assigned_role IN ('lilies','codex')),
                      status TEXT NOT NULL,
                      lease_revision INTEGER NOT NULL DEFAULT 0 CHECK(lease_revision >= 0),
                      revision INTEGER NOT NULL CHECK(revision >= 1),
                      dispatch_authorized INTEGER NOT NULL DEFAULT 0
                        CHECK(dispatch_authorized IN (0,1)),
                      current_result_id TEXT,
                      payload_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_collab_dev_work_assignment
                      ON collaborative_development_work_items(
                        assignment_id,status,created_at,work_item_id
                      );

                    CREATE TABLE IF NOT EXISTS collaborative_development_leases (
                      lease_id TEXT PRIMARY KEY,
                      assignment_id TEXT NOT NULL,
                      work_item_id TEXT NOT NULL,
                      owner_role TEXT NOT NULL CHECK(owner_role IN ('lilies','codex')),
                      owner_id TEXT NOT NULL,
                      fence INTEGER NOT NULL CHECK(fence >= 1),
                      work_item_revision INTEGER NOT NULL CHECK(work_item_revision >= 1),
                      status TEXT NOT NULL
                        CHECK(status IN ('active','released','expired','revoked')),
                      payload_json TEXT NOT NULL,
                      acquired_at TEXT NOT NULL,
                      expires_at TEXT NOT NULL,
                      released_at TEXT,
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id),
                      FOREIGN KEY(work_item_id)
                        REFERENCES collaborative_development_work_items(work_item_id)
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_collab_dev_one_active_lease
                      ON collaborative_development_leases(work_item_id)
                      WHERE status='active';

                    CREATE TABLE IF NOT EXISTS collaborative_development_results (
                      result_id TEXT PRIMARY KEY,
                      assignment_id TEXT NOT NULL,
                      work_item_id TEXT NOT NULL,
                      lease_id TEXT NOT NULL,
                      work_item_revision INTEGER NOT NULL CHECK(work_item_revision >= 1),
                      payload_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id),
                      FOREIGN KEY(work_item_id)
                        REFERENCES collaborative_development_work_items(work_item_id),
                      FOREIGN KEY(lease_id)
                        REFERENCES collaborative_development_leases(lease_id)
                    );

                    CREATE TABLE IF NOT EXISTS collaborative_development_reviews (
                      review_id TEXT PRIMARY KEY,
                      assignment_id TEXT NOT NULL,
                      work_item_id TEXT NOT NULL,
                      result_id TEXT NOT NULL,
                      work_item_revision INTEGER NOT NULL CHECK(work_item_revision >= 1),
                      verdict TEXT NOT NULL CHECK(verdict IN ('accepted','rework')),
                      payload_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id),
                      FOREIGN KEY(work_item_id)
                        REFERENCES collaborative_development_work_items(work_item_id),
                      FOREIGN KEY(result_id)
                        REFERENCES collaborative_development_results(result_id)
                    );

                    CREATE TABLE IF NOT EXISTS collaborative_development_events (
                      event_id TEXT PRIMARY KEY,
                      assignment_id TEXT NOT NULL,
                      seq INTEGER NOT NULL CHECK(seq >= 1),
                      event_type TEXT NOT NULL,
                      actor_role TEXT NOT NULL,
                      actor_id TEXT NOT NULL,
                      aggregate_type TEXT NOT NULL,
                      aggregate_id TEXT NOT NULL,
                      aggregate_revision INTEGER NOT NULL CHECK(aggregate_revision >= 1),
                      idempotency_key TEXT NOT NULL,
                      payload_json TEXT NOT NULL,
                      event_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      UNIQUE(assignment_id,seq),
                      UNIQUE(
                        assignment_id,event_type,actor_role,actor_id,idempotency_key
                      ),
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id)
                    );

                    CREATE TABLE IF NOT EXISTS collaborative_development_reader_cursors (
                      assignment_id TEXT NOT NULL,
                      reader_role TEXT NOT NULL,
                      reader_id TEXT NOT NULL,
                      ack_seq INTEGER NOT NULL CHECK(ack_seq >= 0),
                      revision INTEGER NOT NULL CHECK(revision >= 1),
                      payload_json TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      PRIMARY KEY(assignment_id,reader_role,reader_id),
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id)
                    );

                    CREATE TABLE IF NOT EXISTS collaborative_development_outbox (
                      outbox_id TEXT PRIMARY KEY,
                      assignment_id TEXT NOT NULL,
                      work_item_id TEXT NOT NULL,
                      destination_role TEXT NOT NULL
                        CHECK(destination_role IN ('lilies','codex')),
                      kind TEXT NOT NULL CHECK(kind IN ('work_dispatch','lilies_review')),
                      idempotency_key TEXT NOT NULL,
                      request_digest TEXT NOT NULL,
                      payload_json TEXT NOT NULL,
                      item_json TEXT NOT NULL,
                      status TEXT NOT NULL
                        CHECK(status IN ('pending','delivered','failed','cancelled')),
                      attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                      available_at TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      delivered_at TEXT,
                      last_error TEXT,
                      UNIQUE(assignment_id,kind,work_item_id,idempotency_key),
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id),
                      FOREIGN KEY(work_item_id)
                        REFERENCES collaborative_development_work_items(work_item_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_collab_dev_outbox_pending
                      ON collaborative_development_outbox(
                        status,available_at,created_at,outbox_id
                      );

                    CREATE TABLE IF NOT EXISTS collaborative_development_outbox_claims (
                      outbox_id TEXT PRIMARY KEY,
                      claim_id TEXT NOT NULL UNIQUE,
                      claimed_by TEXT NOT NULL,
                      claimed_at TEXT NOT NULL,
                      expires_at TEXT NOT NULL,
                      FOREIGN KEY(outbox_id)
                        REFERENCES collaborative_development_outbox(outbox_id)
                        ON DELETE CASCADE
                    );
                    CREATE INDEX IF NOT EXISTS idx_collab_dev_outbox_claim_expiry
                      ON collaborative_development_outbox_claims(expires_at,outbox_id);

                    CREATE TABLE IF NOT EXISTS collaborative_development_invocation_fences (
                      fence_id TEXT PRIMARY KEY,
                      assignment_id TEXT NOT NULL,
                      outbox_id TEXT NOT NULL,
                      attempt INTEGER NOT NULL CHECK(attempt >= 1),
                      claim_id TEXT NOT NULL,
                      destination_role TEXT NOT NULL
                        CHECK(destination_role IN ('lilies','codex')),
                      payload_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      UNIQUE(outbox_id,attempt),
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id),
                      FOREIGN KEY(outbox_id)
                        REFERENCES collaborative_development_outbox(outbox_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_collab_dev_invocation_fence_assignment
                      ON collaborative_development_invocation_fences(
                        assignment_id,created_at,fence_id
                      );

                    CREATE TABLE IF NOT EXISTS
                      collaborative_development_tool_metering_requirements (
                        assignment_id TEXT PRIMARY KEY,
                        required_at TEXT NOT NULL,
                        FOREIGN KEY(assignment_id)
                          REFERENCES collaborative_development_assignments(assignment_id)
                      );

                    CREATE TABLE IF NOT EXISTS collaborative_development_tool_usage (
                      reservation_id TEXT PRIMARY KEY,
                      assignment_id TEXT NOT NULL,
                      actor_role TEXT NOT NULL CHECK(actor_role IN ('lilies','codex')),
                      usage_id TEXT NOT NULL,
                      tool_name TEXT NOT NULL CHECK(tool_name IN (
                        'workspace_search','workspace_read','workspace_write',
                        'workspace_patch','process_run','git_status','git_diff'
                      )),
                      request_digest TEXT NOT NULL,
                      tool_calls INTEGER NOT NULL CHECK(tool_calls = 1),
                      commands INTEGER NOT NULL CHECK(commands IN (0,1)),
                      command_argv_json TEXT,
                      command_cwd TEXT,
                      status TEXT NOT NULL CHECK(status IN ('reserved','completed')),
                      response_digest TEXT,
                      output_digest TEXT,
                      consumer_type TEXT CHECK(consumer_type IN ('result','review')),
                      consumer_id TEXT,
                      record_json TEXT NOT NULL,
                      reserved_at TEXT NOT NULL,
                      completed_at TEXT,
                      UNIQUE(assignment_id,actor_role,usage_id),
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_collab_dev_tool_usage_assignment
                      ON collaborative_development_tool_usage(
                        assignment_id,reserved_at,reservation_id
                      );
                    CREATE INDEX IF NOT EXISTS idx_collab_dev_tool_usage_command
                      ON collaborative_development_tool_usage(
                        assignment_id,actor_role,commands,status,consumer_id
                      );

                    CREATE TABLE IF NOT EXISTS
                      collaborative_development_provider_cost_reservations (
                        reservation_id TEXT PRIMARY KEY,
                        assignment_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        provider_request_id TEXT NOT NULL,
                        request_digest TEXT NOT NULL,
                        worst_case_cost_units INTEGER NOT NULL
                          CHECK(worst_case_cost_units > 0),
                        settled_cost_units INTEGER
                          CHECK(settled_cost_units IS NULL OR settled_cost_units >= 0),
                        status TEXT NOT NULL CHECK(status IN ('reserved','settled')),
                        record_json TEXT NOT NULL,
                        reserved_at TEXT NOT NULL,
                        settled_at TEXT,
                        UNIQUE(provider,provider_request_id),
                        FOREIGN KEY(assignment_id)
                          REFERENCES collaborative_development_assignments(assignment_id)
                      );
                    CREATE INDEX IF NOT EXISTS
                      idx_collab_dev_provider_reservation_assignment
                      ON collaborative_development_provider_cost_reservations(
                        assignment_id,status,reserved_at,reservation_id
                      );

                    CREATE TABLE IF NOT EXISTS collaborative_development_provider_costs (
                      receipt_id TEXT PRIMARY KEY,
                      reservation_id TEXT NOT NULL UNIQUE,
                      assignment_id TEXT NOT NULL,
                      provider TEXT NOT NULL,
                      provider_request_id TEXT NOT NULL,
                      request_digest TEXT NOT NULL,
                      cost_usd REAL NOT NULL CHECK(cost_usd >= 0),
                      payload_json TEXT NOT NULL,
                      issued_at TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      UNIQUE(provider,provider_request_id),
                      FOREIGN KEY(assignment_id)
                        REFERENCES collaborative_development_assignments(assignment_id),
                      FOREIGN KEY(reservation_id)
                        REFERENCES collaborative_development_provider_cost_reservations(
                          reservation_id
                        )
                    );
                    CREATE INDEX IF NOT EXISTS idx_collab_dev_provider_cost_assignment
                      ON collaborative_development_provider_costs(
                        assignment_id,issued_at,receipt_id
                      );

                    CREATE TABLE IF NOT EXISTS collaborative_development_receipts (
                      operation TEXT NOT NULL,
                      scope_id TEXT NOT NULL,
                      actor_role TEXT NOT NULL,
                      actor_id TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL,
                      request_digest TEXT NOT NULL,
                      response_model TEXT NOT NULL,
                      response_json TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      PRIMARY KEY(
                        operation,scope_id,actor_role,actor_id,idempotency_key
                      )
                    );
                    INSERT INTO collaborative_development_metadata(key,value)
                    VALUES ('schema_version','1')
                    ON CONFLICT(key) DO NOTHING;
                    COMMIT;
                    """
                )
                self._migrate_zero_cost_provider_receipts(connection)
                schema_row = connection.execute(
                    "SELECT value FROM collaborative_development_metadata "
                    "WHERE key='schema_version'"
                ).fetchone()
                if schema_row is None or int(schema_row["value"]) != SCHEMA_VERSION:
                    raise CollaborativeDevelopmentStorageError(
                        "unsupported collaborative development schema version"
                    )
                self._initialized = True
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
                self._enforce_storage_permissions()

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise CollaborativeDevelopmentStorageError("store must be initialized before use")

    @staticmethod
    def _receipt(
        connection: sqlite3.Connection,
        *,
        operation: str,
        scope_id: UUID | str,
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        model: type[ModelT],
    ) -> ModelT | None:
        row = connection.execute(
            """
            SELECT * FROM collaborative_development_receipts
            WHERE operation=? AND scope_id=? AND actor_role=? AND actor_id=?
              AND idempotency_key=?
            """,
            (
                operation,
                str(scope_id),
                actor_role,
                actor_id,
                idempotency_key,
            ),
        ).fetchone()
        if row is None:
            return None
        if str(row["request_digest"]) != request_digest:
            raise CollaborativeDevelopmentConflict(
                "idempotency key was reused with a different request"
            )
        return _decode_model(model, str(row["response_json"]))

    @staticmethod
    def _save_receipt(
        connection: sqlite3.Connection,
        *,
        operation: str,
        scope_id: UUID | str,
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        response: BaseModel,
        created_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO collaborative_development_receipts(
              operation,scope_id,actor_role,actor_id,idempotency_key,
              request_digest,response_model,response_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                operation,
                str(scope_id),
                actor_role,
                actor_id,
                idempotency_key,
                request_digest,
                response.__class__.__name__,
                _persisted_json(response),
                created_at.isoformat(),
            ),
        )

    @staticmethod
    def _assignment_conn(
        connection: sqlite3.Connection, assignment_id: UUID | str
    ) -> DevelopmentAssignment:
        row = connection.execute(
            "SELECT payload_json FROM collaborative_development_assignments WHERE assignment_id=?",
            (str(assignment_id),),
        ).fetchone()
        if row is None:
            raise CollaborativeDevelopmentNotFound("development assignment not found")
        return _decode_model(DevelopmentAssignment, str(row["payload_json"]))

    @staticmethod
    def _work_item_conn(
        connection: sqlite3.Connection, work_item_id: UUID | str
    ) -> tuple[DevelopmentWorkItem, sqlite3.Row]:
        row = connection.execute(
            "SELECT * FROM collaborative_development_work_items WHERE work_item_id=?",
            (str(work_item_id),),
        ).fetchone()
        if row is None:
            raise CollaborativeDevelopmentNotFound("development work item not found")
        return _decode_model(DevelopmentWorkItem, str(row["payload_json"])), row

    @staticmethod
    def _lease_conn(connection: sqlite3.Connection, lease_id: UUID | str) -> DevelopmentLease:
        row = connection.execute(
            "SELECT payload_json FROM collaborative_development_leases WHERE lease_id=?",
            (str(lease_id),),
        ).fetchone()
        if row is None:
            raise CollaborativeDevelopmentNotFound("development lease not found")
        return _decode_model(DevelopmentLease, str(row["payload_json"]))

    @staticmethod
    def _role_has(
        assignment: DevelopmentAssignment,
        agent_role: AgentRole,
        task_role: DevelopmentTaskRole,
    ) -> bool:
        return any(
            grant.agent_role == agent_role and task_role in grant.task_roles
            for grant in assignment.agent_roles
        )

    @staticmethod
    def _ensure_assignment_writable(assignment: DevelopmentAssignment, *, now: datetime) -> None:
        if assignment.status != AssignmentStatus.active:
            raise CollaborativeDevelopmentInvalidState(
                f"assignment is {assignment.status.value}, not active"
            )
        if now >= assignment.deadline:
            raise CollaborativeDevelopmentInvalidState("assignment deadline has passed")

    @staticmethod
    def _update_assignment_conn(
        connection: sqlite3.Connection, assignment: DevelopmentAssignment
    ) -> None:
        connection.execute(
            """
            UPDATE collaborative_development_assignments
            SET status=?,approval_mode=?,execution_mode=?,revision=?,
                payload_json=?,updated_at=?
            WHERE assignment_id=?
            """,
            (
                assignment.status.value,
                assignment.approval_mode.value,
                assignment.execution_mode.value,
                assignment.revision,
                _persisted_json(assignment),
                assignment.updated_at.isoformat(),
                str(assignment.assignment_id),
            ),
        )

    @staticmethod
    def _update_work_item_conn(
        connection: sqlite3.Connection,
        work_item: DevelopmentWorkItem,
        *,
        dispatch_authorized: bool | None = None,
        current_result_id: UUID | None = None,
    ) -> None:
        fields = [
            "status=?",
            "lease_revision=?",
            "revision=?",
            "payload_json=?",
            "updated_at=?",
        ]
        values: list[Any] = [
            work_item.status.value,
            work_item.lease_revision,
            work_item.revision,
            _persisted_json(work_item),
            work_item.updated_at.isoformat(),
        ]
        if dispatch_authorized is not None:
            fields.append("dispatch_authorized=?")
            values.append(1 if dispatch_authorized else 0)
        if current_result_id is not None:
            fields.append("current_result_id=?")
            values.append(str(current_result_id))
        values.append(str(work_item.work_item_id))
        cursor = connection.execute(
            f"UPDATE collaborative_development_work_items SET {','.join(fields)} "
            "WHERE work_item_id=?",
            values,
        )
        if cursor.rowcount != 1:
            raise CollaborativeDevelopmentNotFound("development work item not found")

    @staticmethod
    def _append_event_conn(
        connection: sqlite3.Connection,
        *,
        assignment_id: UUID,
        event_type: str,
        actor_role: str,
        actor_id: str,
        aggregate_type: str,
        aggregate_id: UUID,
        aggregate_revision: int,
        idempotency_key: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> DevelopmentEvent:
        row = connection.execute(
            "SELECT next_seq FROM collaborative_development_assignments WHERE assignment_id=?",
            (str(assignment_id),),
        ).fetchone()
        if row is None:
            raise CollaborativeDevelopmentNotFound("development assignment not found")
        seq = int(row["next_seq"])
        event = DevelopmentEvent(
            event_id=uuid5(
                NAMESPACE_URL,
                f"lilies:collaborative-development:{assignment_id}:{seq}:{event_type}",
            ),
            assignment_id=assignment_id,
            seq=seq,
            event_type=event_type,
            actor_role=actor_role,
            actor_id=actor_id,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            aggregate_revision=aggregate_revision,
            idempotency_key=idempotency_key,
            payload=dict(payload),
            created_at=created_at,
        )
        connection.execute(
            """
            INSERT INTO collaborative_development_events(
              event_id,assignment_id,seq,event_type,actor_role,actor_id,
              aggregate_type,aggregate_id,aggregate_revision,idempotency_key,
              payload_json,event_json,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(event.event_id),
                str(assignment_id),
                seq,
                event_type,
                actor_role,
                actor_id,
                aggregate_type,
                str(aggregate_id),
                aggregate_revision,
                idempotency_key,
                _persisted_json(payload),
                _persisted_json(event),
                created_at.isoformat(),
            ),
        )
        connection.execute(
            "UPDATE collaborative_development_assignments SET next_seq=? WHERE assignment_id=?",
            (seq + 1, str(assignment_id)),
        )
        return event

    @staticmethod
    def _enqueue_outbox_conn(
        connection: sqlite3.Connection,
        *,
        assignment_id: UUID,
        work_item_id: UUID,
        destination_role: AgentRole,
        kind: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        now: datetime,
    ) -> DispatchOutboxItem:
        request_digest = _digest(payload)
        existing = connection.execute(
            """
            SELECT * FROM collaborative_development_outbox
            WHERE assignment_id=? AND kind=? AND work_item_id=?
              AND idempotency_key=?
            """,
            (str(assignment_id), kind, str(work_item_id), idempotency_key),
        ).fetchone()
        if existing is not None:
            if str(existing["request_digest"]) != request_digest:
                raise CollaborativeDevelopmentConflict(
                    "outbox idempotency key was reused with a different payload"
                )
            return _decode_model(DispatchOutboxItem, str(existing["item_json"]))
        outbox_id = uuid5(
            NAMESPACE_URL,
            f"lilies:collaborative-development:outbox:"
            f"{assignment_id}:{kind}:{work_item_id}:{idempotency_key}",
        )
        item = DispatchOutboxItem(
            outbox_id=outbox_id,
            assignment_id=assignment_id,
            work_item_id=work_item_id,
            destination_role=destination_role,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=dict(payload),
            status=OutboxStatus.pending,
            attempts=0,
            available_at=now,
            created_at=now,
            updated_at=now,
        )
        connection.execute(
            """
            INSERT INTO collaborative_development_outbox(
              outbox_id,assignment_id,work_item_id,destination_role,kind,
              idempotency_key,request_digest,payload_json,item_json,status,
              attempts,available_at,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                str(item.outbox_id),
                str(assignment_id),
                str(work_item_id),
                destination_role.value,
                kind,
                idempotency_key,
                request_digest,
                _persisted_json(payload),
                _persisted_json(item),
                item.status.value,
                0,
                now.isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        return item

    @classmethod
    def _ensure_lilies_review_outbox_conn(
        cls,
        connection: sqlite3.Connection,
        *,
        assignment_id: UUID,
        work_item_id: UUID,
        result_id: UUID,
        work_item_revision: int,
        now: datetime,
    ) -> DispatchOutboxItem:
        existing = connection.execute(
            """
            SELECT item_json FROM collaborative_development_outbox
            WHERE assignment_id=? AND work_item_id=? AND kind='lilies_review'
              AND json_extract(payload_json,'$.result_id')=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (str(assignment_id), str(work_item_id), str(result_id)),
        ).fetchone()
        if existing is not None:
            item = _decode_model(DispatchOutboxItem, str(existing["item_json"]))
            if item.status == OutboxStatus.cancelled:
                reactivated = item.model_copy(
                    update={
                        "status": OutboxStatus.pending,
                        "available_at": now,
                        "updated_at": now,
                        "last_error": None,
                    }
                )
                connection.execute(
                    """
                    UPDATE collaborative_development_outbox
                    SET status='pending',available_at=?,item_json=?,updated_at=?,
                        last_error=NULL
                    WHERE outbox_id=?
                    """,
                    (
                        now.isoformat(),
                        _persisted_json(reactivated),
                        now.isoformat(),
                        str(reactivated.outbox_id),
                    ),
                )
                return reactivated
            return item
        return cls._enqueue_outbox_conn(
            connection,
            assignment_id=assignment_id,
            work_item_id=work_item_id,
            destination_role=AgentRole.lilies,
            kind="lilies_review",
            idempotency_key=f"review-result:{result_id}",
            payload={
                "assignment_id": str(assignment_id),
                "work_item_id": str(work_item_id),
                "result_id": str(result_id),
                "work_item_revision": work_item_revision,
            },
            now=now,
        )

    @classmethod
    def _ensure_work_dispatch_outbox_conn(
        cls,
        connection: sqlite3.Connection,
        *,
        assignment_id: UUID,
        work_item: DevelopmentWorkItem,
        now: datetime,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> DispatchOutboxItem:
        existing = connection.execute(
            """
            SELECT item_json FROM collaborative_development_outbox
            WHERE assignment_id=? AND work_item_id=? AND kind='work_dispatch'
              AND CAST(
                json_extract(payload_json,'$.work_item_revision') AS INTEGER
              )=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                str(assignment_id),
                str(work_item.work_item_id),
                work_item.revision,
            ),
        ).fetchone()
        if existing is not None:
            item = _decode_model(DispatchOutboxItem, str(existing["item_json"]))
            if item.status == OutboxStatus.cancelled:
                reactivated = item.model_copy(
                    update={
                        "status": OutboxStatus.pending,
                        "available_at": now,
                        "updated_at": now,
                        "last_error": None,
                    }
                )
                connection.execute(
                    """
                    UPDATE collaborative_development_outbox
                    SET status='pending',available_at=?,item_json=?,updated_at=?,
                        last_error=NULL
                    WHERE outbox_id=?
                    """,
                    (
                        now.isoformat(),
                        _persisted_json(reactivated),
                        now.isoformat(),
                        str(reactivated.outbox_id),
                    ),
                )
                return reactivated
            return item
        payload: dict[str, Any] = {
            "assignment_id": str(assignment_id),
            "work_item_id": str(work_item.work_item_id),
            "work_item_revision": work_item.revision,
        }
        if extra_payload:
            payload.update(extra_payload)
        return cls._enqueue_outbox_conn(
            connection,
            assignment_id=assignment_id,
            work_item_id=work_item.work_item_id,
            destination_role=work_item.assigned_role,
            kind="work_dispatch",
            idempotency_key=f"work-revision:{work_item.revision}",
            payload=payload,
            now=now,
        )

    async def create_assignment(
        self,
        assignment: DevelopmentAssignment,
        *,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentAssignment:
        self._require_initialized()
        return await asyncio.to_thread(
            self._create_assignment_sync,
            assignment,
            actor_id,
            str(idempotency_key),
        )

    def _create_assignment_sync(
        self,
        assignment: DevelopmentAssignment,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        if assignment.revision != 1 or assignment.status != AssignmentStatus.active:
            raise ValueError("new assignment must be active at revision 1")
        if assignment.updated_at != assignment.created_at:
            raise ValueError("new assignment updated_at must equal created_at")
        request_digest = _digest(assignment)
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="create_assignment",
                scope_id=assignment.assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentAssignment,
            )
            if replay is not None:
                return replay
            now = utc_now()
            if assignment.deadline <= now:
                raise CollaborativeDevelopmentInvalidState(
                    "assignment deadline must be later than server creation time"
                )
            if assignment.deadline > now + timedelta(seconds=assignment.budget.max_wall_seconds):
                raise CollaborativeDevelopmentBudgetExceeded(
                    "assignment deadline exceeds the wall-time budget from server creation"
                )
            server_assignment = DevelopmentAssignment.model_validate(
                {
                    **assignment.model_dump(mode="python"),
                    "created_at": now,
                    "updated_at": now,
                }
            )
            if connection.execute(
                "SELECT 1 FROM collaborative_development_assignments WHERE assignment_id=?",
                (str(assignment.assignment_id),),
            ).fetchone():
                raise CollaborativeDevelopmentConflict("assignment id already exists")
            connection.execute(
                """
                INSERT INTO collaborative_development_assignments(
                  assignment_id,software_id,baseline_commit,status,approval_mode,
                  execution_mode,revision,next_seq,deadline,payload_json,
                  created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(server_assignment.assignment_id),
                    server_assignment.software_id,
                    server_assignment.baseline_commit,
                    server_assignment.status.value,
                    server_assignment.approval_mode.value,
                    server_assignment.execution_mode.value,
                    server_assignment.revision,
                    1,
                    server_assignment.deadline.isoformat(),
                    _persisted_json(server_assignment),
                    server_assignment.created_at.isoformat(),
                    server_assignment.updated_at.isoformat(),
                ),
            )
            self._append_event_conn(
                connection,
                assignment_id=server_assignment.assignment_id,
                event_type="assignment.created",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="assignment",
                aggregate_id=server_assignment.assignment_id,
                aggregate_revision=1,
                idempotency_key=idempotency_key,
                payload={
                    "software_id": server_assignment.software_id,
                    "baseline_commit": server_assignment.baseline_commit,
                    "approval_mode": server_assignment.approval_mode.value,
                    "execution_mode": server_assignment.execution_mode.value,
                    "enterprise_denominator": False,
                },
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="create_assignment",
                scope_id=server_assignment.assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=server_assignment,
                created_at=now,
            )
            return server_assignment

    async def get_assignment(self, assignment_id: UUID | str) -> DevelopmentAssignment:
        self._require_initialized()
        return await asyncio.to_thread(self._get_assignment_sync, assignment_id)

    def _get_assignment_sync(self, assignment_id: UUID | str) -> DevelopmentAssignment:
        connection = self._connect()
        try:
            return self._assignment_conn(connection, assignment_id)
        finally:
            connection.close()

    async def get_lease(self, lease_id: UUID | str) -> DevelopmentLease:
        self._require_initialized()
        return await asyncio.to_thread(self._get_lease_sync, lease_id)

    def _get_lease_sync(self, lease_id: UUID | str) -> DevelopmentLease:
        connection = self._connect()
        try:
            return self._lease_conn(connection, lease_id)
        finally:
            connection.close()

    async def apply_workspace_grant_revision(
        self,
        assignment_id: UUID | str,
        *,
        outbox_id: UUID | str,
        replacement_grant: WorkspaceGrant,
        replacement_budget: DevelopmentBudget | None = None,
        expected_assignment_revision: int,
        expected_grant_digest: str,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentAssignment:
        """Atomically revise one role grant while leaving dispatch paused.

        Decision policy remains in the owner-only service.  This storage
        boundary only applies a fully validated replacement with strict
        identity, revision, assignment, role, and authorization-failure fences.
        A separately idempotent :meth:`resume_authorization_outbox` call lets
        the broker durably revise its manifest before any worker can race.
        """

        self._require_initialized()
        grant = WorkspaceGrant.model_validate(
            (
                replacement_grant.model_dump(mode="python")
                if isinstance(replacement_grant, WorkspaceGrant)
                else replacement_grant
            )
        )
        budget = (
            DevelopmentBudget.model_validate(
                (
                    replacement_budget.model_dump(mode="python")
                    if isinstance(replacement_budget, DevelopmentBudget)
                    else replacement_budget
                )
            )
            if replacement_budget is not None
            else None
        )
        return await asyncio.to_thread(
            self._apply_workspace_grant_revision_sync,
            UUID(str(assignment_id)),
            UUID(str(outbox_id)),
            grant,
            budget,
            expected_assignment_revision,
            expected_grant_digest,
            actor_id,
            str(idempotency_key),
        )

    def _apply_workspace_grant_revision_sync(
        self,
        assignment_id: UUID,
        outbox_id: UUID,
        replacement_grant: WorkspaceGrant,
        replacement_budget: DevelopmentBudget | None,
        expected_assignment_revision: int,
        expected_grant_digest: str,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        request = {
            "outbox_id": str(outbox_id),
            "replacement_grant": replacement_grant,
            "replacement_budget": replacement_budget,
            "expected_assignment_revision": expected_assignment_revision,
            "expected_grant_digest": expected_grant_digest,
        }
        request_digest = _digest(request)
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="apply_workspace_grant_revision",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentAssignment,
            )
            if replay is not None:
                return replay

            assignment = self._assignment_conn(connection, assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            if assignment.revision != expected_assignment_revision:
                raise CollaborativeDevelopmentConflict("assignment revision compare-and-set failed")
            matches = [
                grant
                for grant in assignment.workspace_grants
                if grant.agent_role == replacement_grant.agent_role
            ]
            if len(matches) != 1:
                raise CollaborativeDevelopmentAuthorizationError(
                    "replacement role has no unique current workspace grant"
                )
            current = matches[0]
            if _digest(current) != expected_grant_digest:
                raise CollaborativeDevelopmentConflict(
                    "workspace grant digest compare-and-set failed"
                )
            stable_identity = (
                replacement_grant.workspace_id == current.workspace_id,
                replacement_grant.agent_role == current.agent_role,
                replacement_grant.workspace_root == current.workspace_root,
                replacement_grant.baseline_commit == current.baseline_commit,
                replacement_grant.created_at == current.created_at,
            )
            if not all(stable_identity):
                raise CollaborativeDevelopmentAuthorizationError(
                    "grant revision cannot change workspace identity or baseline"
                )
            if replacement_grant.grant_revision != current.grant_revision + 1:
                raise CollaborativeDevelopmentConflict(
                    "replacement grant revision must advance exactly once"
                )

            outbox_row = connection.execute(
                """
                SELECT item_json FROM collaborative_development_outbox
                WHERE outbox_id=? AND assignment_id=? AND destination_role=?
                """,
                (
                    str(outbox_id),
                    str(assignment_id),
                    replacement_grant.agent_role.value,
                ),
            ).fetchone()
            if outbox_row is None:
                raise CollaborativeDevelopmentConflict(
                    "authorization outbox does not match the assignment and role"
                )
            outbox = _decode_model(
                DispatchOutboxItem,
                str(outbox_row["item_json"]),
            )
            if (
                outbox.status != OutboxStatus.failed
                or outbox.last_error != "authorization_required"
            ):
                raise CollaborativeDevelopmentInvalidState(
                    "only an authorization_required failed outbox can resume"
                )

            replacement_grants = tuple(
                replacement_grant if grant.agent_role == replacement_grant.agent_role else grant
                for grant in assignment.workspace_grants
            )
            updated_assignment = DevelopmentAssignment.model_validate(
                {
                    **assignment.model_dump(mode="python"),
                    "workspace_grants": replacement_grants,
                    "budget": replacement_budget or assignment.budget,
                    "revision": assignment.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_assignment_conn(connection, updated_assignment)

            new_grant_digest = _digest(replacement_grant)
            self._append_event_conn(
                connection,
                assignment_id=assignment_id,
                event_type="assignment.workspace_grant_revised",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="assignment",
                aggregate_id=assignment_id,
                aggregate_revision=updated_assignment.revision,
                idempotency_key=idempotency_key,
                payload={
                    "agent_role": replacement_grant.agent_role.value,
                    "previous_grant_revision": current.grant_revision,
                    "current_grant_revision": replacement_grant.grant_revision,
                    "previous_grant_digest": expected_grant_digest,
                    "current_grant_digest": new_grant_digest,
                    "budget_changed": replacement_budget is not None,
                    "authorization_outbox_id": str(outbox_id),
                    "dispatch_remains_paused": True,
                },
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="apply_workspace_grant_revision",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated_assignment,
                created_at=now,
            )
            return updated_assignment

    async def resume_authorization_outbox(
        self,
        assignment_id: UUID | str,
        *,
        outbox_id: UUID | str,
        expected_grant_digest: str,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DispatchOutboxItem:
        """Resume one authorization outbox after broker revision is durable."""

        self._require_initialized()
        return await asyncio.to_thread(
            self._resume_authorization_outbox_sync,
            UUID(str(assignment_id)),
            UUID(str(outbox_id)),
            expected_grant_digest,
            actor_id,
            str(idempotency_key),
        )

    def _resume_authorization_outbox_sync(
        self,
        assignment_id: UUID,
        outbox_id: UUID,
        expected_grant_digest: str,
        actor_id: str,
        idempotency_key: str,
    ) -> DispatchOutboxItem:
        request_digest = _digest(
            {
                "outbox_id": str(outbox_id),
                "expected_grant_digest": expected_grant_digest,
            }
        )
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="resume_authorization_outbox",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DispatchOutboxItem,
            )
            if replay is not None:
                return replay
            assignment = self._assignment_conn(connection, assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            row = connection.execute(
                """
                SELECT item_json FROM collaborative_development_outbox
                WHERE outbox_id=? AND assignment_id=?
                """,
                (str(outbox_id), str(assignment_id)),
            ).fetchone()
            if row is None:
                raise CollaborativeDevelopmentConflict(
                    "authorization outbox does not match the assignment"
                )
            outbox = _decode_model(DispatchOutboxItem, str(row["item_json"]))
            matching_grants = [
                grant
                for grant in assignment.workspace_grants
                if grant.agent_role == outbox.destination_role
            ]
            if len(matching_grants) != 1 or (_digest(matching_grants[0]) != expected_grant_digest):
                raise CollaborativeDevelopmentConflict(
                    "resumed outbox grant digest compare-and-set failed"
                )
            if (
                outbox.status != OutboxStatus.failed
                or outbox.last_error != "authorization_required"
            ):
                raise CollaborativeDevelopmentInvalidState(
                    "only an authorization_required failed outbox can resume"
                )
            resumed = outbox.model_copy(
                update={
                    "status": OutboxStatus.pending,
                    "available_at": now,
                    "updated_at": now,
                    "delivered_at": None,
                    "last_error": None,
                }
            )
            connection.execute(
                """
                UPDATE collaborative_development_outbox
                SET status='pending',available_at=?,updated_at=?,delivered_at=NULL,
                    last_error=NULL,item_json=?
                WHERE outbox_id=?
                """,
                (
                    now.isoformat(),
                    now.isoformat(),
                    _persisted_json(resumed),
                    str(outbox_id),
                ),
            )
            connection.execute(
                "DELETE FROM collaborative_development_outbox_claims WHERE outbox_id=?",
                (str(outbox_id),),
            )
            self._append_event_conn(
                connection,
                assignment_id=assignment_id,
                event_type="assignment.authorization_outbox_resumed",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="assignment",
                aggregate_id=assignment_id,
                aggregate_revision=assignment.revision,
                idempotency_key=idempotency_key,
                payload={
                    "agent_role": outbox.destination_role.value,
                    "grant_digest": expected_grant_digest,
                    "outbox_id": str(outbox_id),
                },
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="resume_authorization_outbox",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=resumed,
                created_at=now,
            )
            return resumed

    async def requeue_review_reconciliation(
        self,
        assignment_id: UUID | str,
        *,
        outbox_id: UUID | str,
        expected_work_item_revision: int,
        expected_failed_attempt: int,
        reason: str,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DispatchOutboxItem:
        """Explicitly requeue one review whose prior side effect is unknown.

        The original outbox, grant, assignment budget, and accumulated usage
        remain unchanged.  Only the failed outbox state is moved back to
        ``pending``; the next dispatcher invocation therefore uses the next
        durable attempt number.  A caller must fence both the work-item
        revision and the failed attempt it inspected.
        """

        self._require_initialized()
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("review reconciliation requires a reason")
        if len(normalized_reason) > 2_000:
            raise ValueError("review reconciliation reason is too long")
        if expected_work_item_revision < 1:
            raise ValueError("expected work item revision must be at least one")
        if expected_failed_attempt < 1:
            raise ValueError("expected failed attempt must be at least one")
        return await asyncio.to_thread(
            self._requeue_review_reconciliation_sync,
            UUID(str(assignment_id)),
            UUID(str(outbox_id)),
            expected_work_item_revision,
            expected_failed_attempt,
            normalized_reason,
            actor_id,
            str(idempotency_key),
        )

    def _requeue_review_reconciliation_sync(
        self,
        assignment_id: UUID,
        outbox_id: UUID,
        expected_work_item_revision: int,
        expected_failed_attempt: int,
        reason: str,
        actor_id: str,
        idempotency_key: str,
    ) -> DispatchOutboxItem:
        persisted_reason = _persisted_text(reason)
        request_digest = _digest(
            {
                "outbox_id": str(outbox_id),
                "expected_work_item_revision": expected_work_item_revision,
                "expected_failed_attempt": expected_failed_attempt,
                "reason": persisted_reason,
            }
        )
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="requeue_review_reconciliation",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DispatchOutboxItem,
            )
            if replay is not None:
                return replay

            assignment = self._assignment_conn(connection, assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            row = connection.execute(
                """
                SELECT item_json
                FROM collaborative_development_outbox
                WHERE outbox_id=? AND assignment_id=?
                """,
                (str(outbox_id), str(assignment_id)),
            ).fetchone()
            if row is None:
                raise CollaborativeDevelopmentConflict(
                    "review reconciliation outbox does not match the assignment"
                )
            outbox = _decode_model(DispatchOutboxItem, str(row["item_json"]))
            if outbox.kind != "lilies_review" or outbox.destination_role != AgentRole.lilies:
                raise CollaborativeDevelopmentInvalidState(
                    "only a Lilies review outbox can be reconciled"
                )
            if (
                outbox.status != OutboxStatus.failed
                or outbox.last_error != "reconciliation_required"
            ):
                raise CollaborativeDevelopmentInvalidState(
                    "only a reconciliation_required failed review can be requeued"
                )
            if outbox.attempts != expected_failed_attempt:
                raise CollaborativeDevelopmentConflict(
                    "review reconciliation attempt compare-and-set failed"
                )

            item, item_row = self._work_item_conn(connection, outbox.work_item_id)
            if item.assignment_id != assignment_id:
                raise CollaborativeDevelopmentConflict(
                    "review reconciliation work item does not match the assignment"
                )
            if item.revision != expected_work_item_revision:
                raise CollaborativeDevelopmentConflict("work item revision compare-and-set failed")
            if item.status != WorkItemStatus.ready_for_lilies_review:
                raise CollaborativeDevelopmentInvalidState(
                    "review reconciliation requires a ready_for_lilies_review work item"
                )

            raw_result_id = outbox.payload.get("result_id")
            try:
                result_id = UUID(str(raw_result_id))
            except (TypeError, ValueError) as error:
                raise CollaborativeDevelopmentConflict(
                    "review reconciliation outbox has no valid result binding"
                ) from error
            if str(item_row["current_result_id"]) != str(result_id):
                raise CollaborativeDevelopmentConflict(
                    "review reconciliation result is no longer current"
                )
            result_row = connection.execute(
                """
                SELECT 1
                FROM collaborative_development_results
                WHERE result_id=? AND assignment_id=? AND work_item_id=?
                """,
                (str(result_id), str(assignment_id), str(item.work_item_id)),
            ).fetchone()
            if result_row is None:
                raise CollaborativeDevelopmentConflict(
                    "review reconciliation result binding is missing"
                )
            fence_row = connection.execute(
                """
                SELECT 1
                FROM collaborative_development_invocation_fences
                WHERE assignment_id=? AND outbox_id=? AND attempt=?
                  AND destination_role='lilies'
                """,
                (
                    str(assignment_id),
                    str(outbox_id),
                    expected_failed_attempt,
                ),
            ).fetchone()
            prior_invocation_fenced = fence_row is not None

            requeued = outbox.model_copy(
                update={
                    "status": OutboxStatus.pending,
                    "available_at": now,
                    "updated_at": now,
                    "delivered_at": None,
                    "last_error": None,
                }
            )
            connection.execute(
                """
                UPDATE collaborative_development_outbox
                SET status='pending',available_at=?,updated_at=?,
                    delivered_at=NULL,last_error=NULL,item_json=?
                WHERE outbox_id=?
                """,
                (
                    now.isoformat(),
                    now.isoformat(),
                    _persisted_json(requeued),
                    str(outbox_id),
                ),
            )
            connection.execute(
                "DELETE FROM collaborative_development_outbox_claims WHERE outbox_id=?",
                (str(outbox_id),),
            )
            self._append_event_conn(
                connection,
                assignment_id=assignment_id,
                event_type="work_item.review_reconciliation_requeued",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="work_item",
                aggregate_id=item.work_item_id,
                aggregate_revision=item.revision,
                idempotency_key=idempotency_key,
                payload={
                    "outbox_id": str(outbox_id),
                    "result_id": str(result_id),
                    "failed_attempt": expected_failed_attempt,
                    "next_attempt": expected_failed_attempt + 1,
                    "execution_mode": assignment.execution_mode.value,
                    "prior_invocation_fenced": prior_invocation_fenced,
                    "reason": persisted_reason,
                    "grant_changed": False,
                    "budget_reset": False,
                },
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="requeue_review_reconciliation",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=requeued,
                created_at=now,
            )
            return requeued

    async def set_execution_mode(
        self,
        assignment_id: UUID | str,
        mode: ExecutionMode,
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentAssignment:
        self._require_initialized()
        return await asyncio.to_thread(
            self._set_execution_mode_sync,
            UUID(str(assignment_id)),
            mode,
            expected_revision,
            actor_id,
            str(idempotency_key),
        )

    def _set_execution_mode_sync(
        self,
        assignment_id: UUID,
        mode: ExecutionMode,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        request = {"mode": mode.value, "expected_revision": expected_revision}
        request_digest = _digest(request)
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="set_execution_mode",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentAssignment,
            )
            if replay is not None:
                return replay
            assignment = self._assignment_conn(connection, assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            if assignment.revision != expected_revision:
                raise CollaborativeDevelopmentConflict("assignment revision compare-and-set failed")
            updated = assignment.model_copy(
                update={
                    "execution_mode": mode,
                    "revision": assignment.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_assignment_conn(connection, updated)
            self._append_event_conn(
                connection,
                assignment_id=assignment_id,
                event_type="assignment.execution_mode_changed",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="assignment",
                aggregate_id=assignment_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={
                    "previous": assignment.execution_mode.value,
                    "current": mode.value,
                },
                created_at=now,
            )
            if mode == ExecutionMode.autonomous:
                rows = connection.execute(
                    """
                    SELECT * FROM collaborative_development_work_items
                    WHERE assignment_id=? AND status IN ('proposed','rework','awaiting_dispatch')
                    ORDER BY created_at,work_item_id
                    """,
                    (str(assignment_id),),
                ).fetchall()
                for row in rows:
                    item = _decode_model(DevelopmentWorkItem, str(row["payload_json"]))
                    if not self._agent_invocation_budget_available(
                        updated
                    ) or not self._dependencies_satisfied_conn(connection, item):
                        connection.execute(
                            "UPDATE collaborative_development_work_items "
                            "SET dispatch_authorized=0 WHERE work_item_id=?",
                            (str(item.work_item_id),),
                        )
                        continue
                    if item.status != WorkItemStatus.awaiting_dispatch:
                        item = item.model_copy(
                            update={
                                "status": WorkItemStatus.awaiting_dispatch,
                                "revision": item.revision + 1,
                                "updated_at": now,
                            }
                        )
                        self._update_work_item_conn(connection, item, dispatch_authorized=True)
                        self._append_event_conn(
                            connection,
                            assignment_id=assignment_id,
                            event_type="work_item.awaiting_dispatch",
                            actor_role="platform",
                            actor_id="autonomous-dispatcher",
                            aggregate_type="work_item",
                            aggregate_id=item.work_item_id,
                            aggregate_revision=item.revision,
                            idempotency_key=f"{idempotency_key}:work:{item.work_item_id}",
                            payload={"reason": "execution_mode_autonomous"},
                            created_at=now,
                        )
                    else:
                        connection.execute(
                            "UPDATE collaborative_development_work_items "
                            "SET dispatch_authorized=1 WHERE work_item_id=?",
                            (str(item.work_item_id),),
                        )
                    self._ensure_work_dispatch_outbox_conn(
                        connection,
                        assignment_id=assignment_id,
                        work_item=item,
                        now=now,
                    )
                review_rows = connection.execute(
                    """
                    SELECT work_item_id,current_result_id,revision
                    FROM collaborative_development_work_items
                    WHERE assignment_id=? AND status='ready_for_lilies_review'
                      AND current_result_id IS NOT NULL
                    ORDER BY created_at,work_item_id
                    """,
                    (str(assignment_id),),
                ).fetchall()
                if self._agent_invocation_budget_available(updated):
                    for review_row in review_rows:
                        self._ensure_lilies_review_outbox_conn(
                            connection,
                            assignment_id=assignment_id,
                            work_item_id=UUID(str(review_row["work_item_id"])),
                            result_id=UUID(str(review_row["current_result_id"])),
                            work_item_revision=int(review_row["revision"]),
                            now=now,
                        )
            elif assignment.execution_mode == ExecutionMode.autonomous:
                # Switching the execution policy is a stop boundary.  Durable
                # autonomous dispatches that have not been delivered must not
                # remain executable under manual_dispatch.
                connection.execute(
                    """
                    UPDATE collaborative_development_work_items
                    SET dispatch_authorized=0
                    WHERE assignment_id=? AND status='awaiting_dispatch'
                    """,
                    (str(assignment_id),),
                )
                pending_rows = connection.execute(
                    """
                    SELECT item_json FROM collaborative_development_outbox
                    WHERE assignment_id=? AND status='pending'
                    """,
                    (str(assignment_id),),
                ).fetchall()
                for pending_row in pending_rows:
                    pending_item = _decode_model(DispatchOutboxItem, str(pending_row["item_json"]))
                    cancelled = pending_item.model_copy(
                        update={
                            "status": OutboxStatus.cancelled,
                            "updated_at": now,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE collaborative_development_outbox
                        SET status='cancelled',item_json=?,updated_at=?
                        WHERE outbox_id=?
                        """,
                        (
                            _persisted_json(cancelled),
                            now.isoformat(),
                            str(cancelled.outbox_id),
                        ),
                    )
                    connection.execute(
                        "DELETE FROM collaborative_development_outbox_claims WHERE outbox_id=?",
                        (str(cancelled.outbox_id),),
                    )
            self._save_receipt(
                connection,
                operation="set_execution_mode",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    async def set_approval_mode(
        self,
        assignment_id: UUID | str,
        mode: ApprovalMode,
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentAssignment:
        self._require_initialized()
        return await asyncio.to_thread(
            self._set_approval_mode_sync,
            UUID(str(assignment_id)),
            mode,
            expected_revision,
            actor_id,
            str(idempotency_key),
        )

    def _set_approval_mode_sync(
        self,
        assignment_id: UUID,
        mode: ApprovalMode,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        request_digest = _digest({"mode": mode.value, "expected_revision": expected_revision})
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="set_approval_mode",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentAssignment,
            )
            if replay is not None:
                return replay
            assignment = self._assignment_conn(connection, assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            if assignment.revision != expected_revision:
                raise CollaborativeDevelopmentConflict("assignment revision compare-and-set failed")
            updated = assignment.model_copy(
                update={
                    "approval_mode": mode,
                    "revision": assignment.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_assignment_conn(connection, updated)
            self._append_event_conn(
                connection,
                assignment_id=assignment_id,
                event_type="assignment.approval_mode_changed",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="assignment",
                aggregate_id=assignment_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={
                    "previous": assignment.approval_mode.value,
                    "current": mode.value,
                },
                created_at=now,
            )
            if mode == ApprovalMode.auto_forward:
                review_rows = connection.execute(
                    """
                    SELECT work_item_id,current_result_id,revision
                    FROM collaborative_development_work_items
                    WHERE assignment_id=? AND status='ready_for_lilies_review'
                      AND current_result_id IS NOT NULL
                    ORDER BY created_at,work_item_id
                    """,
                    (str(assignment_id),),
                ).fetchall()
                for review_row in review_rows:
                    self._ensure_lilies_review_outbox_conn(
                        connection,
                        assignment_id=assignment_id,
                        work_item_id=UUID(str(review_row["work_item_id"])),
                        result_id=UUID(str(review_row["current_result_id"])),
                        work_item_revision=int(review_row["revision"]),
                        now=now,
                    )
            elif (
                assignment.approval_mode == ApprovalMode.auto_forward
                and assignment.execution_mode == ExecutionMode.manual_dispatch
            ):
                pending_rows = connection.execute(
                    """
                    SELECT item_json FROM collaborative_development_outbox
                    WHERE assignment_id=? AND kind='lilies_review' AND status='pending'
                    """,
                    (str(assignment_id),),
                ).fetchall()
                for pending_row in pending_rows:
                    pending_item = _decode_model(DispatchOutboxItem, str(pending_row["item_json"]))
                    cancelled = pending_item.model_copy(
                        update={
                            "status": OutboxStatus.cancelled,
                            "updated_at": now,
                        }
                    )
                    connection.execute(
                        """
                        UPDATE collaborative_development_outbox
                        SET status='cancelled',item_json=?,updated_at=?
                        WHERE outbox_id=?
                        """,
                        (
                            _persisted_json(cancelled),
                            now.isoformat(),
                            str(cancelled.outbox_id),
                        ),
                    )
                    connection.execute(
                        "DELETE FROM collaborative_development_outbox_claims WHERE outbox_id=?",
                        (str(cancelled.outbox_id),),
                    )
            self._save_receipt(
                connection,
                operation="set_approval_mode",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    async def create_work_item(
        self,
        item: DevelopmentWorkItem,
        *,
        actor_role: str,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentWorkItem:
        self._require_initialized()
        return await asyncio.to_thread(
            self._create_work_item_sync,
            item,
            actor_role,
            actor_id,
            str(idempotency_key),
        )

    def _create_work_item_sync(
        self,
        item: DevelopmentWorkItem,
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        if item.status != WorkItemStatus.proposed or item.revision != 1 or item.lease_revision != 0:
            raise ValueError("new work item must be proposed at revision 1 without a lease")
        request_digest = _digest(item)
        now = utc_now()
        server_item = item.model_copy(
            update={
                "created_at": now,
                "updated_at": now,
            }
        )
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="create_work_item",
                scope_id=item.assignment_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentWorkItem,
            )
            if replay is not None:
                return replay
            assignment = self._assignment_conn(connection, item.assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            if not self._role_has(assignment, item.assigned_role, DevelopmentTaskRole.implementer):
                raise CollaborativeDevelopmentAuthorizationError(
                    "assigned agent does not have the implementer role"
                )
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM collaborative_development_work_items "
                    "WHERE assignment_id=?",
                    (str(item.assignment_id),),
                ).fetchone()[0]
            )
            if count >= assignment.budget.max_work_items:
                raise CollaborativeDevelopmentBudgetExceeded(
                    "assignment work item budget is exhausted"
                )
            if connection.execute(
                "SELECT 1 FROM collaborative_development_work_items WHERE work_item_id=?",
                (str(item.work_item_id),),
            ).fetchone():
                raise CollaborativeDevelopmentConflict("work item id already exists")
            for dependency in item.dependencies:
                dependency_row = connection.execute(
                    "SELECT assignment_id FROM collaborative_development_work_items "
                    "WHERE work_item_id=?",
                    (str(dependency),),
                ).fetchone()
                if dependency_row is None or str(dependency_row["assignment_id"]) != str(
                    item.assignment_id
                ):
                    raise CollaborativeDevelopmentConflict(
                        "work item dependency must exist in the same assignment"
                    )
            if item.parent_work_item_id is not None:
                parent = connection.execute(
                    "SELECT assignment_id FROM collaborative_development_work_items "
                    "WHERE work_item_id=?",
                    (str(item.parent_work_item_id),),
                ).fetchone()
                if parent is None or str(parent["assignment_id"]) != str(item.assignment_id):
                    raise CollaborativeDevelopmentConflict(
                        "parent work item must exist in the same assignment"
                    )
            autonomous = assignment.execution_mode == ExecutionMode.autonomous
            autonomous_dispatch = (
                autonomous
                and self._agent_invocation_budget_available(assignment)
                and self._dependencies_satisfied_conn(connection, server_item)
            )
            stored = (
                server_item.model_copy(
                    update={
                        "status": WorkItemStatus.awaiting_dispatch,
                        "revision": 2,
                        "updated_at": now,
                    }
                )
                if autonomous_dispatch
                else server_item
            )
            connection.execute(
                """
                INSERT INTO collaborative_development_work_items(
                  work_item_id,assignment_id,kind,assigned_role,status,
                  lease_revision,revision,dispatch_authorized,current_result_id,
                  payload_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(stored.work_item_id),
                    str(stored.assignment_id),
                    stored.kind.value,
                    stored.assigned_role.value,
                    stored.status.value,
                    stored.lease_revision,
                    stored.revision,
                    1 if autonomous_dispatch else 0,
                    None,
                    _persisted_json(stored),
                    stored.created_at.isoformat(),
                    stored.updated_at.isoformat(),
                ),
            )
            self._append_event_conn(
                connection,
                assignment_id=item.assignment_id,
                event_type="work_item.created",
                actor_role=actor_role,
                actor_id=actor_id,
                aggregate_type="work_item",
                aggregate_id=item.work_item_id,
                aggregate_revision=1,
                idempotency_key=idempotency_key,
                payload={
                    "kind": item.kind.value,
                    "assigned_role": item.assigned_role.value,
                    "status": WorkItemStatus.proposed.value,
                },
                created_at=now,
            )
            if autonomous_dispatch:
                self._append_event_conn(
                    connection,
                    assignment_id=item.assignment_id,
                    event_type="work_item.awaiting_dispatch",
                    actor_role="platform",
                    actor_id="autonomous-dispatcher",
                    aggregate_type="work_item",
                    aggregate_id=item.work_item_id,
                    aggregate_revision=stored.revision,
                    idempotency_key=f"{idempotency_key}:autonomous",
                    payload={"reason": "autonomous_assignment"},
                    created_at=now,
                )
                self._ensure_work_dispatch_outbox_conn(
                    connection,
                    assignment_id=item.assignment_id,
                    work_item=stored,
                    now=now,
                )
            self._save_receipt(
                connection,
                operation="create_work_item",
                scope_id=item.assignment_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=stored,
                created_at=now,
            )
            return stored

    async def get_work_item(self, work_item_id: UUID | str) -> DevelopmentWorkItem:
        self._require_initialized()
        return await asyncio.to_thread(self._get_work_item_sync, work_item_id)

    def _get_work_item_sync(self, work_item_id: UUID | str) -> DevelopmentWorkItem:
        connection = self._connect()
        try:
            return self._work_item_conn(connection, work_item_id)[0]
        finally:
            connection.close()

    async def list_work_items(self, assignment_id: UUID | str) -> list[DevelopmentWorkItem]:
        self._require_initialized()
        return await asyncio.to_thread(self._list_work_items_sync, assignment_id)

    def _list_work_items_sync(self, assignment_id: UUID | str) -> list[DevelopmentWorkItem]:
        connection = self._connect()
        try:
            self._assignment_conn(connection, assignment_id)
            rows = connection.execute(
                """
                SELECT payload_json FROM collaborative_development_work_items
                WHERE assignment_id=? ORDER BY created_at,work_item_id
                """,
                (str(assignment_id),),
            ).fetchall()
            return [_decode_model(DevelopmentWorkItem, str(row["payload_json"])) for row in rows]
        finally:
            connection.close()

    def _dependencies_satisfied_conn(
        self, connection: sqlite3.Connection, item: DevelopmentWorkItem
    ) -> bool:
        for dependency in item.dependencies:
            row = connection.execute(
                "SELECT status FROM collaborative_development_work_items "
                "WHERE work_item_id=? AND assignment_id=?",
                (str(dependency), str(item.assignment_id)),
            ).fetchone()
            if row is None or str(row["status"]) not in {"accepted", "closed"}:
                return False
        return True

    @staticmethod
    def _agent_invocation_budget_available(
        assignment: DevelopmentAssignment,
    ) -> bool:
        return (
            assignment.budget.max_commands > 0
            and assignment.budget.max_tool_calls > 0
            and assignment.budget.max_cost_usd > 0
        )

    def _promote_ready_autonomous_work_items_conn(
        self,
        connection: sqlite3.Connection,
        *,
        assignment: DevelopmentAssignment,
        now: datetime,
        idempotency_prefix: str,
    ) -> None:
        if (
            assignment.execution_mode != ExecutionMode.autonomous
            or not self._agent_invocation_budget_available(assignment)
        ):
            return
        rows = connection.execute(
            """
            SELECT payload_json
            FROM collaborative_development_work_items
            WHERE assignment_id=? AND status='proposed'
            ORDER BY created_at,work_item_id
            """,
            (str(assignment.assignment_id),),
        ).fetchall()
        for row in rows:
            item = _decode_model(
                DevelopmentWorkItem,
                str(row["payload_json"]),
            )
            if not self._dependencies_satisfied_conn(connection, item):
                continue
            promoted = item.model_copy(
                update={
                    "status": WorkItemStatus.awaiting_dispatch,
                    "revision": item.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_work_item_conn(
                connection,
                promoted,
                dispatch_authorized=True,
            )
            self._append_event_conn(
                connection,
                assignment_id=assignment.assignment_id,
                event_type="work_item.awaiting_dispatch",
                actor_role="platform",
                actor_id="autonomous-dependency-dispatcher",
                aggregate_type="work_item",
                aggregate_id=promoted.work_item_id,
                aggregate_revision=promoted.revision,
                idempotency_key=(f"{idempotency_prefix}:dependency-ready:{promoted.work_item_id}"),
                payload={"reason": "dependencies_accepted"},
                created_at=now,
            )
            self._ensure_work_dispatch_outbox_conn(
                connection,
                assignment_id=assignment.assignment_id,
                work_item=promoted,
                now=now,
            )

    async def dispatch_work_item(
        self,
        work_item_id: UUID | str,
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentWorkItem:
        self._require_initialized()
        return await asyncio.to_thread(
            self._dispatch_work_item_sync,
            UUID(str(work_item_id)),
            expected_revision,
            actor_id,
            str(idempotency_key),
        )

    def _dispatch_work_item_sync(
        self,
        work_item_id: UUID,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        request_digest = _digest({"expected_revision": expected_revision})
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="dispatch_work_item",
                scope_id=work_item_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentWorkItem,
            )
            if replay is not None:
                return replay
            item, _ = self._work_item_conn(connection, work_item_id)
            assignment = self._assignment_conn(connection, item.assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            if item.revision != expected_revision:
                raise CollaborativeDevelopmentConflict("work item revision compare-and-set failed")
            if item.status not in {
                WorkItemStatus.proposed,
                WorkItemStatus.rework,
                WorkItemStatus.awaiting_dispatch,
            }:
                raise CollaborativeDevelopmentInvalidState(
                    f"cannot dispatch work item from {item.status.value}"
                )
            if item.status == WorkItemStatus.awaiting_dispatch:
                existing_generation = connection.execute(
                    """
                    SELECT 1 FROM collaborative_development_outbox
                    WHERE work_item_id=? AND kind='work_dispatch'
                      AND status IN ('pending','delivered')
                      AND CAST(
                        json_extract(payload_json,'$.work_item_revision') AS INTEGER
                      )=?
                    LIMIT 1
                    """,
                    (str(work_item_id), item.revision),
                ).fetchone()
                if existing_generation is not None:
                    raise CollaborativeDevelopmentConflict(
                        "this work item generation is already dispatched"
                    )
            if not self._dependencies_satisfied_conn(connection, item):
                raise CollaborativeDevelopmentInvalidState(
                    "work item dependencies are not accepted"
                )
            updated = item.model_copy(
                update={
                    "status": WorkItemStatus.awaiting_dispatch,
                    "revision": item.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_work_item_conn(connection, updated, dispatch_authorized=True)
            self._append_event_conn(
                connection,
                assignment_id=item.assignment_id,
                event_type="work_item.awaiting_dispatch",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="work_item",
                aggregate_id=work_item_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={"manual_dispatch": True},
                created_at=now,
            )
            self._ensure_work_dispatch_outbox_conn(
                connection,
                assignment_id=item.assignment_id,
                work_item=updated,
                now=now,
            )
            self._save_receipt(
                connection,
                operation="dispatch_work_item",
                scope_id=work_item_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    async def acquire_lease(
        self,
        work_item_id: UUID | str,
        *,
        owner_role: AgentRole,
        owner_id: str,
        expected_revision: int,
        ttl_seconds: int = 900,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentLease:
        self._require_initialized()
        return await asyncio.to_thread(
            self._acquire_lease_sync,
            UUID(str(work_item_id)),
            owner_role,
            owner_id,
            expected_revision,
            ttl_seconds,
            str(idempotency_key),
        )

    def _acquire_lease_sync(
        self,
        work_item_id: UUID,
        owner_role: AgentRole,
        owner_id: str,
        expected_revision: int,
        ttl_seconds: int,
        idempotency_key: str,
    ) -> DevelopmentLease:
        if not 1 <= ttl_seconds <= 3_600:
            raise ValueError("lease ttl_seconds must be between 1 and 3600")
        request_digest = _digest(
            {
                "owner_role": owner_role.value,
                "expected_revision": expected_revision,
                "ttl_seconds": ttl_seconds,
            }
        )
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="acquire_lease",
                scope_id=work_item_id,
                actor_role=owner_role.value,
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentLease,
            )
            if replay is not None:
                return replay
            self._recover_expired_leases_conn(connection, now=now)
            item, row = self._work_item_conn(connection, work_item_id)
            assignment = self._assignment_conn(connection, item.assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            if item.revision != expected_revision:
                raise CollaborativeDevelopmentConflict("work item revision compare-and-set failed")
            if item.status != WorkItemStatus.awaiting_dispatch:
                raise CollaborativeDevelopmentInvalidState("work item is not awaiting dispatch")
            if int(row["dispatch_authorized"]) != 1:
                raise CollaborativeDevelopmentAuthorizationError(
                    "manual work item has not been dispatched"
                )
            if owner_role != item.assigned_role or not self._role_has(
                assignment, owner_role, DevelopmentTaskRole.implementer
            ):
                raise CollaborativeDevelopmentAuthorizationError(
                    "lease owner is not the assigned implementer"
                )
            if not self._dependencies_satisfied_conn(connection, item):
                raise CollaborativeDevelopmentInvalidState(
                    "work item dependencies are not accepted"
                )
            fence = item.lease_revision + 1
            resulting_revision = item.revision + 1
            lease = DevelopmentLease(
                lease_id=uuid5(
                    NAMESPACE_URL,
                    f"lilies:collaborative-development:lease:{work_item_id}:{fence}:{owner_id}",
                ),
                assignment_id=item.assignment_id,
                work_item_id=work_item_id,
                owner_role=owner_role,
                owner_id=owner_id,
                fence=fence,
                work_item_revision=resulting_revision,
                status=LeaseStatus.active,
                acquired_at=now,
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            updated = item.model_copy(
                update={
                    "status": WorkItemStatus.leased,
                    "lease_revision": fence,
                    "revision": resulting_revision,
                    "updated_at": now,
                }
            )
            try:
                connection.execute(
                    """
                    INSERT INTO collaborative_development_leases(
                      lease_id,assignment_id,work_item_id,owner_role,owner_id,
                      fence,work_item_revision,status,payload_json,acquired_at,
                      expires_at,released_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(lease.lease_id),
                        str(lease.assignment_id),
                        str(lease.work_item_id),
                        lease.owner_role.value,
                        lease.owner_id,
                        lease.fence,
                        lease.work_item_revision,
                        lease.status.value,
                        _persisted_json(lease),
                        lease.acquired_at.isoformat(),
                        lease.expires_at.isoformat(),
                        None,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CollaborativeDevelopmentConflict(
                    "work item already has an active lease"
                ) from error
            self._update_work_item_conn(connection, updated, dispatch_authorized=False)
            self._append_event_conn(
                connection,
                assignment_id=item.assignment_id,
                event_type="work_item.leased",
                actor_role=owner_role.value,
                actor_id=owner_id,
                aggregate_type="work_item",
                aggregate_id=work_item_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={"lease_id": str(lease.lease_id), "fence": fence},
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="acquire_lease",
                scope_id=work_item_id,
                actor_role=owner_role.value,
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=lease,
                created_at=now,
            )
            return lease

    async def start_work(
        self,
        lease_id: UUID | str,
        *,
        owner_id: str,
        expected_work_item_revision: int,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentWorkItem:
        self._require_initialized()
        return await asyncio.to_thread(
            self._start_work_sync,
            UUID(str(lease_id)),
            owner_id,
            expected_work_item_revision,
            str(idempotency_key),
        )

    def _start_work_sync(
        self,
        lease_id: UUID,
        owner_id: str,
        expected_work_item_revision: int,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        request_digest = _digest({"expected_work_item_revision": expected_work_item_revision})
        now = utc_now()
        with self._transaction() as connection:
            lease = self._lease_conn(connection, lease_id)
            replay = self._receipt(
                connection,
                operation="start_work",
                scope_id=lease_id,
                actor_role=lease.owner_role.value,
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentWorkItem,
            )
            if replay is not None:
                return replay
            self._validate_active_lease(lease, owner_id=owner_id, now=now)
            item, _ = self._work_item_conn(connection, lease.work_item_id)
            if item.revision != expected_work_item_revision:
                raise CollaborativeDevelopmentConflict("work item revision compare-and-set failed")
            if item.status != WorkItemStatus.leased or item.lease_revision != lease.fence:
                raise CollaborativeDevelopmentInvalidState(
                    "lease fence no longer owns the work item"
                )
            updated = item.model_copy(
                update={
                    "status": WorkItemStatus.working,
                    "revision": item.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_work_item_conn(connection, updated)
            self._append_event_conn(
                connection,
                assignment_id=item.assignment_id,
                event_type="work_item.working",
                actor_role=lease.owner_role.value,
                actor_id=owner_id,
                aggregate_type="work_item",
                aggregate_id=item.work_item_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={"lease_id": str(lease_id), "fence": lease.fence},
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="start_work",
                scope_id=lease_id,
                actor_role=lease.owner_role.value,
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    @staticmethod
    def _validate_active_lease(lease: DevelopmentLease, *, owner_id: str, now: datetime) -> None:
        if lease.owner_id != owner_id:
            raise CollaborativeDevelopmentAuthorizationError("lease owner mismatch")
        if lease.status != LeaseStatus.active:
            raise CollaborativeDevelopmentInvalidState("lease is not active")
        if lease.expires_at <= now:
            raise CollaborativeDevelopmentInvalidState("lease has expired")

    @staticmethod
    def _path_is_granted(path: str, allowed_paths: tuple[str, ...]) -> bool:
        candidate = PurePosixPath(path)
        return any(
            candidate == PurePosixPath(grant) or PurePosixPath(grant) in candidate.parents
            for grant in allowed_paths
        )

    def _validate_result_authority(
        self,
        assignment: DevelopmentAssignment,
        result: DevelopmentResult,
    ) -> None:
        if result.baseline_commit != assignment.baseline_commit:
            raise CollaborativeDevelopmentAuthorizationError(
                "result baseline does not match the frozen assignment baseline"
            )
        grant = next(
            (
                candidate
                for candidate in assignment.workspace_grants
                if candidate.agent_role == result.agent_role
            ),
            None,
        )
        if grant is None:
            raise CollaborativeDevelopmentAuthorizationError("result role has no workspace grant")
        allowed_commands = set(grant.allowed_argv)
        for command in result.commands:
            if command.argv not in allowed_commands:
                raise CollaborativeDevelopmentAuthorizationError(
                    f"command argv is outside the frozen grant: {command.argv!r}"
                )
            if not self._path_is_granted(command.cwd, grant.allowed_paths):
                raise CollaborativeDevelopmentAuthorizationError(
                    f"command cwd is outside the frozen grant: {command.cwd}"
                )

    def _validate_review_authority(
        self,
        assignment: DevelopmentAssignment,
        review: LiliesReview,
    ) -> None:
        grant = next(
            (
                candidate
                for candidate in assignment.workspace_grants
                if candidate.agent_role == AgentRole.lilies
            ),
            None,
        )
        if grant is None:
            raise CollaborativeDevelopmentAuthorizationError("Lilies has no review workspace grant")
        allowed_commands = set(grant.allowed_argv)
        for command in review.verification_commands:
            if command.argv not in allowed_commands:
                raise CollaborativeDevelopmentAuthorizationError(
                    f"review argv is outside the frozen grant: {command.argv!r}"
                )
            if not self._path_is_granted(command.cwd, grant.allowed_paths):
                raise CollaborativeDevelopmentAuthorizationError(
                    f"review cwd is outside the frozen grant: {command.cwd}"
                )

    @staticmethod
    def _requires_metered_tool_usage_conn(
        connection: sqlite3.Connection,
        assignment_id: UUID,
    ) -> bool:
        return (
            connection.execute(
                """
                SELECT 1
                FROM collaborative_development_tool_metering_requirements
                WHERE assignment_id=?
                UNION ALL
                SELECT 1
                FROM collaborative_development_tool_usage
                WHERE assignment_id=?
                LIMIT 1
                """,
                (str(assignment_id), str(assignment_id)),
            ).fetchone()
            is not None
        )

    def _bind_metered_command_receipts_conn(
        self,
        connection: sqlite3.Connection,
        *,
        assignment_id: UUID,
        actor_role: AgentRole,
        commands: tuple[CommandReceipt, ...],
        consumer_type: Literal["result", "review"],
        consumer_id: UUID,
    ) -> None:
        """Bind submitted receipts to completed, server-metered commands.

        Reservations are created before process execution and completed by the
        trusted tool wrapper.  A result or review cannot create command budget
        after the fact, reuse another submission's command, or claim a receipt
        that was never observed by the tool service.
        """

        rows = connection.execute(
            """
            SELECT reservation_id,record_json
            FROM collaborative_development_tool_usage
            WHERE assignment_id=? AND actor_role=? AND commands=1
              AND status='completed' AND consumer_id IS NULL
            ORDER BY reserved_at,reservation_id
            """,
            (str(assignment_id), actor_role.value),
        ).fetchall()
        available = [
            _decode_model(
                DevelopmentToolUsageRecord,
                str(row["record_json"]),
            )
            for row in rows
        ]
        selected: list[DevelopmentToolUsageRecord] = []
        for receipt in commands:
            match_index = next(
                (
                    index
                    for index, record in enumerate(available)
                    if record.command_argv == receipt.argv
                    and record.command_cwd == receipt.cwd
                    and record.output_digest == receipt.output_digest
                    and record.completed_at is not None
                    and receipt.started_at <= record.reserved_at
                    and record.completed_at <= receipt.finished_at
                ),
                None,
            )
            if match_index is None:
                raise CollaborativeDevelopmentConflict(
                    "command receipt is not bound to a completed trusted usage record"
                )
            selected.append(available.pop(match_index))

        for record in selected:
            bound = record.model_copy(
                update={
                    "consumer_type": consumer_type,
                    "consumer_id": consumer_id,
                }
            )
            updated = connection.execute(
                """
                UPDATE collaborative_development_tool_usage
                SET consumer_type=?,consumer_id=?,record_json=?
                WHERE reservation_id=? AND consumer_id IS NULL
                """,
                (
                    consumer_type,
                    str(consumer_id),
                    _persisted_json(bound),
                    str(record.reservation_id),
                ),
            )
            if updated.rowcount != 1:
                raise CollaborativeDevelopmentConflict(
                    "command usage was concurrently bound to another submission"
                )

    @staticmethod
    def _legacy_submitted_command_count_conn(
        connection: sqlite3.Connection,
        assignment_id: UUID,
    ) -> int:
        result_count = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(json_array_length(
                  json_extract(payload_json,'$.commands')
                )),0)
                FROM collaborative_development_results WHERE assignment_id=?
                """,
                (str(assignment_id),),
            ).fetchone()[0]
        )
        review_count = int(
            connection.execute(
                """
                SELECT COALESCE(SUM(json_array_length(
                  json_extract(payload_json,'$.verification_commands')
                )),0)
                FROM collaborative_development_reviews WHERE assignment_id=?
                """,
                (str(assignment_id),),
            ).fetchone()[0]
        )
        return result_count + review_count

    async def abort_work(
        self,
        lease_id: UUID | str,
        *,
        owner_id: str,
        expected_work_item_revision: int,
        reason: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentWorkItem:
        """Release the current fence and return its item to the same outbox.

        This transition is used only when an autonomous role handler returns a
        retry/authorization outcome or an invalid lifecycle payload.  It never
        creates another outbox row and never changes assignment authority.
        """

        self._require_initialized()
        return await asyncio.to_thread(
            self._abort_work_sync,
            UUID(str(lease_id)),
            owner_id,
            expected_work_item_revision,
            reason,
            str(idempotency_key),
        )

    def _abort_work_sync(
        self,
        lease_id: UUID,
        owner_id: str,
        expected_work_item_revision: int,
        reason: str,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        normalized_reason = reason.strip()
        if not normalized_reason or len(normalized_reason) > 20_000:
            raise ValueError("work abort reason must contain 1 to 20000 characters")
        request_digest = _digest(
            {
                "expected_work_item_revision": expected_work_item_revision,
                "reason": normalized_reason,
            }
        )
        now = utc_now()
        with self._transaction() as connection:
            lease = self._lease_conn(connection, lease_id)
            replay = self._receipt(
                connection,
                operation="abort_work",
                scope_id=lease_id,
                actor_role=lease.owner_role.value,
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentWorkItem,
            )
            if replay is not None:
                return replay
            self._validate_active_lease(lease, owner_id=owner_id, now=now)
            item, _ = self._work_item_conn(connection, lease.work_item_id)
            if item.revision != expected_work_item_revision:
                raise CollaborativeDevelopmentConflict("work item revision compare-and-set failed")
            if (
                item.status not in {WorkItemStatus.leased, WorkItemStatus.working}
                or item.lease_revision != lease.fence
            ):
                raise CollaborativeDevelopmentInvalidState(
                    "work abort requires the current lease fence"
                )
            released = lease.model_copy(update={"status": LeaseStatus.released, "released_at": now})
            connection.execute(
                """
                UPDATE collaborative_development_leases
                SET status='released',payload_json=?,released_at=? WHERE lease_id=?
                """,
                (_persisted_json(released), now.isoformat(), str(lease.lease_id)),
            )
            updated = item.model_copy(
                update={
                    "status": WorkItemStatus.awaiting_dispatch,
                    "revision": item.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_work_item_conn(
                connection,
                updated,
                dispatch_authorized=True,
            )
            self._append_event_conn(
                connection,
                assignment_id=item.assignment_id,
                event_type="work_item.execution_aborted",
                actor_role=lease.owner_role.value,
                actor_id=owner_id,
                aggregate_type="work_item",
                aggregate_id=item.work_item_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={
                    "lease_id": str(lease.lease_id),
                    "fence": lease.fence,
                    "reason": normalized_reason,
                },
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="abort_work",
                scope_id=lease_id,
                actor_role=lease.owner_role.value,
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    async def get_result(self, result_id: UUID | str) -> DevelopmentResult:
        self._require_initialized()
        return await asyncio.to_thread(self._get_result_sync, UUID(str(result_id)))

    def _get_result_sync(self, result_id: UUID) -> DevelopmentResult:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT payload_json FROM collaborative_development_results
                WHERE result_id=?
                """,
                (str(result_id),),
            ).fetchone()
            if row is None:
                raise CollaborativeDevelopmentNotFound("development result not found")
            return _decode_model(DevelopmentResult, str(row["payload_json"]))
        finally:
            connection.close()

    async def submit_result(
        self,
        result: DevelopmentResult,
        *,
        owner_id: str,
        expected_work_item_revision: int,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentWorkItem:
        self._require_initialized()
        return await asyncio.to_thread(
            self._submit_result_sync,
            result,
            owner_id,
            expected_work_item_revision,
            str(idempotency_key),
        )

    def _submit_result_sync(
        self,
        result: DevelopmentResult,
        owner_id: str,
        expected_work_item_revision: int,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        request_digest = _digest(
            {
                "result": result,
                "expected_work_item_revision": expected_work_item_revision,
            }
        )
        now = utc_now()
        server_result = result.model_copy(update={"created_at": now})
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="submit_result",
                scope_id=result.work_item_id,
                actor_role=result.agent_role.value,
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentWorkItem,
            )
            if replay is not None:
                return replay
            item, _ = self._work_item_conn(connection, result.work_item_id)
            assignment = self._assignment_conn(connection, item.assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            lease = self._lease_conn(connection, result.lease_id)
            self._validate_active_lease(lease, owner_id=owner_id, now=now)
            if (
                result.assignment_id != item.assignment_id
                or lease.assignment_id != item.assignment_id
                or lease.work_item_id != item.work_item_id
                or result.agent_role != item.assigned_role
                or result.agent_role != lease.owner_role
            ):
                raise CollaborativeDevelopmentAuthorizationError(
                    "result, work item, role, and lease binding mismatch"
                )
            if item.revision != expected_work_item_revision:
                raise CollaborativeDevelopmentConflict("work item revision compare-and-set failed")
            if item.status != WorkItemStatus.working or item.lease_revision != lease.fence:
                raise CollaborativeDevelopmentInvalidState(
                    "result requires the current working lease fence"
                )
            self._validate_result_authority(assignment, server_result)
            if connection.execute(
                "SELECT 1 FROM collaborative_development_results WHERE result_id=?",
                (str(result.result_id),),
            ).fetchone():
                raise CollaborativeDevelopmentConflict("result id already exists")
            if self._requires_metered_tool_usage_conn(
                connection,
                item.assignment_id,
            ):
                self._bind_metered_command_receipts_conn(
                    connection,
                    assignment_id=item.assignment_id,
                    actor_role=result.agent_role,
                    commands=server_result.commands,
                    consumer_type="result",
                    consumer_id=result.result_id,
                )
            else:
                used_commands = self._legacy_submitted_command_count_conn(
                    connection,
                    item.assignment_id,
                )
                if used_commands + len(result.commands) > assignment.budget.max_commands:
                    raise CollaborativeDevelopmentBudgetExceeded(
                        "assignment command budget would be exceeded"
                    )
            resulting_revision = item.revision + 1
            connection.execute(
                """
                INSERT INTO collaborative_development_results(
                  result_id,assignment_id,work_item_id,lease_id,
                  work_item_revision,payload_json,created_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    str(result.result_id),
                    str(result.assignment_id),
                    str(result.work_item_id),
                    str(result.lease_id),
                    resulting_revision,
                    _persisted_json(server_result),
                    server_result.created_at.isoformat(),
                ),
            )
            released = lease.model_copy(update={"status": LeaseStatus.released, "released_at": now})
            connection.execute(
                """
                UPDATE collaborative_development_leases
                SET status='released',payload_json=?,released_at=? WHERE lease_id=?
                """,
                (_persisted_json(released), now.isoformat(), str(lease.lease_id)),
            )
            updated = item.model_copy(
                update={
                    "status": WorkItemStatus.ready_for_lilies_review,
                    "revision": resulting_revision,
                    "updated_at": now,
                }
            )
            self._update_work_item_conn(connection, updated, current_result_id=result.result_id)
            self._append_event_conn(
                connection,
                assignment_id=item.assignment_id,
                event_type="work_item.result_submitted",
                actor_role=result.agent_role.value,
                actor_id=owner_id,
                aggregate_type="work_item",
                aggregate_id=item.work_item_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={
                    "result_id": str(result.result_id),
                    "diff_digest": result.diff_digest,
                    "tests": len(server_result.tests),
                },
                created_at=now,
            )
            if (
                assignment.execution_mode == ExecutionMode.autonomous
                or assignment.approval_mode == ApprovalMode.auto_forward
            ) and self._agent_invocation_budget_available(assignment):
                self._ensure_lilies_review_outbox_conn(
                    connection,
                    assignment_id=item.assignment_id,
                    work_item_id=item.work_item_id,
                    result_id=result.result_id,
                    work_item_revision=updated.revision,
                    now=now,
                )
            self._save_receipt(
                connection,
                operation="submit_result",
                scope_id=item.work_item_id,
                actor_role=result.agent_role.value,
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    async def submit_review(
        self,
        review: LiliesReview,
        *,
        reviewer_id: str,
        expected_work_item_revision: int,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentWorkItem:
        self._require_initialized()
        return await asyncio.to_thread(
            self._submit_review_sync,
            review,
            reviewer_id,
            expected_work_item_revision,
            str(idempotency_key),
        )

    def _submit_review_sync(
        self,
        review: LiliesReview,
        reviewer_id: str,
        expected_work_item_revision: int,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        request_digest = _digest(
            {
                "review": review,
                "expected_work_item_revision": expected_work_item_revision,
            }
        )
        now = utc_now()
        server_review = review.model_copy(update={"created_at": now})
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="submit_review",
                scope_id=review.work_item_id,
                actor_role=AgentRole.lilies.value,
                actor_id=reviewer_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentWorkItem,
            )
            if replay is not None:
                return replay
            item, row = self._work_item_conn(connection, review.work_item_id)
            assignment = self._assignment_conn(connection, item.assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            if not self._role_has(assignment, AgentRole.lilies, DevelopmentTaskRole.reviewer):
                raise CollaborativeDevelopmentAuthorizationError(
                    "Lilies does not have the reviewer role"
                )
            if (
                review.assignment_id != item.assignment_id
                or item.revision != expected_work_item_revision
            ):
                raise CollaborativeDevelopmentConflict(
                    "review binding or work item compare-and-set failed"
                )
            if item.status != WorkItemStatus.ready_for_lilies_review:
                raise CollaborativeDevelopmentInvalidState(
                    "work item is not ready for Lilies review"
                )
            if str(row["current_result_id"]) != str(review.result_id):
                raise CollaborativeDevelopmentConflict("review does not target the current result")
            result_row = connection.execute(
                """
                SELECT payload_json FROM collaborative_development_results
                WHERE result_id=? AND assignment_id=? AND work_item_id=?
                """,
                (
                    str(review.result_id),
                    str(review.assignment_id),
                    str(review.work_item_id),
                ),
            ).fetchone()
            if result_row is None:
                raise CollaborativeDevelopmentConflict(
                    "review result binding is not present in durable storage"
                )
            current_result = _decode_model(
                DevelopmentResult,
                str(result_row["payload_json"]),
            )
            reviewed_criteria = tuple(check.criterion for check in review.acceptance_checks)
            if reviewed_criteria != item.acceptance:
                raise CollaborativeDevelopmentConflict(
                    "review must evaluate every frozen acceptance criterion in order"
                )
            if review.verdict == ReviewVerdict.accepted:
                if (
                    not all(check.passed for check in review.acceptance_checks)
                    or any(command.exit_code != 0 for command in review.verification_commands)
                    or review.next_requirements
                ):
                    raise CollaborativeDevelopmentConflict(
                        "accepted review requires passing checks, zero-exit "
                        "verification commands, and no rework request"
                    )
                required_evidence = {
                    current_result.diff_digest,
                    *(command.output_digest for command in review.verification_commands),
                    *(
                        evidence
                        for check in review.acceptance_checks
                        for evidence in check.evidence_refs
                    ),
                }
                if not required_evidence.issubset(set(review.evidence_refs)):
                    raise CollaborativeDevelopmentConflict(
                        "accepted review evidence is not bound to the current result "
                        "and every successful verification"
                    )
            self._validate_review_authority(assignment, server_review)
            if connection.execute(
                "SELECT 1 FROM collaborative_development_reviews WHERE review_id=?",
                (str(review.review_id),),
            ).fetchone():
                raise CollaborativeDevelopmentConflict("review id already exists")
            if self._requires_metered_tool_usage_conn(
                connection,
                item.assignment_id,
            ):
                self._bind_metered_command_receipts_conn(
                    connection,
                    assignment_id=item.assignment_id,
                    actor_role=AgentRole.lilies,
                    commands=server_review.verification_commands,
                    consumer_type="review",
                    consumer_id=review.review_id,
                )
            else:
                used_commands = self._legacy_submitted_command_count_conn(
                    connection,
                    item.assignment_id,
                )
                if (
                    used_commands + len(review.verification_commands)
                    > assignment.budget.max_commands
                ):
                    raise CollaborativeDevelopmentBudgetExceeded(
                        "assignment command budget would be exceeded by review"
                    )
            first_revision = item.revision + 1
            first_status = (
                WorkItemStatus.accepted
                if review.verdict == ReviewVerdict.accepted
                else WorkItemStatus.rework
            )
            first = item.model_copy(
                update={
                    "status": first_status,
                    "revision": first_revision,
                    "updated_at": now,
                }
            )
            connection.execute(
                """
                INSERT INTO collaborative_development_reviews(
                  review_id,assignment_id,work_item_id,result_id,
                  work_item_revision,verdict,payload_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    str(review.review_id),
                    str(review.assignment_id),
                    str(review.work_item_id),
                    str(review.result_id),
                    first_revision,
                    review.verdict.value,
                    _persisted_json(server_review),
                    server_review.created_at.isoformat(),
                ),
            )
            self._update_work_item_conn(connection, first, dispatch_authorized=False)
            self._append_event_conn(
                connection,
                assignment_id=item.assignment_id,
                event_type=f"work_item.{review.verdict.value}",
                actor_role=AgentRole.lilies.value,
                actor_id=reviewer_id,
                aggregate_type="work_item",
                aggregate_id=item.work_item_id,
                aggregate_revision=first.revision,
                idempotency_key=idempotency_key,
                payload={
                    "review_id": str(review.review_id),
                    "result_id": str(review.result_id),
                },
                created_at=now,
            )
            final = first
            if review.verdict == ReviewVerdict.rework:
                final = first.model_copy(
                    update={
                        "status": WorkItemStatus.awaiting_dispatch,
                        "revision": first.revision + 1,
                    }
                )
                autonomous = (
                    assignment.execution_mode == ExecutionMode.autonomous
                    and self._agent_invocation_budget_available(assignment)
                    and self._dependencies_satisfied_conn(connection, first)
                )
                self._update_work_item_conn(connection, final, dispatch_authorized=autonomous)
                self._append_event_conn(
                    connection,
                    assignment_id=item.assignment_id,
                    event_type="work_item.awaiting_dispatch",
                    actor_role="platform",
                    actor_id="collaborative-development-state-machine",
                    aggregate_type="work_item",
                    aggregate_id=item.work_item_id,
                    aggregate_revision=final.revision,
                    idempotency_key=f"{idempotency_key}:requeue",
                    payload={"after_review": "rework", "autonomous": autonomous},
                    created_at=now,
                )
                if autonomous:
                    self._ensure_work_dispatch_outbox_conn(
                        connection,
                        assignment_id=item.assignment_id,
                        work_item=final,
                        extra_payload={
                            "review_id": str(review.review_id),
                        },
                        now=now,
                    )
            else:
                self._promote_ready_autonomous_work_items_conn(
                    connection,
                    assignment=assignment,
                    now=now,
                    idempotency_prefix=idempotency_key,
                )
            self._save_receipt(
                connection,
                operation="submit_review",
                scope_id=item.work_item_id,
                actor_role=AgentRole.lilies.value,
                actor_id=reviewer_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=final,
                created_at=now,
            )
            return final

    async def close_work_item(
        self,
        work_item_id: UUID | str,
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentWorkItem:
        self._require_initialized()
        return await asyncio.to_thread(
            self._close_work_item_sync,
            UUID(str(work_item_id)),
            expected_revision,
            actor_id,
            str(idempotency_key),
        )

    def _close_work_item_sync(
        self,
        work_item_id: UUID,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentWorkItem:
        request_digest = _digest({"expected_revision": expected_revision})
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="close_work_item",
                scope_id=work_item_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentWorkItem,
            )
            if replay is not None:
                return replay
            item, _ = self._work_item_conn(connection, work_item_id)
            if item.revision != expected_revision:
                raise CollaborativeDevelopmentConflict("work item revision compare-and-set failed")
            if item.status != WorkItemStatus.accepted:
                raise CollaborativeDevelopmentInvalidState("only accepted work items can be closed")
            updated = item.model_copy(
                update={
                    "status": WorkItemStatus.closed,
                    "revision": item.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_work_item_conn(connection, updated)
            self._append_event_conn(
                connection,
                assignment_id=item.assignment_id,
                event_type="work_item.closed",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="work_item",
                aggregate_id=item.work_item_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={},
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="close_work_item",
                scope_id=work_item_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    async def stop_assignment(
        self,
        assignment_id: UUID | str,
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentAssignment:
        self._require_initialized()
        return await asyncio.to_thread(
            self._stop_assignment_sync,
            UUID(str(assignment_id)),
            expected_revision,
            actor_id,
            str(idempotency_key),
        )

    def _stop_assignment_sync(
        self,
        assignment_id: UUID,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        request_digest = _digest({"expected_revision": expected_revision})
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="stop_assignment",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentAssignment,
            )
            if replay is not None:
                return replay
            assignment = self._assignment_conn(connection, assignment_id)
            if assignment.revision != expected_revision:
                raise CollaborativeDevelopmentConflict("assignment revision compare-and-set failed")
            if assignment.status != AssignmentStatus.active:
                raise CollaborativeDevelopmentInvalidState("only active assignments can be stopped")
            updated = assignment.model_copy(
                update={
                    "status": AssignmentStatus.stopped,
                    "revision": assignment.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_assignment_conn(connection, updated)
            cancellable_rows = connection.execute(
                """
                SELECT payload_json FROM collaborative_development_work_items
                WHERE assignment_id=?
                  AND status NOT IN ('accepted','closed','cancelled')
                ORDER BY created_at,work_item_id
                """,
                (str(assignment_id),),
            ).fetchall()
            cancelled_work_items: list[DevelopmentWorkItem] = []
            for row in cancellable_rows:
                work_item = _decode_model(
                    DevelopmentWorkItem,
                    str(row["payload_json"]),
                )
                cancelled_work_item = work_item.model_copy(
                    update={
                        "status": WorkItemStatus.cancelled,
                        "revision": work_item.revision + 1,
                        "updated_at": now,
                    }
                )
                self._update_work_item_conn(
                    connection,
                    cancelled_work_item,
                    dispatch_authorized=False,
                )
                self._append_event_conn(
                    connection,
                    assignment_id=assignment_id,
                    event_type="work_item.cancelled",
                    actor_role="user",
                    actor_id=actor_id,
                    aggregate_type="work_item",
                    aggregate_id=work_item.work_item_id,
                    aggregate_revision=cancelled_work_item.revision,
                    idempotency_key=(f"stop-cancel:{assignment_id}:{work_item.work_item_id}"),
                    payload={"reason": "assignment_stopped"},
                    created_at=now,
                )
                cancelled_work_items.append(cancelled_work_item)
            active_leases = connection.execute(
                """
                SELECT payload_json FROM collaborative_development_leases
                WHERE assignment_id=? AND status='active'
                """,
                (str(assignment_id),),
            ).fetchall()
            for row in active_leases:
                lease = _decode_model(DevelopmentLease, str(row["payload_json"]))
                revoked = lease.model_copy(
                    update={"status": LeaseStatus.revoked, "released_at": now}
                )
                connection.execute(
                    """
                    UPDATE collaborative_development_leases
                    SET status='revoked',payload_json=?,released_at=? WHERE lease_id=?
                    """,
                    (_persisted_json(revoked), now.isoformat(), str(lease.lease_id)),
                )
            pending = connection.execute(
                """
                SELECT item_json FROM collaborative_development_outbox
                WHERE assignment_id=? AND status='pending'
                """,
                (str(assignment_id),),
            ).fetchall()
            for row in pending:
                item = _decode_model(DispatchOutboxItem, str(row["item_json"]))
                cancelled = item.model_copy(
                    update={"status": OutboxStatus.cancelled, "updated_at": now}
                )
                connection.execute(
                    """
                    UPDATE collaborative_development_outbox
                    SET status='cancelled',item_json=?,updated_at=? WHERE outbox_id=?
                    """,
                    (_persisted_json(cancelled), now.isoformat(), str(item.outbox_id)),
                )
            connection.execute(
                """
                DELETE FROM collaborative_development_outbox_claims
                WHERE outbox_id IN (
                  SELECT outbox_id FROM collaborative_development_outbox
                  WHERE assignment_id=?
                )
                """,
                (str(assignment_id),),
            )
            self._append_event_conn(
                connection,
                assignment_id=assignment_id,
                event_type="assignment.stopped",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="assignment",
                aggregate_id=assignment_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={
                    "revoked_leases": len(active_leases),
                    "cancelled_outbox": len(pending),
                    "cancelled_work_items": len(cancelled_work_items),
                },
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="stop_assignment",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    async def archive_assignment(
        self,
        assignment_id: UUID | str,
        *,
        expected_revision: int,
        actor_id: str,
        idempotency_key: IdempotencyKey,
    ) -> DevelopmentAssignment:
        self._require_initialized()
        return await asyncio.to_thread(
            self._archive_assignment_sync,
            UUID(str(assignment_id)),
            expected_revision,
            actor_id,
            str(idempotency_key),
        )

    def _archive_assignment_sync(
        self,
        assignment_id: UUID,
        expected_revision: int,
        actor_id: str,
        idempotency_key: str,
    ) -> DevelopmentAssignment:
        request_digest = _digest({"expected_revision": expected_revision})
        now = utc_now()
        with self._transaction() as connection:
            replay = self._receipt(
                connection,
                operation="archive_assignment",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=DevelopmentAssignment,
            )
            if replay is not None:
                return replay
            assignment = self._assignment_conn(connection, assignment_id)
            if assignment.revision != expected_revision:
                raise CollaborativeDevelopmentConflict("assignment revision compare-and-set failed")
            if assignment.status != AssignmentStatus.stopped:
                raise CollaborativeDevelopmentInvalidState(
                    "only stopped assignments can be archived"
                )
            open_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM collaborative_development_work_items
                    WHERE assignment_id=?
                      AND status NOT IN ('accepted','closed','cancelled')
                    """,
                    (str(assignment_id),),
                ).fetchone()[0]
            )
            if open_count:
                raise CollaborativeDevelopmentInvalidState(
                    "assignment has work items without a completed or cancelled outcome"
                )
            updated = assignment.model_copy(
                update={
                    "status": AssignmentStatus.archived,
                    "revision": assignment.revision + 1,
                    "updated_at": now,
                }
            )
            self._update_assignment_conn(connection, updated)
            self._append_event_conn(
                connection,
                assignment_id=assignment_id,
                event_type="assignment.archived",
                actor_role="user",
                actor_id=actor_id,
                aggregate_type="assignment",
                aggregate_id=assignment_id,
                aggregate_revision=updated.revision,
                idempotency_key=idempotency_key,
                payload={
                    "enterprise_denominator": False,
                    "history_readable": True,
                    "terminal_work_item_statuses": [
                        WorkItemStatus.accepted.value,
                        WorkItemStatus.closed.value,
                        WorkItemStatus.cancelled.value,
                    ],
                },
                created_at=now,
            )
            self._save_receipt(
                connection,
                operation="archive_assignment",
                scope_id=assignment_id,
                actor_role="user",
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=updated,
                created_at=now,
            )
            return updated

    async def recover_expired_leases(
        self, *, now: datetime | None = None
    ) -> list[DevelopmentLease]:
        self._require_initialized()
        return await asyncio.to_thread(self._recover_expired_leases_sync, _as_utc(now))

    def _recover_expired_leases_sync(self, now: datetime) -> list[DevelopmentLease]:
        with self._transaction() as connection:
            return self._recover_expired_leases_conn(connection, now=now)

    def _recover_expired_leases_conn(
        self, connection: sqlite3.Connection, *, now: datetime
    ) -> list[DevelopmentLease]:
        rows = connection.execute(
            """
            SELECT payload_json FROM collaborative_development_leases
            WHERE status='active' AND expires_at<=?
            ORDER BY expires_at,lease_id
            """,
            (now.isoformat(),),
        ).fetchall()
        expired: list[DevelopmentLease] = []
        for row in rows:
            lease = _decode_model(DevelopmentLease, str(row["payload_json"]))
            recovered_lease = lease.model_copy(
                update={"status": LeaseStatus.expired, "released_at": now}
            )
            connection.execute(
                """
                UPDATE collaborative_development_leases
                SET status='expired',payload_json=?,released_at=? WHERE lease_id=?
                """,
                (
                    _persisted_json(recovered_lease),
                    now.isoformat(),
                    str(lease.lease_id),
                ),
            )
            item, _ = self._work_item_conn(connection, lease.work_item_id)
            if (
                item.status in {WorkItemStatus.leased, WorkItemStatus.working}
                and item.lease_revision == lease.fence
            ):
                assignment = self._assignment_conn(connection, item.assignment_id)
                autonomous = (
                    assignment.status == AssignmentStatus.active
                    and assignment.execution_mode == ExecutionMode.autonomous
                    and now < assignment.deadline
                )
                updated = item.model_copy(
                    update={
                        "status": WorkItemStatus.awaiting_dispatch,
                        "revision": item.revision + 1,
                        "updated_at": now,
                    }
                )
                self._update_work_item_conn(connection, updated, dispatch_authorized=autonomous)
                recovery_key = f"lease-expired:{lease.lease_id}"
                self._append_event_conn(
                    connection,
                    assignment_id=item.assignment_id,
                    event_type="work_item.lease_expired",
                    actor_role="platform",
                    actor_id="collaborative-development-recovery",
                    aggregate_type="work_item",
                    aggregate_id=item.work_item_id,
                    aggregate_revision=updated.revision,
                    idempotency_key=recovery_key,
                    payload={"lease_id": str(lease.lease_id), "fence": lease.fence},
                    created_at=now,
                )
                if autonomous:
                    self._ensure_work_dispatch_outbox_conn(
                        connection,
                        assignment_id=item.assignment_id,
                        work_item=updated,
                        extra_payload={
                            "expired_lease_id": str(lease.lease_id),
                        },
                        now=now,
                    )
            expired.append(recovered_lease)
        return expired

    async def read_events(
        self,
        assignment_id: UUID | str,
        *,
        after: int = 0,
        limit: int = 1_000,
    ) -> list[DevelopmentEvent]:
        self._require_initialized()
        if after < 0:
            raise ValueError("event cursor cannot be negative")
        if not 1 <= limit <= 5_000:
            raise ValueError("event limit must be between 1 and 5000")
        return await asyncio.to_thread(self._read_events_sync, assignment_id, after, limit)

    def _read_events_sync(
        self, assignment_id: UUID | str, after: int, limit: int
    ) -> list[DevelopmentEvent]:
        connection = self._connect()
        try:
            self._assignment_conn(connection, assignment_id)
            rows = connection.execute(
                """
                SELECT event_json FROM collaborative_development_events
                WHERE assignment_id=? AND seq>? ORDER BY seq LIMIT ?
                """,
                (str(assignment_id), after, limit),
            ).fetchall()
            return [_decode_model(DevelopmentEvent, str(row["event_json"])) for row in rows]
        finally:
            connection.close()

    async def ack_events(
        self,
        assignment_id: UUID | str,
        *,
        reader_role: str,
        reader_id: str,
        ack_seq: int,
        expected_cursor_revision: int,
        idempotency_key: IdempotencyKey,
    ) -> ReaderCursor:
        self._require_initialized()
        return await asyncio.to_thread(
            self._ack_events_sync,
            UUID(str(assignment_id)),
            reader_role,
            reader_id,
            ack_seq,
            expected_cursor_revision,
            str(idempotency_key),
        )

    def _ack_events_sync(
        self,
        assignment_id: UUID,
        reader_role: str,
        reader_id: str,
        ack_seq: int,
        expected_cursor_revision: int,
        idempotency_key: str,
    ) -> ReaderCursor:
        if ack_seq < 0 or expected_cursor_revision < 0:
            raise ValueError("cursor values cannot be negative")
        request_digest = _digest(
            {
                "ack_seq": ack_seq,
                "expected_cursor_revision": expected_cursor_revision,
            }
        )
        now = utc_now()
        with self._transaction() as connection:
            self._assignment_conn(connection, assignment_id)
            replay = self._receipt(
                connection,
                operation="ack_events",
                scope_id=assignment_id,
                actor_role=reader_role,
                actor_id=reader_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                model=ReaderCursor,
            )
            if replay is not None:
                return replay
            max_seq = int(
                connection.execute(
                    "SELECT next_seq-1 FROM collaborative_development_assignments "
                    "WHERE assignment_id=?",
                    (str(assignment_id),),
                ).fetchone()[0]
            )
            if ack_seq > max_seq:
                raise CollaborativeDevelopmentConflict(
                    "reader cannot acknowledge an event that does not exist"
                )
            row = connection.execute(
                """
                SELECT payload_json FROM collaborative_development_reader_cursors
                WHERE assignment_id=? AND reader_role=? AND reader_id=?
                """,
                (str(assignment_id), reader_role, reader_id),
            ).fetchone()
            if row is None:
                current_ack = 0
                current_revision = 0
            else:
                current = _decode_model(ReaderCursor, str(row["payload_json"]))
                current_ack = current.ack_seq
                current_revision = current.revision
            if current_revision != expected_cursor_revision:
                raise CollaborativeDevelopmentConflict(
                    "reader cursor revision compare-and-set failed"
                )
            if ack_seq < current_ack:
                raise CollaborativeDevelopmentConflict("reader cursor cannot move backwards")
            cursor = ReaderCursor(
                assignment_id=assignment_id,
                reader_role=reader_role,
                reader_id=reader_id,
                ack_seq=ack_seq,
                revision=current_revision + 1,
                updated_at=now,
            )
            connection.execute(
                """
                INSERT INTO collaborative_development_reader_cursors(
                  assignment_id,reader_role,reader_id,ack_seq,revision,
                  payload_json,updated_at
                ) VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(assignment_id,reader_role,reader_id) DO UPDATE SET
                  ack_seq=excluded.ack_seq,
                  revision=excluded.revision,
                  payload_json=excluded.payload_json,
                  updated_at=excluded.updated_at
                """,
                (
                    str(assignment_id),
                    reader_role,
                    reader_id,
                    cursor.ack_seq,
                    cursor.revision,
                    _persisted_json(cursor),
                    now.isoformat(),
                ),
            )
            self._save_receipt(
                connection,
                operation="ack_events",
                scope_id=assignment_id,
                actor_role=reader_role,
                actor_id=reader_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response=cursor,
                created_at=now,
            )
            return cursor

    async def get_reader_cursor(
        self,
        assignment_id: UUID | str,
        *,
        reader_role: str,
        reader_id: str,
    ) -> ReaderCursor | None:
        self._require_initialized()
        return await asyncio.to_thread(
            self._get_reader_cursor_sync, assignment_id, reader_role, reader_id
        )

    def _get_reader_cursor_sync(
        self, assignment_id: UUID | str, reader_role: str, reader_id: str
    ) -> ReaderCursor | None:
        connection = self._connect()
        try:
            self._assignment_conn(connection, assignment_id)
            row = connection.execute(
                """
                SELECT payload_json FROM collaborative_development_reader_cursors
                WHERE assignment_id=? AND reader_role=? AND reader_id=?
                """,
                (str(assignment_id), reader_role, reader_id),
            ).fetchone()
            return (
                _decode_model(ReaderCursor, str(row["payload_json"])) if row is not None else None
            )
        finally:
            connection.close()

    async def list_review_reconciliations(
        self,
        assignment_id: UUID | str,
    ) -> list[DispatchOutboxItem]:
        """List currently actionable unknown-outcome Lilies reviews."""

        self._require_initialized()
        return await asyncio.to_thread(
            self._list_review_reconciliations_sync,
            UUID(str(assignment_id)),
        )

    def _list_review_reconciliations_sync(
        self,
        assignment_id: UUID,
    ) -> list[DispatchOutboxItem]:
        connection = self._connect()
        try:
            self._assignment_conn(connection, assignment_id)
            rows = connection.execute(
                """
                SELECT outbox.item_json
                FROM collaborative_development_outbox AS outbox
                JOIN collaborative_development_work_items AS work
                  ON work.work_item_id=outbox.work_item_id
                WHERE outbox.assignment_id=?
                  AND outbox.kind='lilies_review'
                  AND outbox.destination_role='lilies'
                  AND outbox.status='failed'
                  AND outbox.last_error='reconciliation_required'
                  AND work.status='ready_for_lilies_review'
                  AND work.current_result_id=
                    json_extract(outbox.payload_json,'$.result_id')
                ORDER BY outbox.updated_at,outbox.outbox_id
                """,
                (str(assignment_id),),
            ).fetchall()
            return [_decode_model(DispatchOutboxItem, str(row["item_json"])) for row in rows]
        finally:
            connection.close()

    async def list_pending_outbox(
        self, *, now: datetime | None = None, limit: int = 1_000
    ) -> list[DispatchOutboxItem]:
        self._require_initialized()
        if not 1 <= limit <= 5_000:
            raise ValueError("outbox limit must be between 1 and 5000")
        return await asyncio.to_thread(self._list_pending_outbox_sync, _as_utc(now), limit)

    def _list_pending_outbox_sync(self, now: datetime, limit: int) -> list[DispatchOutboxItem]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT outbox.item_json
                FROM collaborative_development_outbox AS outbox
                LEFT JOIN collaborative_development_outbox_claims AS claim
                  ON claim.outbox_id=outbox.outbox_id AND claim.expires_at>?
                WHERE outbox.status='pending' AND outbox.available_at<=?
                  AND claim.outbox_id IS NULL
                ORDER BY outbox.available_at,outbox.created_at,outbox.outbox_id
                LIMIT ?
                """,
                (now.isoformat(), now.isoformat(), limit),
            ).fetchall()
            return [_decode_model(DispatchOutboxItem, str(row["item_json"])) for row in rows]
        finally:
            connection.close()

    async def claim_pending_outbox(
        self,
        *,
        claimed_by: str,
        now: datetime | None = None,
        claim_ttl_seconds: int = 900,
        limit: int = 1_000,
    ) -> list[DispatchOutboxClaim]:
        """Atomically fence outbox invocation across processes.

        The handler receives the outbox idempotency key, but no second
        dispatcher may invoke it while this durable claim is live.  An expired
        claim is recoverable after a crash; a stale claimant cannot finalize
        the record because completion is fenced by ``claim_id``.
        """

        self._require_initialized()
        if not claimed_by.strip():
            raise ValueError("outbox claimant id cannot be empty")
        if not 1 <= claim_ttl_seconds <= 86_400:
            raise ValueError("outbox claim TTL must be between 1 and 86400 seconds")
        if not 1 <= limit <= 5_000:
            raise ValueError("outbox limit must be between 1 and 5000")
        return await asyncio.to_thread(
            self._claim_pending_outbox_sync,
            claimed_by,
            _as_utc(now),
            claim_ttl_seconds,
            limit,
        )

    def _claim_pending_outbox_sync(
        self,
        claimed_by: str,
        now: datetime,
        claim_ttl_seconds: int,
        limit: int,
    ) -> list[DispatchOutboxClaim]:
        expires_at = now + timedelta(seconds=claim_ttl_seconds)
        claims: list[DispatchOutboxClaim] = []
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM collaborative_development_outbox_claims WHERE expires_at<=?",
                (now.isoformat(),),
            )
            rows = connection.execute(
                """
                SELECT outbox.item_json
                FROM collaborative_development_outbox AS outbox
                JOIN collaborative_development_assignments AS assignment
                  ON assignment.assignment_id=outbox.assignment_id
                JOIN collaborative_development_work_items AS work
                  ON work.work_item_id=outbox.work_item_id
                LEFT JOIN collaborative_development_outbox_claims AS claim
                  ON claim.outbox_id=outbox.outbox_id
                WHERE outbox.status='pending' AND outbox.available_at<=?
                  AND claim.outbox_id IS NULL
                  AND assignment.status='active' AND assignment.deadline>?
                  AND CAST(
                    json_extract(
                      assignment.payload_json,'$.budget.max_commands'
                    ) AS INTEGER
                  )>0
                  AND CAST(
                    json_extract(
                      assignment.payload_json,'$.budget.max_tool_calls'
                    ) AS INTEGER
                  )>0
                  AND CAST(
                    json_extract(
                      assignment.payload_json,'$.budget.max_cost_usd'
                    ) AS REAL
                  )>0
                  AND (
                    (
                      outbox.kind='work_dispatch'
                      AND work.status='awaiting_dispatch'
                      AND work.dispatch_authorized=1
                      AND NOT EXISTS (
                        SELECT 1
                        FROM json_each(
                          work.payload_json,'$.dependencies'
                        ) AS dependency
                        LEFT JOIN collaborative_development_work_items
                          AS dependency_work
                          ON dependency_work.work_item_id=dependency.value
                          AND dependency_work.assignment_id=work.assignment_id
                        WHERE dependency_work.work_item_id IS NULL
                           OR dependency_work.status NOT IN ('accepted','closed')
                      )
                    )
                    OR (
                      outbox.kind='lilies_review'
                      AND work.status='ready_for_lilies_review'
                    )
                  )
                ORDER BY outbox.available_at,outbox.created_at,outbox.outbox_id
                LIMIT ?
                """,
                (now.isoformat(), now.isoformat(), limit),
            ).fetchall()
            for row in rows:
                outbox = _decode_model(DispatchOutboxItem, str(row["item_json"]))
                claim = DispatchOutboxClaim(
                    claim_id=uuid4(),
                    outbox=outbox,
                    claimed_by=claimed_by,
                    claimed_at=now,
                    expires_at=expires_at,
                )
                connection.execute(
                    """
                    INSERT INTO collaborative_development_outbox_claims(
                      outbox_id,claim_id,claimed_by,claimed_at,expires_at
                    ) VALUES (?,?,?,?,?)
                    """,
                    (
                        str(outbox.outbox_id),
                        str(claim.claim_id),
                        claimed_by,
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                claims.append(claim)
        return claims

    @staticmethod
    def _require_outbox_claim_conn(
        connection: sqlite3.Connection,
        *,
        outbox_id: UUID,
        claim_id: UUID,
        now: datetime,
    ) -> None:
        row = connection.execute(
            """
            SELECT claim_id,expires_at
            FROM collaborative_development_outbox_claims
            WHERE outbox_id=?
            """,
            (str(outbox_id),),
        ).fetchone()
        if (
            row is None
            or str(row["claim_id"]) != str(claim_id)
            or datetime.fromisoformat(str(row["expires_at"])) <= now
        ):
            raise CollaborativeDevelopmentConflict(
                "outbox claim is missing, expired, or fenced by a newer dispatcher"
            )

    async def require_development_tool_metering(
        self,
        assignment_id: UUID | str,
    ) -> None:
        """Durably disable legacy self-reported command accounting."""

        self._require_initialized()
        await asyncio.to_thread(
            self._require_development_tool_metering_sync,
            UUID(str(assignment_id)),
            utc_now(),
        )

    def _require_development_tool_metering_sync(
        self,
        assignment_id: UUID,
        now: datetime,
    ) -> None:
        with self._transaction() as connection:
            self._assignment_conn(connection, assignment_id)
            connection.execute(
                """
                INSERT OR IGNORE INTO
                  collaborative_development_tool_metering_requirements(
                    assignment_id,required_at
                  ) VALUES (?,?)
                """,
                (str(assignment_id), now.isoformat()),
            )

    async def reserve_development_tool_usage(
        self,
        *,
        assignment_id: UUID | str,
        actor_role: AgentRole,
        usage_id: str,
        tool_name: str,
        request_digest: str,
        command_argv: tuple[str, ...] | None,
        command_cwd: str | None,
    ) -> bool:
        """Reserve one actual tool call, and optionally one actual command.

        The stable idempotency boundary is
        ``(assignment_id, actor_role, usage_id)``.  The same request returns
        ``False`` and must not execute again; reusing the key for another
        request is a conflict.  The budget check and insert share one
        ``BEGIN IMMEDIATE`` transaction, so restart and multi-process races
        cannot overrun ``max_tool_calls`` or ``max_commands``.
        """

        self._require_initialized()
        normalized_usage_id = usage_id.strip()
        if (
            not normalized_usage_id
            or len(normalized_usage_id) > 240
            or any(character in normalized_usage_id for character in "\x00\r\n")
        ):
            raise ValueError("usage id must be a bounded opaque identifier")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", request_digest) is None:
            raise ValueError("request digest must be a canonical SHA-256 digest")
        command_tools = {"process_run", "git_status", "git_diff"}
        known_tools = {
            "workspace_search",
            "workspace_read",
            "workspace_write",
            "workspace_patch",
            *command_tools,
        }
        if tool_name not in known_tools:
            raise ValueError("unknown collaborative-development tool")
        is_command = tool_name in command_tools
        if is_command != (command_argv is not None and command_cwd is not None):
            raise ValueError("process and Git tools require command argv and cwd metadata")
        if command_argv is not None and (
            not command_argv
            or any(
                not value or any(character in value for character in "\x00\r\n")
                for value in command_argv
            )
        ):
            raise ValueError("command argv must be non-empty and control-free")
        if command_cwd is not None:
            command_cwd = str(self._validated_usage_command_cwd(command_cwd))
        await self.require_development_tool_metering(assignment_id)
        return await asyncio.to_thread(
            self._reserve_development_tool_usage_sync,
            UUID(str(assignment_id)),
            actor_role,
            normalized_usage_id,
            tool_name,
            request_digest,
            command_argv,
            command_cwd,
            utc_now(),
        )

    @staticmethod
    def _validated_usage_command_cwd(value: str) -> PurePosixPath:
        candidate = PurePosixPath(value)
        if (
            not value
            or "\x00" in value
            or "\\" in value
            or candidate.is_absolute()
            or ".." in candidate.parts
            or candidate.as_posix() == ""
        ):
            raise ValueError("command cwd must be a normalized relative path")
        return candidate

    def _reserve_development_tool_usage_sync(
        self,
        assignment_id: UUID,
        actor_role: AgentRole,
        usage_id: str,
        tool_name: str,
        request_digest: str,
        command_argv: tuple[str, ...] | None,
        command_cwd: str | None,
        now: datetime,
    ) -> bool:
        command_count: Literal[0, 1] = 1 if command_argv is not None else 0
        with self._transaction() as connection:
            assignment = self._assignment_conn(connection, assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            if not any(grant.agent_role == actor_role for grant in assignment.agent_roles):
                raise CollaborativeDevelopmentAuthorizationError(
                    "tool usage role is not present in the assignment"
                )

            existing = connection.execute(
                """
                SELECT record_json
                FROM collaborative_development_tool_usage
                WHERE assignment_id=? AND actor_role=? AND usage_id=?
                """,
                (str(assignment_id), actor_role.value, usage_id),
            ).fetchone()
            if existing is not None:
                record = _decode_model(
                    DevelopmentToolUsageRecord,
                    str(existing["record_json"]),
                )
                if (
                    record.tool_name != tool_name
                    or record.request_digest != request_digest
                    or record.command_argv != command_argv
                    or record.command_cwd != command_cwd
                ):
                    raise CollaborativeDevelopmentConflict(
                        "usage id was replayed with a different tool request"
                    )
                return False

            totals = connection.execute(
                """
                SELECT
                  COALESCE(SUM(tool_calls),0) AS tool_calls,
                  COALESCE(SUM(commands),0) AS commands
                FROM collaborative_development_tool_usage
                WHERE assignment_id=?
                """,
                (str(assignment_id),),
            ).fetchone()
            used_tool_calls = int(totals["tool_calls"])
            used_commands = int(totals["commands"])
            if used_tool_calls + 1 > assignment.budget.max_tool_calls:
                raise CollaborativeDevelopmentBudgetExceeded(
                    "assignment tool-call budget is exhausted"
                )
            if used_commands + command_count > assignment.budget.max_commands:
                raise CollaborativeDevelopmentBudgetExceeded(
                    "assignment command budget is exhausted"
                )
            record = DevelopmentToolUsageRecord(
                reservation_id=uuid5(
                    NAMESPACE_URL,
                    (
                        "lilies:collaborative-development:tool-usage:"
                        f"{assignment_id}:{actor_role.value}:{usage_id}"
                    ),
                ),
                assignment_id=assignment_id,
                actor_role=actor_role,
                usage_id=usage_id,
                tool_name=tool_name,
                request_digest=request_digest,
                commands=command_count,
                command_argv=command_argv,
                command_cwd=command_cwd,
                reserved_at=now,
            )
            connection.execute(
                """
                INSERT INTO collaborative_development_tool_usage(
                  reservation_id,assignment_id,actor_role,usage_id,tool_name,
                  request_digest,tool_calls,commands,command_argv_json,
                  command_cwd,status,record_json,reserved_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(record.reservation_id),
                    str(record.assignment_id),
                    record.actor_role.value,
                    record.usage_id,
                    record.tool_name,
                    record.request_digest,
                    record.tool_calls,
                    record.commands,
                    (
                        _persisted_json(record.command_argv)
                        if record.command_argv is not None
                        else None
                    ),
                    record.command_cwd,
                    record.status,
                    _persisted_json(record),
                    record.reserved_at.isoformat(),
                ),
            )
            return True

    async def complete_development_tool_usage(
        self,
        *,
        assignment_id: UUID | str,
        actor_role: AgentRole,
        usage_id: str,
        request_digest: str,
        response_digest: str,
        output_digest: str | None,
    ) -> None:
        """Bind server-observed completion evidence to a prior reservation."""

        self._require_initialized()
        for label, digest in (
            ("request", request_digest),
            ("response", response_digest),
            ("output", output_digest),
        ):
            if (
                digest is not None
                and re.fullmatch(
                    r"sha256:[0-9a-f]{64}",
                    digest,
                )
                is None
            ):
                raise ValueError(f"{label} digest must be canonical SHA-256")
        await asyncio.to_thread(
            self._complete_development_tool_usage_sync,
            UUID(str(assignment_id)),
            actor_role,
            usage_id.strip(),
            request_digest,
            response_digest,
            output_digest,
            utc_now(),
        )

    def _complete_development_tool_usage_sync(
        self,
        assignment_id: UUID,
        actor_role: AgentRole,
        usage_id: str,
        request_digest: str,
        response_digest: str,
        output_digest: str | None,
        now: datetime,
    ) -> None:
        with self._transaction() as connection:
            self._assignment_conn(connection, assignment_id)
            row = connection.execute(
                """
                SELECT record_json
                FROM collaborative_development_tool_usage
                WHERE assignment_id=? AND actor_role=? AND usage_id=?
                """,
                (str(assignment_id), actor_role.value, usage_id),
            ).fetchone()
            if row is None:
                raise CollaborativeDevelopmentNotFound("tool usage reservation does not exist")
            record = _decode_model(
                DevelopmentToolUsageRecord,
                str(row["record_json"]),
            )
            if record.request_digest != request_digest:
                raise CollaborativeDevelopmentConflict(
                    "tool completion request digest differs from its reservation"
                )
            if record.status == "completed":
                if (
                    record.response_digest != response_digest
                    or record.output_digest != output_digest
                ):
                    raise CollaborativeDevelopmentConflict(
                        "tool usage completion was replayed with different evidence"
                    )
                return
            completed = record.model_copy(
                update={
                    "status": "completed",
                    "response_digest": response_digest,
                    "output_digest": output_digest,
                    "completed_at": now,
                }
            )
            connection.execute(
                """
                UPDATE collaborative_development_tool_usage
                SET status='completed',response_digest=?,output_digest=?,
                    record_json=?,completed_at=?
                WHERE reservation_id=?
                """,
                (
                    response_digest,
                    output_digest,
                    _persisted_json(completed),
                    now.isoformat(),
                    str(completed.reservation_id),
                ),
            )

    async def list_development_tool_usage(
        self,
        assignment_id: UUID | str,
    ) -> list[DevelopmentToolUsageRecord]:
        self._require_initialized()
        return await asyncio.to_thread(
            self._list_development_tool_usage_sync,
            UUID(str(assignment_id)),
        )

    def _list_development_tool_usage_sync(
        self,
        assignment_id: UUID,
    ) -> list[DevelopmentToolUsageRecord]:
        connection = self._connect()
        try:
            self._assignment_conn(connection, assignment_id)
            rows = connection.execute(
                """
                SELECT record_json
                FROM collaborative_development_tool_usage
                WHERE assignment_id=?
                ORDER BY reserved_at,reservation_id
                """,
                (str(assignment_id),),
            ).fetchall()
            return [
                _decode_model(
                    DevelopmentToolUsageRecord,
                    str(row["record_json"]),
                )
                for row in rows
            ]
        finally:
            connection.close()

    async def reserve_trusted_provider_cost(
        self,
        authorization: TrustedProviderCostAuthorization,
    ) -> bool:
        """Atomically authorize a paid provider request before invocation.

        Only a control-plane-verified worst-case amount enters this path.  All
        unsettled reservations continue to consume their full upper bound
        across process restart; a later trusted receipt replaces that amount
        with actual cost and releases the difference.
        """

        self._require_initialized()
        verifier = self._trusted_provider_cost_authorizer
        if verifier is None or not verifier(authorization):
            raise CollaborativeDevelopmentAuthorizationError(
                "provider cost authorization is not trusted by the control plane"
            )
        return await asyncio.to_thread(
            self._reserve_trusted_provider_cost_sync,
            authorization,
            utc_now(),
        )

    def _reserve_trusted_provider_cost_sync(
        self,
        authorization: TrustedProviderCostAuthorization,
        now: datetime,
    ) -> bool:
        request_digest = _digest(authorization)
        worst_case_units = _usd_reservation_units(authorization.worst_case_cost_usd)
        with self._transaction() as connection:
            assignment = self._assignment_conn(
                connection,
                authorization.assignment_id,
            )
            self._ensure_assignment_writable(assignment, now=now)
            existing = connection.execute(
                """
                SELECT request_digest
                FROM collaborative_development_provider_cost_reservations
                WHERE reservation_id=?
                """,
                (str(authorization.reservation_id),),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != request_digest:
                    raise CollaborativeDevelopmentConflict(
                        "provider reservation id was replayed with another payload"
                    )
                return False
            provider_request = connection.execute(
                """
                SELECT request_digest
                FROM collaborative_development_provider_cost_reservations
                WHERE provider=? AND provider_request_id=?
                """,
                (
                    authorization.provider,
                    authorization.provider_request_id,
                ),
            ).fetchone()
            if provider_request is not None:
                raise CollaborativeDevelopmentConflict(
                    "provider request already has another cost reservation"
                )
            committed_units = int(
                connection.execute(
                    """
                    SELECT COALESCE(SUM(
                      CASE
                        WHEN status='settled' THEN settled_cost_units
                        ELSE worst_case_cost_units
                      END
                    ),0)
                    FROM collaborative_development_provider_cost_reservations
                    WHERE assignment_id=?
                    """,
                    (str(authorization.assignment_id),),
                ).fetchone()[0]
            )
            budget_units = _usd_budget_units(assignment.budget.max_cost_usd)
            if committed_units + worst_case_units > budget_units:
                raise CollaborativeDevelopmentBudgetExceeded(
                    "trusted provider worst-case reservation exceeds the assignment cost budget"
                )
            reservation = TrustedProviderCostReservation(
                cost_cap=authorization,
                reserved_at=now,
            )
            connection.execute(
                """
                INSERT INTO collaborative_development_provider_cost_reservations(
                  reservation_id,assignment_id,provider,provider_request_id,
                  request_digest,worst_case_cost_units,status,record_json,
                  reserved_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    str(authorization.reservation_id),
                    str(authorization.assignment_id),
                    authorization.provider,
                    authorization.provider_request_id,
                    request_digest,
                    worst_case_units,
                    reservation.status,
                    _persisted_json(reservation),
                    now.isoformat(),
                ),
            )
            return True

    async def record_trusted_provider_cost(
        self,
        receipt: TrustedProviderCostReceipt,
    ) -> bool:
        """Settle a prior authorization with a verified provider receipt."""

        self._require_initialized()
        verifier = self._trusted_provider_receipt_verifier
        if verifier is None or not verifier(receipt):
            raise CollaborativeDevelopmentAuthorizationError(
                "provider cost receipt is not trusted by the control plane"
            )
        return await asyncio.to_thread(
            self._record_trusted_provider_cost_sync,
            receipt,
            utc_now(),
        )

    def _record_trusted_provider_cost_sync(
        self,
        receipt: TrustedProviderCostReceipt,
        now: datetime,
    ) -> bool:
        request_digest = _digest(receipt)
        settled_units = _usd_reservation_units(receipt.cost_usd)
        with self._transaction() as connection:
            self._assignment_conn(connection, receipt.assignment_id)
            existing = connection.execute(
                """
                SELECT request_digest
                FROM collaborative_development_provider_costs
                WHERE receipt_id=?
                """,
                (receipt.receipt_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"]) != request_digest:
                    raise CollaborativeDevelopmentConflict(
                        "provider receipt id was replayed with another payload"
                    )
                return False
            row = connection.execute(
                """
                SELECT record_json,worst_case_cost_units,status
                FROM collaborative_development_provider_cost_reservations
                WHERE reservation_id=? AND assignment_id=?
                """,
                (
                    str(receipt.reservation_id),
                    str(receipt.assignment_id),
                ),
            ).fetchone()
            if row is None:
                raise CollaborativeDevelopmentConflict(
                    "provider receipt has no prior trusted cost reservation"
                )
            reservation = _decode_model(
                TrustedProviderCostReservation,
                str(row["record_json"]),
            )
            authorization = reservation.cost_cap
            if (
                receipt.provider != authorization.provider
                or receipt.provider_request_id != authorization.provider_request_id
                or receipt.model != authorization.model
            ):
                raise CollaborativeDevelopmentConflict(
                    "provider receipt differs from its frozen authorization"
                )
            if str(row["status"]) != "reserved":
                raise CollaborativeDevelopmentConflict(
                    "provider cost reservation was already settled"
                )
            if settled_units > int(row["worst_case_cost_units"]):
                raise CollaborativeDevelopmentBudgetExceeded(
                    "trusted provider receipt exceeds its worst-case reservation"
                )
            provider_request = connection.execute(
                """
                SELECT request_digest
                FROM collaborative_development_provider_costs
                WHERE provider=? AND provider_request_id=?
                """,
                (receipt.provider, receipt.provider_request_id),
            ).fetchone()
            if provider_request is not None:
                raise CollaborativeDevelopmentConflict(
                    "provider request already has another cost receipt"
                )
            settled = TrustedProviderCostReservation(
                cost_cap=authorization,
                status="settled",
                receipt=receipt,
                reserved_at=reservation.reserved_at,
                settled_at=now,
            )
            connection.execute(
                """
                INSERT INTO collaborative_development_provider_costs(
                  receipt_id,reservation_id,assignment_id,provider,
                  provider_request_id,request_digest,cost_usd,payload_json,
                  issued_at,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    receipt.receipt_id,
                    str(receipt.reservation_id),
                    str(receipt.assignment_id),
                    receipt.provider,
                    receipt.provider_request_id,
                    request_digest,
                    receipt.cost_usd,
                    _persisted_json(receipt),
                    receipt.issued_at.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE collaborative_development_provider_cost_reservations
                SET settled_cost_units=?,status='settled',record_json=?,
                    settled_at=?
                WHERE reservation_id=? AND status='reserved'
                """,
                (
                    settled_units,
                    _persisted_json(settled),
                    now.isoformat(),
                    str(receipt.reservation_id),
                ),
            )
            return True

    async def list_trusted_provider_cost_reservations(
        self,
        assignment_id: UUID | str,
    ) -> list[TrustedProviderCostReservation]:
        self._require_initialized()
        return await asyncio.to_thread(
            self._list_trusted_provider_cost_reservations_sync,
            UUID(str(assignment_id)),
        )

    def _list_trusted_provider_cost_reservations_sync(
        self,
        assignment_id: UUID,
    ) -> list[TrustedProviderCostReservation]:
        connection = self._connect()
        try:
            self._assignment_conn(connection, assignment_id)
            rows = connection.execute(
                """
                SELECT record_json
                FROM collaborative_development_provider_cost_reservations
                WHERE assignment_id=?
                ORDER BY reserved_at,reservation_id
                """,
                (str(assignment_id),),
            ).fetchall()
            return [
                _decode_model(
                    TrustedProviderCostReservation,
                    str(row["record_json"]),
                )
                for row in rows
            ]
        finally:
            connection.close()

    async def acquire_dispatch_invocation_fence(
        self,
        *,
        assignment_id: UUID | str,
        outbox_id: UUID | str,
        attempt: int,
        claim_id: UUID | str,
        now: datetime | None = None,
    ) -> DevelopmentInvocationFenceResult:
        """Atomically fence one outbox attempt before entering a role handler.

        A replay returns the original fence with ``acquired=False`` and cannot
        enter the handler again.  This operation consumes no tool, command, or
        provider-cost budget; those are recorded only where the resource is
        actually used.
        """

        self._require_initialized()
        if attempt < 1:
            raise ValueError("dispatch attempt must be at least one")
        return await asyncio.to_thread(
            self._acquire_dispatch_invocation_fence_sync,
            UUID(str(assignment_id)),
            UUID(str(outbox_id)),
            attempt,
            UUID(str(claim_id)),
            _as_utc(now),
        )

    def _acquire_dispatch_invocation_fence_sync(
        self,
        assignment_id: UUID,
        outbox_id: UUID,
        attempt: int,
        claim_id: UUID,
        now: datetime,
    ) -> DevelopmentInvocationFenceResult:
        with self._transaction() as connection:
            assignment = self._assignment_conn(connection, assignment_id)
            self._ensure_assignment_writable(assignment, now=now)
            self._require_outbox_claim_conn(
                connection,
                outbox_id=outbox_id,
                claim_id=claim_id,
                now=now,
            )
            row = connection.execute(
                """
                SELECT item_json
                FROM collaborative_development_outbox
                WHERE outbox_id=? AND assignment_id=?
                """,
                (str(outbox_id), str(assignment_id)),
            ).fetchone()
            if row is None:
                raise CollaborativeDevelopmentNotFound(
                    "dispatch outbox does not belong to the assignment"
                )
            outbox = _decode_model(DispatchOutboxItem, str(row["item_json"]))
            if outbox.status != OutboxStatus.pending:
                raise CollaborativeDevelopmentInvalidState(
                    "only a pending outbox can acquire an invocation fence"
                )
            if attempt != outbox.attempts + 1:
                raise CollaborativeDevelopmentConflict(
                    "dispatch fence attempt does not match the durable outbox attempt"
                )

            existing = connection.execute(
                """
                SELECT payload_json
                FROM collaborative_development_invocation_fences
                WHERE outbox_id=? AND attempt=?
                """,
                (str(outbox_id), attempt),
            ).fetchone()
            if existing is not None:
                fence = _decode_model(
                    DevelopmentInvocationFence,
                    str(existing["payload_json"]),
                )
                return DevelopmentInvocationFenceResult(
                    fence=fence,
                    acquired=False,
                )

            fence = DevelopmentInvocationFence(
                fence_id=uuid5(
                    NAMESPACE_URL,
                    (f"lilies:collaborative-development:invocation-fence:{outbox_id}:{attempt}"),
                ),
                assignment_id=assignment_id,
                outbox_id=outbox_id,
                attempt=attempt,
                claim_id=claim_id,
                destination_role=outbox.destination_role,
                created_at=now,
            )
            connection.execute(
                """
                INSERT INTO collaborative_development_invocation_fences(
                  fence_id,assignment_id,outbox_id,attempt,claim_id,
                  destination_role,payload_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    str(fence.fence_id),
                    str(fence.assignment_id),
                    str(fence.outbox_id),
                    fence.attempt,
                    str(fence.claim_id),
                    fence.destination_role.value,
                    _persisted_json(fence),
                    fence.created_at.isoformat(),
                ),
            )
            return DevelopmentInvocationFenceResult(
                fence=fence,
                acquired=True,
            )

    async def list_invocation_fences(
        self,
        assignment_id: UUID | str,
    ) -> list[DevelopmentInvocationFence]:
        """Read the server-owned, budget-neutral invocation fences."""

        self._require_initialized()
        return await asyncio.to_thread(
            self._list_invocation_fences_sync,
            UUID(str(assignment_id)),
        )

    def _list_invocation_fences_sync(
        self,
        assignment_id: UUID,
    ) -> list[DevelopmentInvocationFence]:
        connection = self._connect()
        try:
            self._assignment_conn(connection, assignment_id)
            rows = connection.execute(
                """
                SELECT payload_json
                FROM collaborative_development_invocation_fences
                WHERE assignment_id=?
                ORDER BY created_at,fence_id
                """,
                (str(assignment_id),),
            ).fetchall()
            return [
                _decode_model(DevelopmentInvocationFence, str(row["payload_json"])) for row in rows
            ]
        finally:
            connection.close()

    async def renew_outbox_claim(
        self,
        outbox_id: UUID | str,
        *,
        claim_id: UUID | str,
        extend_seconds: int,
        now: datetime | None = None,
    ) -> datetime:
        self._require_initialized()
        if not 1 <= extend_seconds <= 86_400:
            raise ValueError("outbox claim extension must be between 1 and 86400 seconds")
        return await asyncio.to_thread(
            self._renew_outbox_claim_sync,
            UUID(str(outbox_id)),
            UUID(str(claim_id)),
            extend_seconds,
            _as_utc(now),
        )

    def _renew_outbox_claim_sync(
        self,
        outbox_id: UUID,
        claim_id: UUID,
        extend_seconds: int,
        now: datetime,
    ) -> datetime:
        expires_at = now + timedelta(seconds=extend_seconds)
        with self._transaction() as connection:
            self._require_outbox_claim_conn(
                connection,
                outbox_id=outbox_id,
                claim_id=claim_id,
                now=now,
            )
            changed = connection.execute(
                """
                UPDATE collaborative_development_outbox_claims
                SET expires_at=?
                WHERE outbox_id=? AND claim_id=?
                """,
                (expires_at.isoformat(), str(outbox_id), str(claim_id)),
            ).rowcount
            if changed != 1:
                raise CollaborativeDevelopmentConflict(
                    "outbox claim renewal compare-and-set failed"
                )
        return expires_at

    async def mark_outbox_delivered(
        self,
        outbox_id: UUID | str,
        *,
        claim_id: UUID | str,
        delivered_at: datetime | None = None,
    ) -> DispatchOutboxItem:
        self._require_initialized()
        return await asyncio.to_thread(
            self._mark_outbox_delivered_sync,
            UUID(str(outbox_id)),
            UUID(str(claim_id)),
            _as_utc(delivered_at),
        )

    def _mark_outbox_delivered_sync(
        self, outbox_id: UUID, claim_id: UUID, delivered_at: datetime
    ) -> DispatchOutboxItem:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT item_json FROM collaborative_development_outbox WHERE outbox_id=?",
                (str(outbox_id),),
            ).fetchone()
            if row is None:
                raise CollaborativeDevelopmentNotFound("outbox item not found")
            item = _decode_model(DispatchOutboxItem, str(row["item_json"]))
            self._require_outbox_claim_conn(
                connection,
                outbox_id=outbox_id,
                claim_id=claim_id,
                now=delivered_at,
            )
            if item.status != OutboxStatus.pending:
                raise CollaborativeDevelopmentInvalidState(
                    f"cannot deliver {item.status.value} outbox item"
                )
            updated = item.model_copy(
                update={
                    "status": OutboxStatus.delivered,
                    "attempts": item.attempts + 1,
                    "updated_at": delivered_at,
                    "delivered_at": delivered_at,
                    "last_error": None,
                }
            )
            connection.execute(
                """
                UPDATE collaborative_development_outbox
                SET status='delivered',attempts=?,item_json=?,updated_at=?,
                    delivered_at=?,last_error=NULL WHERE outbox_id=?
                """,
                (
                    updated.attempts,
                    _persisted_json(updated),
                    delivered_at.isoformat(),
                    delivered_at.isoformat(),
                    str(outbox_id),
                ),
            )
            connection.execute(
                "DELETE FROM collaborative_development_outbox_claims "
                "WHERE outbox_id=? AND claim_id=?",
                (str(outbox_id), str(claim_id)),
            )
            return updated

    async def mark_outbox_failed(
        self,
        outbox_id: UUID | str,
        *,
        claim_id: UUID | str,
        error: str,
        retry_at: datetime | None,
    ) -> DispatchOutboxItem:
        self._require_initialized()
        return await asyncio.to_thread(
            self._mark_outbox_failed_sync,
            UUID(str(outbox_id)),
            UUID(str(claim_id)),
            error,
            _as_utc(retry_at) if retry_at is not None else None,
        )

    def _mark_outbox_failed_sync(
        self,
        outbox_id: UUID,
        claim_id: UUID,
        error: str,
        retry_at: datetime | None,
    ) -> DispatchOutboxItem:
        if not error.strip():
            raise ValueError("outbox failure requires an error")
        persisted_error = _persisted_text(error)
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT item_json FROM collaborative_development_outbox WHERE outbox_id=?",
                (str(outbox_id),),
            ).fetchone()
            if row is None:
                raise CollaborativeDevelopmentNotFound("outbox item not found")
            item = _decode_model(DispatchOutboxItem, str(row["item_json"]))
            if item.status != OutboxStatus.pending:
                raise CollaborativeDevelopmentInvalidState("only pending outbox items can fail")
            self._require_outbox_claim_conn(
                connection,
                outbox_id=outbox_id,
                claim_id=claim_id,
                now=now,
            )
            status = OutboxStatus.pending if retry_at is not None else OutboxStatus.failed
            updated = item.model_copy(
                update={
                    "status": status,
                    "attempts": item.attempts + 1,
                    "available_at": retry_at or item.available_at,
                    "updated_at": now,
                    "last_error": persisted_error,
                }
            )
            connection.execute(
                """
                UPDATE collaborative_development_outbox
                SET status=?,attempts=?,available_at=?,item_json=?,updated_at=?,
                    last_error=? WHERE outbox_id=?
                """,
                (
                    status.value,
                    updated.attempts,
                    updated.available_at.isoformat(),
                    _persisted_json(updated),
                    now.isoformat(),
                    persisted_error,
                    str(outbox_id),
                ),
            )
            connection.execute(
                "DELETE FROM collaborative_development_outbox_claims "
                "WHERE outbox_id=? AND claim_id=?",
                (str(outbox_id), str(claim_id)),
            )
            return updated

    async def journal_mode(self) -> str:
        """Return the actual SQLite journal mode for qualification evidence."""

        self._require_initialized()
        return await asyncio.to_thread(self._journal_mode_sync)

    def _journal_mode_sync(self) -> str:
        connection = self._connect()
        try:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0])
        finally:
            connection.close()


__all__ = [
    "CollaborativeDevelopmentAuthorizationError",
    "CollaborativeDevelopmentBudgetExceeded",
    "CollaborativeDevelopmentConflict",
    "CollaborativeDevelopmentInvalidState",
    "CollaborativeDevelopmentNotFound",
    "CollaborativeDevelopmentStorageError",
    "CollaborativeDevelopmentStore",
    "DevelopmentToolUsageRecord",
    "SCHEMA_VERSION",
    "TrustedProviderCostAuthorization",
    "TrustedProviderCostReceipt",
    "TrustedProviderCostReservation",
]
