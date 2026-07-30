from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from .collaboration_models import (
    ApprovalDecision,
    CollaborationChannel,
    CollaborationMessageEnvelope,
    CollaborationReport,
    DeveloperLease,
    DeveloperResponse,
    EnvironmentResponse,
    LiliesReprobeResult,
    ReaderCursor,
    TaskPackageAmendment,
    VerificationClaim,
    VerificationResult,
    sanitize_collaboration_payload,
    validate_collaboration_payload_safety,
)


COLLABORATION_SCHEMA_VERSION = 2
_WRITABLE_CHANNEL_STATUSES = frozenset({"active", "disconnected"})
_CLOSED_CHANNEL_STATUSES = frozenset({"closing", "closed", "archived"})
_DEVELOPER_VISIBLE_REPORT_STATUSES = frozenset(
    {
        "approved_for_codex",
        "implementing",
        "ready_for_lilies_verification",
        "verification_failed",
        "routed_to_task_author",
        "task_package_amended",
        "environment_failed",
        "environment_restored",
        "unresolved",
    }
)
_DIRECT_DEVELOPER_ROUTES = frozenset({"task_author", "environment", "verifier"})
_CAPABILITY_REPORT_CATEGORIES = frozenset({"platform_capability_gap", "platform_defect_suspected"})
_CLAIM_RESOLVED_REPORT_STATUSES = {
    "platform_capability_gap": frozenset(
        {"lilies_verified", "independently_verified", "rejected", "withdrawn"}
    ),
    "platform_defect_suspected": frozenset(
        {"lilies_verified", "independently_verified", "rejected", "withdrawn"}
    ),
    "task_spec_gap": frozenset({"lilies_rechecks", "independently_verified"}),
    "environment_gap": frozenset({"lilies_health_checks", "independently_verified"}),
}
_ACTIVE_CLAIM_STATUSES = frozenset(
    {"frozen", "ready_for_independent_verification", "awaiting_independent_verification"}
)
_REPORT_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "report_id",
        "category",
        "phase",
        "severity",
        "summary",
        "original_goal",
        "requirement_digest",
        "platform_contract_digest",
        "manuals_checked",
        "attempted_routes",
        "expected",
        "actual",
        "reproduction",
        "missing_contract",
        "blocking_scope",
        "independent_work",
        "workaround_considered",
        "workaround_loss",
        "requested_outcome",
        "confidence",
        "secret_redactions",
        "evidence_refs",
    }
)
_DOMAIN_TRANSPORT_FIELDS = frozenset(
    {
        "expected_claim_revision",
        "expected_report_revision",
        "idempotency_key",
        "lease_id",
        "lease_owner_id",
        "message",
        "next_claim_status",
        "next_report_route",
        "next_report_status",
        "next_visibility",
        "owner_id",
        "payload",
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | str | None, *, default: datetime | None = None) -> datetime:
    if value is None:
        if default is None:
            raise ValueError("a timezone-aware timestamp is required")
        value = default
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | str | None, *, default: datetime | None = None) -> str | None:
    if value is None and default is None:
        return None
    return _as_utc(value, default=default).isoformat()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _record(value: Mapping[str, Any] | Any) -> dict[str, Any]:
    normalized = _jsonable(value)
    if not isinstance(normalized, dict):
        raise TypeError("collaboration records must be mappings or Pydantic models")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode()).hexdigest()}"


def _idempotency_digest(value: Any) -> str:
    return _digest(value)


def _validated_request_digest(value: Any, *, fallback: str | None = None) -> str:
    if value is None:
        if fallback is None:
            raise ValueError("client request digest is required")
        return fallback
    digest = str(value)
    raw = digest.removeprefix("sha256:")
    if not digest.startswith("sha256:") or len(raw) != 64:
        raise ValueError("client request digest must be a sha256 digest")
    try:
        bytes.fromhex(raw)
    except ValueError as error:
        raise ValueError("client request digest must be a sha256 digest") from error
    return digest


def _canonical_content_hash(value: Any) -> str:
    digest = str(value)
    raw = digest.removeprefix("sha256:")
    if len(raw) == 64:
        try:
            bytes.fromhex(raw)
        except ValueError:
            pass
        else:
            return f"sha256:{raw.lower()}"
    return digest


def _report_evidence_digest(payload: Mapping[str, Any]) -> str:
    """Digest only evidence-bearing report fields.

    Platform-authored validation and approval revisions never call the Lilies
    evidence-revision path, so they cannot consume this budget.  Changes to
    prose such as ``summary`` also cannot disguise an unchanged evidence set.
    """

    evidence_fields = (
        "manuals_checked",
        "attempted_routes",
        "expected",
        "actual",
        "reproduction",
        "missing_contract",
        "independent_work",
        "evidence_refs",
    )
    return _digest({name: payload.get(name) for name in evidence_fields})


def _bearer_selector(bearer: str) -> str:
    if not isinstance(bearer, str) or len(bearer) < 16:
        raise ValueError("collaboration bearer must contain at least 16 characters")
    return f"sha256:{hashlib.sha256(bearer.encode()).hexdigest()}"


def _encode_bearer(bearer: str) -> str:
    _bearer_selector(bearer)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        bearer.encode(),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=32,
    )
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def _verify_bearer(bearer: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        derived = hashlib.scrypt(
            bearer.encode(),
            salt=bytes.fromhex(raw_salt),
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(bytes.fromhex(expected)),
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(derived.hex(), expected)


def _required(record: Mapping[str, Any], name: str) -> Any:
    value = record.get(name)
    if value is None or value == "":
        raise ValueError(f"{name} is required")
    return value


def _normalized_application_ids(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("application_ids must be an ordered list")
    application_ids = [str(UUID(str(item))) for item in value]
    if not application_ids:
        raise ValueError("application_ids cannot be empty")
    if len(application_ids) != len(set(application_ids)):
        raise ValueError("application_ids must be unique")
    return application_ids


def _redact_registered_secrets(
    value: Any,
    *,
    field_names: frozenset[str],
    secret_values: tuple[str, ...],
) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = str(key).strip().casefold().replace("-", "_")
            redacted[str(key)] = (
                "[REDACTED]"
                if normalized_key in field_names
                else _redact_registered_secrets(
                    child,
                    field_names=field_names,
                    secret_values=secret_values,
                )
            )
        return redacted
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            _redact_registered_secrets(
                item,
                field_names=field_names,
                secret_values=secret_values,
            )
            for item in value
        ]
    if isinstance(value, str):
        redacted_value = value
        for secret in secret_values:
            redacted_value = redacted_value.replace(secret, "[REDACTED]")
        return redacted_value
    return value


def _validated_projection(model: Any, value: Mapping[str, Any]) -> dict[str, Any]:
    return model.model_validate(dict(value)).model_dump(mode="json")


class CollaborationStorageError(RuntimeError):
    code = "collaboration_storage_error"
    status_code = 500


class CollaborationNotFound(CollaborationStorageError):
    code = "collaboration_not_found"
    status_code = 404


class CollaborationConflict(CollaborationStorageError):
    code = "collaboration_conflict"
    status_code = 409


class CollaborationChannelClosed(CollaborationConflict):
    code = "collaboration_channel_closed"
    status_code = 410


class CollaborationCredentialAlreadyIssued(CollaborationConflict):
    code = "credential_already_issued"


class CollaborationUnauthorized(CollaborationStorageError):
    code = "collaboration_unauthorized"
    status_code = 403


class CollaborationStore:
    """Durable collaboration facts, isolated from ordinary agent events and logs."""

    def __init__(
        self,
        db_path: Path,
        *,
        registered_secret_fields: Sequence[str] | None = None,
        registered_secret_values: Sequence[str] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._lock = asyncio.Lock()
        self._secret_registry_lock = threading.RLock()
        self._registered_secret_fields = frozenset(
            str(name).strip().casefold().replace("-", "_")
            for name in (registered_secret_fields or ())
            if str(name).strip()
        )
        self._registered_secret_values = tuple(
            sorted(
                {str(value) for value in (registered_secret_values or ()) if str(value)},
                key=len,
                reverse=True,
            )
        )

    def register_secret_value(self, value: str) -> None:
        """Add a runtime-issued credential to the exact-value redaction oracle.

        Collaboration bearers are generated after service construction and are
        intentionally not recoverable from their stored verifier.  Registration
        therefore happens both at issuance and after successful authentication.
        """

        secret = str(value)
        if not secret:
            return
        with self._secret_registry_lock:
            if secret in self._registered_secret_values:
                return
            self._registered_secret_values = tuple(
                sorted(
                    {*self._registered_secret_values, secret},
                    key=len,
                    reverse=True,
                )
            )

    def _redact_payload(self, value: Any) -> Any:
        normalized = _jsonable(value)
        with self._secret_registry_lock:
            secret_values = self._registered_secret_values
        return _redact_registered_secrets(
            normalized,
            field_names=self._registered_secret_fields,
            secret_values=secret_values,
        )

    def _reject_registered_secret_identifiers(self, value: Any) -> None:
        """Reject credentials smuggled into durable identity/index columns.

        Free-form payloads follow the report redaction contract.  Identifier
        columns cannot be rewritten without changing idempotency or ownership,
        so they fail closed instead.
        """

        with self._secret_registry_lock:
            secrets = self._registered_secret_values

        def inspect(candidate: Any) -> None:
            if isinstance(candidate, Mapping):
                for child in candidate.values():
                    inspect(child)
                return
            if isinstance(candidate, (list, tuple, set, frozenset)):
                for child in candidate:
                    inspect(child)
                return
            if not isinstance(candidate, str):
                return
            try:
                sanitized_candidate = sanitize_collaboration_payload(candidate)
                validate_collaboration_payload_safety(candidate)
            except ValueError as error:
                raise CollaborationConflict(
                    "collaboration identifier contains forbidden sensitive material"
                ) from error
            if sanitized_candidate != candidate:
                raise CollaborationConflict(
                    "collaboration identifier contains credential-shaped material"
                )
            for secret in secrets:
                if len(candidate) < len(secret):
                    continue
                for offset in range(len(candidate) - len(secret) + 1):
                    if hmac.compare_digest(candidate[offset : offset + len(secret)], secret):
                        raise CollaborationConflict(
                            "collaboration identifier contains a registered secret"
                        )

        inspect(value)

    def _safe_payload(self, value: Any) -> Any:
        sanitized = sanitize_collaboration_payload(self._redact_payload(value))
        return validate_collaboration_payload_safety(sanitized)

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.db_path.parent, 0o700)
        except OSError:
            pass
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        if self.db_path.exists():
            os.chmod(self.db_path, 0o600)
        return connection

    def _secure_database_files(self) -> None:
        for path in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
        ):
            if path.exists():
                os.chmod(path, 0o600)

    async def initialize(self) -> dict[str, int]:
        async with self._lock:
            result = await asyncio.to_thread(self._initialize_sync)
        self._secure_database_files()
        return result

    def _initialize_sync(self) -> dict[str, int]:
        applied_at = _now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS collaboration_schema (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            current = int(
                connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS version FROM collaboration_schema"
                ).fetchone()["version"]
            )
            if current > COLLABORATION_SCHEMA_VERSION:
                raise CollaborationStorageError(
                    f"collaboration schema {current} is newer than supported "
                    f"{COLLABORATION_SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collaboration_channels (
                  channel_id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
                  assignment_id TEXT NOT NULL UNIQUE,
                  lilies_session_id TEXT NOT NULL UNIQUE,
                  application_ids_json TEXT NOT NULL,
                  approval_mode TEXT NOT NULL CHECK(approval_mode IN ('manual','auto_forward')),
                  max_report_evidence_rounds INTEGER
                    CHECK(max_report_evidence_rounds BETWEEN 1 AND 100),
                  status TEXT NOT NULL CHECK(status IN
                    ('created','active','disconnected','closing','closed','archived')),
                  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                  next_seq INTEGER NOT NULL DEFAULT 1 CHECK(next_seq >= 1),
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  closed_at TEXT,
                  retention_until TEXT
                );

                CREATE TABLE IF NOT EXISTS collaboration_credentials (
                  credential_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  assignment_id TEXT NOT NULL,
                  lilies_session_id TEXT NOT NULL,
                  bearer_selector TEXT NOT NULL UNIQUE,
                  bearer_verifier TEXT NOT NULL,
                  role TEXT NOT NULL,
                  scopes_json TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  revoked_at TEXT,
                  revocation_reason TEXT,
                  UNIQUE(channel_id, role, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_channel_operations (
                  channel_id TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  resulting_revision INTEGER NOT NULL,
                  message_id TEXT,
                  audit_id TEXT,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(channel_id, operation, actor_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id),
                  FOREIGN KEY(audit_id) REFERENCES collaboration_audit(audit_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_messages (
                  message_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  seq INTEGER NOT NULL CHECK(seq >= 1),
                  message_type TEXT NOT NULL,
                  sender_role TEXT NOT NULL,
                  sender_id TEXT NOT NULL,
                  correlation_id TEXT NOT NULL,
                  causal_parent_id TEXT,
                  idempotency_key TEXT NOT NULL,
                  visibility TEXT NOT NULL,
                  payload_schema TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                  request_digest TEXT NOT NULL,
                  client_request_digest TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  UNIQUE(channel_id, seq),
                  UNIQUE(channel_id, sender_role, sender_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(causal_parent_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_reports (
                  report_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  message_id TEXT NOT NULL UNIQUE,
                  category TEXT NOT NULL,
                  phase TEXT NOT NULL,
                  severity TEXT NOT NULL,
                  status TEXT NOT NULL,
                  route TEXT NOT NULL,
                  visibility TEXT NOT NULL,
                  revision INTEGER NOT NULL CHECK(revision >= 1),
                  payload_json TEXT NOT NULL,
                  payload_digest TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_report_revisions (
                  report_id TEXT NOT NULL,
                  revision INTEGER NOT NULL CHECK(revision >= 1),
                  actor_role TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  status TEXT NOT NULL,
                  route TEXT NOT NULL,
                  visibility TEXT NOT NULL,
                  phase TEXT NOT NULL,
                  severity TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  payload_digest TEXT NOT NULL,
                  message_id TEXT,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(report_id, revision),
                  UNIQUE(report_id, actor_role, actor_id, idempotency_key),
                  FOREIGN KEY(report_id) REFERENCES collaboration_reports(report_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_report_evidence_budgets (
                  report_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  max_rounds INTEGER NOT NULL CHECK(max_rounds BETWEEN 1 AND 100),
                  rounds_used INTEGER NOT NULL DEFAULT 0 CHECK(rounds_used >= 0),
                  last_evidence_digest TEXT NOT NULL,
                  unchanged_evidence_streak INTEGER NOT NULL DEFAULT 0
                    CHECK(unchanged_evidence_streak >= 0),
                  status TEXT NOT NULL DEFAULT 'active'
                    CHECK(status IN ('active','budget_exhausted')),
                  exhausted_reason TEXT,
                  exhausted_at TEXT,
                  last_idempotency_key TEXT,
                  last_request_digest TEXT,
                  updated_at TEXT NOT NULL,
                  FOREIGN KEY(report_id) REFERENCES collaboration_reports(report_id),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_approvals (
                  approval_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  report_id TEXT NOT NULL,
                  decision TEXT NOT NULL,
                  expected_report_revision INTEGER NOT NULL,
                  resulting_report_revision INTEGER NOT NULL,
                  actor_role TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  reason TEXT,
                  message_id TEXT,
                  created_at TEXT NOT NULL,
                  UNIQUE(report_id, actor_role, actor_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(report_id) REFERENCES collaboration_reports(report_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_reader_cursors (
                  channel_id TEXT NOT NULL,
                  reader_role TEXT NOT NULL,
                  reader_id TEXT NOT NULL,
                  ack_seq INTEGER NOT NULL CHECK(ack_seq >= 0),
                  revision INTEGER NOT NULL CHECK(revision >= 1),
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(channel_id, reader_id),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_reader_ack_receipts (
                  channel_id TEXT NOT NULL,
                  reader_role TEXT NOT NULL,
                  reader_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  requested_seq INTEGER NOT NULL,
                  resulting_ack_seq INTEGER NOT NULL,
                  resulting_revision INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(channel_id, reader_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_developer_leases (
                  lease_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  report_id TEXT NOT NULL,
                  owner_id TEXT NOT NULL,
                  report_revision INTEGER NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('active','released','expired')),
                  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  acquired_at TEXT NOT NULL,
                  renewed_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  released_at TEXT,
                  UNIQUE(report_id, owner_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(report_id) REFERENCES collaboration_reports(report_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_collaboration_one_active_lease
                  ON collaboration_developer_leases(report_id) WHERE status='active';

                CREATE TABLE IF NOT EXISTS collaboration_lease_operations (
                  lease_id TEXT NOT NULL,
                  operation TEXT NOT NULL,
                  owner_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  resulting_revision INTEGER NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(lease_id, operation, owner_id, idempotency_key),
                  FOREIGN KEY(lease_id) REFERENCES collaboration_developer_leases(lease_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_developer_responses (
                  response_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  report_id TEXT NOT NULL,
                  lease_id TEXT NOT NULL,
                  owner_id TEXT NOT NULL,
                  expected_report_revision INTEGER NOT NULL,
                  resulting_report_revision INTEGER NOT NULL,
                  outcome TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  message_id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  UNIQUE(report_id, owner_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(report_id) REFERENCES collaboration_reports(report_id),
                  FOREIGN KEY(lease_id) REFERENCES collaboration_developer_leases(lease_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_task_amendments (
                  amendment_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  report_id TEXT NOT NULL,
                  task_id TEXT NOT NULL,
                  prior_task_revision INTEGER NOT NULL,
                  task_revision INTEGER NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  message_id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  UNIQUE(report_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(report_id) REFERENCES collaboration_reports(report_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_environment_responses (
                  response_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  report_id TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  message_id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  UNIQUE(report_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(report_id) REFERENCES collaboration_reports(report_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_reprobes (
                  reprobe_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  report_id TEXT NOT NULL,
                  outcome TEXT NOT NULL,
                  observed_contract_digest TEXT,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  message_id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  UNIQUE(report_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(report_id) REFERENCES collaboration_reports(report_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_verification_claims (
                  claim_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  assignment_id TEXT NOT NULL,
                  application_id TEXT NOT NULL,
                  draft_revision INTEGER NOT NULL CHECK(draft_revision >= 0),
                  content_hash TEXT NOT NULL,
                  published_version INTEGER,
                  status TEXT NOT NULL,
                  claim_revision INTEGER NOT NULL DEFAULT 1 CHECK(claim_revision >= 1),
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  message_id TEXT NOT NULL UNIQUE,
                  frozen_at TEXT NOT NULL,
                  invalidated_at TEXT,
                  invalidation_reason TEXT,
                  updated_at TEXT NOT NULL,
                  UNIQUE(channel_id, assignment_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_collaboration_claim_assignment_status
                  ON collaboration_verification_claims(channel_id, assignment_id, status);

                CREATE TABLE IF NOT EXISTS collaboration_verifications (
                  verification_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  claim_id TEXT NOT NULL UNIQUE,
                  claim_revision INTEGER NOT NULL,
                  verifier_id TEXT NOT NULL,
                  oracle_digest TEXT NOT NULL,
                  verdict TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  message_id TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  UNIQUE(claim_id, verifier_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(claim_id) REFERENCES collaboration_verification_claims(claim_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_audit (
                  audit_id TEXT PRIMARY KEY,
                  channel_id TEXT,
                  entity_kind TEXT NOT NULL,
                  entity_id TEXT NOT NULL,
                  event_type TEXT NOT NULL,
                  actor_role TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  details_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(entity_kind, entity_id, actor_role, actor_id, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_outbox (
                  outbox_id TEXT PRIMARY KEY,
                  channel_id TEXT NOT NULL,
                  message_id TEXT,
                  destination TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  status TEXT NOT NULL CHECK(status IN ('pending','delivered','failed')),
                  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
                  available_at TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  delivered_at TEXT,
                  last_error TEXT,
                  UNIQUE(channel_id, destination, idempotency_key),
                  FOREIGN KEY(channel_id) REFERENCES collaboration_channels(channel_id),
                  FOREIGN KEY(message_id) REFERENCES collaboration_messages(message_id)
                );

                CREATE TABLE IF NOT EXISTS collaboration_operation_receipts (
                  operation TEXT NOT NULL,
                  scope_id TEXT NOT NULL,
                  actor_role TEXT NOT NULL,
                  actor_id TEXT NOT NULL,
                  idempotency_key TEXT NOT NULL,
                  request_digest TEXT NOT NULL,
                  response_kind TEXT NOT NULL,
                  response_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  PRIMARY KEY(
                    operation,scope_id,actor_role,actor_id,idempotency_key
                  )
                );

                CREATE INDEX IF NOT EXISTS idx_collaboration_messages_replay
                  ON collaboration_messages(channel_id, seq);
                CREATE INDEX IF NOT EXISTS idx_collaboration_reports_status
                  ON collaboration_reports(status, route, updated_at);
                CREATE INDEX IF NOT EXISTS idx_collaboration_outbox_pending
                  ON collaboration_outbox(status, available_at, created_at);
                CREATE UNIQUE INDEX IF NOT EXISTS
                  idx_collaboration_one_open_task_revision
                  ON collaboration_channels(task_id,task_revision)
                  WHERE status IN ('created','active','disconnected','closing');
                """
            )
            message_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(collaboration_messages)"
                ).fetchall()
            }
            if "client_request_digest" not in message_columns:
                connection.execute(
                    "ALTER TABLE collaboration_messages "
                    "ADD COLUMN client_request_digest TEXT NOT NULL DEFAULT ''"
                )
            channel_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(collaboration_channels)"
                ).fetchall()
            }
            if "application_ids_json" not in channel_columns:
                # Pre-release v1 channels predate durable application binding.
                # The empty default preserves their rows but intentionally makes
                # them ineligible for new claims until recreated explicitly.
                connection.execute(
                    "ALTER TABLE collaboration_channels "
                    "ADD COLUMN application_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "max_report_evidence_rounds" not in channel_columns:
                connection.execute(
                    "ALTER TABLE collaboration_channels "
                    "ADD COLUMN max_report_evidence_rounds INTEGER"
                )
            legacy_channels = connection.execute(
                """
                SELECT * FROM collaboration_channels
                WHERE application_ids_json='[]'
                ORDER BY created_at,channel_id
                """
            ).fetchall()
            for legacy in legacy_channels:
                try:
                    metadata = json.loads(str(legacy["metadata_json"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                if not isinstance(metadata, dict):
                    metadata = {}
                if metadata.get("closure_reason") == ("legacy_channel_missing_application_binding"):
                    continue
                metadata["closure_reason"] = "legacy_channel_missing_application_binding"
                status = str(legacy["status"])
                transition_to_closed = status not in {"closed", "archived"}
                retention_until = legacy["retention_until"]
                closed_at = legacy["closed_at"]
                if closed_at is None:
                    closed_at = (
                        str(retention_until)
                        if retention_until is not None
                        and _as_utc(str(retention_until)) < _as_utc(applied_at)
                        else applied_at
                    )
                connection.execute(
                    """
                    UPDATE collaboration_channels
                    SET status=?,revision=revision+?,metadata_json=?,closed_at=?,
                        updated_at=?
                    WHERE channel_id=?
                    """,
                    (
                        "closed" if transition_to_closed else status,
                        1 if transition_to_closed else 0,
                        _canonical_json(metadata),
                        closed_at,
                        applied_at,
                        legacy["channel_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE collaboration_credentials
                    SET revoked_at=COALESCE(revoked_at,?),
                        revocation_reason=COALESCE(
                          revocation_reason,
                          'legacy channel missing application binding'
                        )
                    WHERE channel_id=? AND revoked_at IS NULL
                    """,
                    (applied_at, legacy["channel_id"]),
                )
                self._append_audit_conn(
                    connection,
                    {
                        "audit_id": f"legacy-channel-closed:{legacy['channel_id']}",
                        "channel_id": legacy["channel_id"],
                        "entity_kind": "collaboration_channel",
                        "entity_id": legacy["channel_id"],
                        "event_type": "legacy_channel_closed",
                        "actor_role": "platform",
                        "actor_id": "collaboration-schema-migration",
                        "idempotency_key": "legacy-application-binding-closure-v1",
                        "details": {"reason": "legacy_channel_missing_application_binding"},
                    },
                )
            needs_message_digest_backfill = connection.execute(
                "SELECT 1 FROM collaboration_messages WHERE client_request_digest='' LIMIT 1"
            ).fetchone()
            if needs_message_digest_backfill is not None:
                # A pre-release v1 database can already have the append-only
                # trigger. Temporarily remove only that trigger while backfilling
                # the new internal digest, then recreate it below.
                connection.execute("DROP TRIGGER IF EXISTS collaboration_messages_no_update")
                connection.execute(
                    "UPDATE collaboration_messages "
                    "SET client_request_digest=request_digest "
                    "WHERE client_request_digest=''"
                )
            needs_channel_attempt_migration = (
                current < 2
                or self._channels_have_legacy_task_revision_uniqueness(
                    connection
                )
            )
            if needs_channel_attempt_migration:
                # PRAGMA foreign_keys cannot change inside an open transaction.
                # Commit the idempotent column/backfill preparation before the
                # exact transactional parent-table rebuild. The physical
                # schema check also repairs pre-release databases that recorded
                # schema v2 before completing this rebuild.
                connection.commit()
                self._migrate_channels_for_repeated_attempts(connection)
            self._create_immutable_triggers(connection)
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS collaboration_channel_report_budget_immutable
                BEFORE UPDATE OF max_report_evidence_rounds
                ON collaboration_channels
                WHEN OLD.max_report_evidence_rounds IS NOT NEW.max_report_evidence_rounds
                BEGIN
                  SELECT RAISE(
                    ABORT,
                    'formal collaboration report evidence budget is immutable'
                  );
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS collaboration_report_budget_limit_immutable
                BEFORE UPDATE OF max_rounds
                ON collaboration_report_evidence_budgets
                WHEN OLD.max_rounds IS NOT NEW.max_rounds
                BEGIN
                  SELECT RAISE(
                    ABORT,
                    'collaboration report evidence budget limit is immutable'
                  );
                END
                """
            )
            if current < 1:
                connection.execute(
                    "INSERT INTO collaboration_schema(version,applied_at) VALUES (?,?)",
                    (1, applied_at),
                )
            if current < 2:
                connection.execute(
                    "INSERT INTO collaboration_schema(version,applied_at) VALUES (?,?)",
                    (2, applied_at),
                )
        return {"schema_version": COLLABORATION_SCHEMA_VERSION}

    @staticmethod
    def _channels_have_legacy_task_revision_uniqueness(
        connection: sqlite3.Connection,
    ) -> bool:
        row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type='table' AND name='collaboration_channels'
            """
        ).fetchone()
        if row is None or row["sql"] is None:
            return False
        normalized = re.sub(r"\s+", "", str(row["sql"])).upper()
        return "UNIQUE(TASK_ID,TASK_REVISION)" in normalized

    @staticmethod
    def _migrate_channels_for_repeated_attempts(
        connection: sqlite3.Connection,
    ) -> None:
        """Preserve closed attempts while allowing one fresh active channel.

        Schema v1 used a table-level ``UNIQUE(task_id, task_revision)``.  That
        made an immutable closed attempt permanently consume the identity and
        contradicted the public formal-authority rotation contract.  SQLite
        cannot drop the implicit unique index, so rebuild only the parent table
        transactionally and replace it with a partial active-channel index.
        """

        columns = [
            str(row["name"])
            for row in connection.execute(
                "PRAGMA table_info(collaboration_channels)"
            ).fetchall()
        ]
        expected_columns = [
            "channel_id",
            "task_id",
            "task_revision",
            "assignment_id",
            "lilies_session_id",
            "application_ids_json",
            "approval_mode",
            "max_report_evidence_rounds",
            "status",
            "revision",
            "next_seq",
            "metadata_json",
            "created_at",
            "updated_at",
            "closed_at",
            "retention_until",
        ]
        if (
            len(columns) != len(expected_columns)
            or set(columns) != set(expected_columns)
        ):
            raise CollaborationStorageError(
                "collaboration channel schema cannot be migrated exactly"
            )
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE collaboration_channels_v2 (
                  channel_id TEXT PRIMARY KEY,
                  task_id TEXT NOT NULL,
                  task_revision INTEGER NOT NULL CHECK(task_revision >= 1),
                  assignment_id TEXT NOT NULL UNIQUE,
                  lilies_session_id TEXT NOT NULL UNIQUE,
                  application_ids_json TEXT NOT NULL,
                  approval_mode TEXT NOT NULL
                    CHECK(approval_mode IN ('manual','auto_forward')),
                  max_report_evidence_rounds INTEGER
                    CHECK(max_report_evidence_rounds BETWEEN 1 AND 100),
                  status TEXT NOT NULL CHECK(status IN
                    ('created','active','disconnected','closing','closed','archived')),
                  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
                  next_seq INTEGER NOT NULL DEFAULT 1 CHECK(next_seq >= 1),
                  metadata_json TEXT NOT NULL DEFAULT '{}',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  closed_at TEXT,
                  retention_until TEXT
                )
                """
            )
            column_list = ",".join(expected_columns)
            connection.execute(
                f"INSERT INTO collaboration_channels_v2({column_list}) "
                f"SELECT {column_list} FROM collaboration_channels"
            )
            connection.execute("DROP TABLE collaboration_channels")
            connection.execute(
                "ALTER TABLE collaboration_channels_v2 "
                "RENAME TO collaboration_channels"
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX idx_collaboration_one_open_task_revision
                ON collaboration_channels(task_id,task_revision)
                WHERE status IN ('created','active','disconnected','closing')
                """
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise CollaborationStorageError(
                "collaboration channel migration broke foreign-key bindings"
            )

    @staticmethod
    def _create_immutable_triggers(connection: sqlite3.Connection) -> None:
        immutable_tables = (
            "collaboration_messages",
            "collaboration_channel_operations",
            "collaboration_report_revisions",
            "collaboration_approvals",
            "collaboration_reader_ack_receipts",
            "collaboration_lease_operations",
            "collaboration_developer_responses",
            "collaboration_task_amendments",
            "collaboration_environment_responses",
            "collaboration_reprobes",
            "collaboration_verifications",
            "collaboration_audit",
            "collaboration_operation_receipts",
        )
        for table in immutable_tables:
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END
                """
            )
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END
                """
            )

    @staticmethod
    def _channel_row(connection: sqlite3.Connection, channel_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM collaboration_channels WHERE channel_id=?", (channel_id,)
        ).fetchone()
        if row is None:
            raise CollaborationNotFound("collaboration channel not found")
        return row

    @classmethod
    def _writable_channel_row(cls, connection: sqlite3.Connection, channel_id: str) -> sqlite3.Row:
        row = cls._channel_row(connection, channel_id)
        if str(row["status"]) in _CLOSED_CHANNEL_STATUSES:
            raise CollaborationChannelClosed("collaboration channel no longer accepts writes")
        if str(row["status"]) not in _WRITABLE_CHANNEL_STATUSES:
            raise CollaborationConflict("collaboration channel is not writable")
        return row

    @staticmethod
    def _decode_row(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        json_fields = {
            "metadata_json": "metadata",
            "scopes_json": "scopes",
            "payload_json": "payload",
            "evidence_refs_json": "evidence_refs",
            "details_json": "details",
        }
        for source, target in json_fields.items():
            if source in result:
                result[target] = json.loads(str(result[source]))
        return result

    @staticmethod
    def _operation_receipt_response(
        row: sqlite3.Row | Mapping[str, Any],
    ) -> dict[str, Any]:
        response = json.loads(str(row["response_json"]))
        if not isinstance(response, dict):
            raise CollaborationStorageError(
                "persisted collaboration operation response must be an object"
            )
        return response

    @classmethod
    def _decode_operation_receipt(cls, row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        decoded = dict(row)
        decoded["response"] = cls._operation_receipt_response(row)
        decoded.pop("response_json", None)
        return decoded

    def _get_operation_receipt_conn(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        scope_id: str,
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str | None = None,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT * FROM collaboration_operation_receipts
            WHERE operation=? AND scope_id=? AND actor_role=? AND actor_id=?
              AND idempotency_key=?
            """,
            (operation, scope_id, actor_role, actor_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if request_digest is not None and not hmac.compare_digest(
            str(row["request_digest"]), request_digest
        ):
            raise CollaborationConflict(
                f"{operation} idempotency key was reused with another payload"
            )
        return self._operation_receipt_response(row)

    def _insert_operation_receipt_conn(
        self,
        connection: sqlite3.Connection,
        *,
        operation: str,
        scope_id: str,
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        response_kind: str,
        response: Mapping[str, Any],
        created_at: str,
    ) -> dict[str, Any]:
        self._reject_registered_secret_identifiers(
            {
                "operation": operation,
                "scope_id": scope_id,
                "actor_role": actor_role,
                "actor_id": actor_id,
                "idempotency_key": idempotency_key,
            }
        )
        existing = self._get_operation_receipt_conn(
            connection,
            operation=operation,
            scope_id=scope_id,
            actor_role=actor_role,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
        )
        if existing is not None:
            return existing
        redacted_response = _record(self._redact_payload(response))
        normalized_response: dict[str, Any] = {}
        for key, value in redacted_response.items():
            # ``credential`` is the name of the activation response container,
            # not a credential value.  Validate its already-public contents;
            # every other response field retains the normal fail-closed key scan.
            if key == "credential":
                sanitized_value = sanitize_collaboration_payload(value)
                validate_collaboration_payload_safety(sanitized_value)
                normalized_response[key] = sanitized_value
            else:
                sanitized_pair = sanitize_collaboration_payload({key: value})
                validate_collaboration_payload_safety(sanitized_pair)
                normalized_response.update(sanitized_pair)
        connection.execute(
            """
            INSERT INTO collaboration_operation_receipts(
              operation,scope_id,actor_role,actor_id,idempotency_key,
              request_digest,response_kind,response_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                operation,
                scope_id,
                actor_role,
                actor_id,
                idempotency_key,
                request_digest,
                response_kind,
                _canonical_json(normalized_response),
                created_at,
            ),
        )
        return normalized_response

    async def get_operation_receipt(
        self,
        operation: str,
        scope_id: str | UUID,
        actor_role: str | Enum,
        actor_id: str,
        idempotency_key: str,
        *,
        request_digest: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_digest = (
            _validated_request_digest(request_digest) if request_digest is not None else None
        )
        return await asyncio.to_thread(
            self._get_operation_receipt_sync,
            operation,
            str(scope_id),
            str(actor_role.value if isinstance(actor_role, Enum) else actor_role),
            actor_id,
            idempotency_key,
            normalized_digest,
        )

    def _get_operation_receipt_sync(
        self,
        operation: str,
        scope_id: str,
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str | None,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._get_operation_receipt_conn(
                connection,
                operation=operation,
                scope_id=scope_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )

    async def record_operation_receipt(
        self,
        *,
        operation: str,
        scope_id: str | UUID,
        actor_role: str | Enum,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        response_kind: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Persist a completed external operation for crash-safe replay."""

        normalized_digest = _validated_request_digest(request_digest)
        async with self._lock:
            return await asyncio.to_thread(
                self._record_operation_receipt_sync,
                operation,
                str(scope_id),
                str(actor_role.value if isinstance(actor_role, Enum) else actor_role),
                actor_id,
                idempotency_key,
                normalized_digest,
                response_kind,
                dict(response),
            )

    def _record_operation_receipt_sync(
        self,
        operation: str,
        scope_id: str,
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        response_kind: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._insert_operation_receipt_conn(
                connection,
                operation=operation,
                scope_id=scope_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response_kind=response_kind,
                response=response,
                created_at=_now().isoformat(),
            )

    @staticmethod
    def _decode_channel(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        projection = {
            "schema_version": "1.0",
            "channel_id": str(data["channel_id"]),
            "task_id": str(data["task_id"]),
            "task_revision": int(data["task_revision"]),
            "assignment_id": str(data["assignment_id"]),
            "lilies_session_id": str(data["lilies_session_id"]),
            "application_ids": json.loads(str(data["application_ids_json"])),
            "approval_mode": str(data["approval_mode"]),
            "max_report_evidence_rounds": (
                int(data["max_report_evidence_rounds"])
                if data.get("max_report_evidence_rounds") is not None
                else None
            ),
            "status": str(data["status"]),
            "revision": int(data["revision"]),
            "next_seq": int(data["next_seq"]),
            "created_at": str(data["created_at"]),
            "closed_at": data["closed_at"],
            "retention_until": data["retention_until"],
        }
        return _validated_projection(CollaborationChannel, projection)

    @staticmethod
    def _decode_message(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        projection = {
            "schema_version": "1.0",
            "message_id": str(data["message_id"]),
            "channel_id": str(data["channel_id"]),
            "seq": int(data["seq"]),
            "message_type": str(data["message_type"]),
            "sender_role": str(data["sender_role"]),
            "sender_id": str(data["sender_id"]),
            "correlation_id": str(data["correlation_id"]),
            "causal_parent_id": data["causal_parent_id"],
            "idempotency_key": str(data["idempotency_key"]),
            "visibility": str(data["visibility"]),
            "payload_schema": str(data["payload_schema"]),
            "payload": json.loads(str(data["payload_json"])),
            "evidence_refs": json.loads(str(data["evidence_refs_json"])),
            "created_at": str(data["created_at"]),
        }
        return _validated_projection(CollaborationMessageEnvelope, projection)

    @staticmethod
    def _decode_report(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        payload = json.loads(str(data["payload_json"]))
        if not isinstance(payload, dict):
            raise CollaborationStorageError("persisted report payload must be an object")
        result = dict(payload)
        result.update(
            {
                "schema_version": str(result.get("schema_version", "1.0")),
                "report_id": str(data["report_id"]),
                "channel_id": str(data["channel_id"]),
                "source_message_id": str(data["message_id"]),
                "category": str(data["category"]),
                "phase": str(data["phase"]),
                "severity": str(data["severity"]),
                "route": str(data["route"]),
                "status": str(data["status"]),
                "revision": int(data["revision"]),
                "created_at": str(data["created_at"]),
                "updated_at": str(data["updated_at"]),
            }
        )
        return _validated_projection(CollaborationReport, result)

    @staticmethod
    def _decode_approval(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        projection = {
            "schema_version": "1.0",
            "approval_id": str(data["approval_id"]),
            "channel_id": str(data["channel_id"]),
            "report_id": str(data["report_id"]),
            "expected_report_revision": int(data["expected_report_revision"]),
            "resulting_report_revision": int(data["resulting_report_revision"]),
            "decision": str(data["decision"]),
            "actor_id": str(data["actor_id"]),
            "reason": data["reason"],
            "idempotency_key": str(data["idempotency_key"]),
            "created_at": str(data["created_at"]),
        }
        return _validated_projection(ApprovalDecision, projection)

    async def create_channel(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        data = _record(record)
        async with self._lock:
            result = await asyncio.to_thread(self._create_channel_sync, data)
        self._secure_database_files()
        return result

    def _create_channel_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        channel_id = str(_required(data, "channel_id"))
        task_id = str(_required(data, "task_id"))
        task_revision = int(_required(data, "task_revision"))
        assignment_id = str(_required(data, "assignment_id"))
        session_id = str(_required(data, "lilies_session_id"))
        application_ids = _normalized_application_ids(data.get("application_ids"))
        approval_mode = str(data.get("approval_mode", "manual"))
        status = str(data.get("status", "created"))
        if status != "created":
            raise CollaborationConflict(
                "raw channel creation may only persist created state; use activate_channel"
            )
        created_at = _iso(data.get("created_at"), default=_now())
        retention_until = _iso(data.get("retention_until"))
        metadata = self._safe_payload(data.get("metadata", {}))
        identity = {
            "task_id": task_id,
            "task_revision": task_revision,
            "assignment_id": assignment_id,
            "lilies_session_id": session_id,
            "application_ids_json": _canonical_json(application_ids),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM collaboration_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if existing is not None:
                if any(str(existing[key]) != str(value) for key, value in identity.items()):
                    raise CollaborationConflict(
                        "channel id was reused with different task or assignment bindings"
                    )
                return self._decode_channel(existing)
            try:
                connection.execute(
                    """
                    INSERT INTO collaboration_channels(
                      channel_id,task_id,task_revision,assignment_id,lilies_session_id,
                      application_ids_json,approval_mode,status,next_seq,metadata_json,
                      created_at,updated_at,retention_until
                    ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)
                    """,
                    (
                        channel_id,
                        task_id,
                        task_revision,
                        assignment_id,
                        session_id,
                        _canonical_json(application_ids),
                        approval_mode,
                        status,
                        _canonical_json(metadata),
                        created_at,
                        created_at,
                        retention_until,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CollaborationConflict(
                    "task revision, assignment, or Lilies session already has a channel"
                ) from error
            return self._decode_channel(self._channel_row(connection, channel_id))

    async def activate_channel(
        self,
        channel_record: Mapping[str, Any] | Any,
        credential_record: Mapping[str, Any] | Any,
        bearer: str,
        message: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        channel_data = _record(channel_record)
        credential_data = _record(credential_record)
        message_data = _record(message)
        async with self._lock:
            result = await asyncio.to_thread(
                self._activate_channel_sync,
                channel_data,
                credential_data,
                bearer,
                message_data,
            )
        self._secure_database_files()
        return result

    def _activate_channel_sync(
        self,
        channel_data: dict[str, Any],
        credential_data: dict[str, Any],
        bearer: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        channel_id = str(_required(channel_data, "channel_id"))
        if str(_required(credential_data, "channel_id")) != channel_id:
            raise CollaborationConflict("activation credential belongs to another channel")
        if str(_required(message, "channel_id")) != channel_id:
            raise CollaborationConflict("activation message belongs to another channel")
        task_id = str(_required(channel_data, "task_id"))
        task_revision = int(_required(channel_data, "task_revision"))
        assignment_id = str(_required(channel_data, "assignment_id"))
        session_id = str(_required(channel_data, "lilies_session_id"))
        application_ids = _normalized_application_ids(channel_data.get("application_ids"))
        max_report_evidence_rounds = int(
            _required(channel_data, "max_report_evidence_rounds")
        )
        if not 1 <= max_report_evidence_rounds <= 100:
            raise ValueError("max_report_evidence_rounds must be between 1 and 100")
        credential_id = str(_required(credential_data, "credential_id"))
        role = str(_required(credential_data, "role"))
        idempotency_key = str(_required(credential_data, "idempotency_key"))
        scopes = sorted({str(scope) for scope in credential_data.get("scopes", [])})
        expires_at = _iso(_required(credential_data, "expires_at"))
        selector = _bearer_selector(bearer)
        semantic = {
            "credential_id": credential_id,
            "channel_id": channel_id,
            "assignment_id": assignment_id,
            "lilies_session_id": session_id,
            "application_ids": application_ids,
            "max_report_evidence_rounds": max_report_evidence_rounds,
            "role": role,
            "scopes": scopes,
            "expires_at": expires_at,
            "bearer_selector": selector,
        }
        request_digest = _idempotency_digest(semantic)
        operation_now = _now()
        now = operation_now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_channel = connection.execute(
                "SELECT * FROM collaboration_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if existing_channel is not None:
                identity_matches = (
                    str(existing_channel["task_id"]) == task_id
                    and int(existing_channel["task_revision"]) == task_revision
                    and str(existing_channel["assignment_id"]) == assignment_id
                    and str(existing_channel["lilies_session_id"]) == session_id
                    and json.loads(str(existing_channel["application_ids_json"])) == application_ids
                    and existing_channel["max_report_evidence_rounds"] is not None
                    and (
                        int(existing_channel["max_report_evidence_rounds"])
                        == max_report_evidence_rounds
                    )
                )
                if not identity_matches:
                    raise CollaborationConflict(
                        "channel id was reused with different activation bindings"
                    )
                credential = connection.execute(
                    """
                    SELECT * FROM collaboration_credentials
                    WHERE channel_id=? AND role=? AND idempotency_key=?
                    """,
                    (channel_id, role, idempotency_key),
                ).fetchone()
                if credential is None:
                    raise CollaborationConflict("channel activation is incomplete")
                same_public_fields = (
                    str(credential["credential_id"]) == credential_id
                    and str(credential["assignment_id"]) == assignment_id
                    and str(credential["lilies_session_id"]) == session_id
                    and json.loads(str(credential["scopes_json"])) == scopes
                    and str(credential["expires_at"]) == expires_at
                )
                if same_public_fields and not _verify_bearer(
                    bearer, str(credential["bearer_verifier"])
                ):
                    raise CollaborationCredentialAlreadyIssued(
                        "activation credential was already issued and cannot be recovered"
                    )
                if not same_public_fields or not hmac.compare_digest(
                    str(credential["request_digest"]), request_digest
                ):
                    raise CollaborationConflict(
                        "channel activation idempotency key was reused with another payload"
                    )
                receipt = self._get_operation_receipt_conn(
                    connection,
                    operation="channel.activate",
                    scope_id=channel_id,
                    actor_role=role,
                    actor_id=session_id,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                )
                if receipt is not None:
                    return receipt
                message_row = self._append_message_conn(connection, message)
                response = {
                    "channel": self._decode_channel(existing_channel),
                    "credential": self._credential_public(credential),
                    "message": message_row,
                }
                return self._insert_operation_receipt_conn(
                    connection,
                    operation="channel.activate",
                    scope_id=channel_id,
                    actor_role=role,
                    actor_id=session_id,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    response_kind="activation",
                    response=response,
                    created_at=now,
                )
            if not scopes:
                raise ValueError("collaboration credential scopes cannot be empty")
            if _as_utc(expires_at) <= _now():
                raise ValueError("collaboration credential expiry must be in the future")
            created_at = _iso(channel_data.get("created_at"), default=_now())
            retention_until = _iso(channel_data.get("retention_until"))
            try:
                connection.execute(
                    """
                    INSERT INTO collaboration_channels(
                      channel_id,task_id,task_revision,assignment_id,lilies_session_id,
                      application_ids_json,approval_mode,max_report_evidence_rounds,
                      status,revision,next_seq,
                      metadata_json,created_at,updated_at,retention_until
                    ) VALUES(?,?,?,?,?,?,?,?,'active',1,1,?,?,?,?)
                    """,
                    (
                        channel_id,
                        task_id,
                        task_revision,
                        assignment_id,
                        session_id,
                        _canonical_json(application_ids),
                        str(channel_data.get("approval_mode", "manual")),
                        max_report_evidence_rounds,
                        _canonical_json(self._safe_payload(channel_data.get("metadata", {}))),
                        created_at,
                        created_at,
                        retention_until,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO collaboration_credentials(
                      credential_id,channel_id,assignment_id,lilies_session_id,
                      bearer_selector,bearer_verifier,role,scopes_json,idempotency_key,
                      request_digest,expires_at,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        credential_id,
                        channel_id,
                        assignment_id,
                        session_id,
                        selector,
                        _encode_bearer(bearer),
                        role,
                        _canonical_json(scopes),
                        idempotency_key,
                        request_digest,
                        expires_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CollaborationConflict(
                    "task, assignment, session, or activation credential is already bound"
                ) from error
            message_row = self._append_message_conn(connection, message)
            channel = self._channel_row(connection, channel_id)
            credential = connection.execute(
                "SELECT * FROM collaboration_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
            if credential is None:  # pragma: no cover
                raise CollaborationStorageError("activation credential did not persist")
            response = {
                "channel": self._decode_channel(channel),
                "credential": self._credential_public(credential),
                "message": message_row,
            }
            return self._insert_operation_receipt_conn(
                connection,
                operation="channel.activate",
                scope_id=channel_id,
                actor_role=role,
                actor_id=session_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response_kind="activation",
                response=response,
                created_at=now,
            )

    async def get_channel(self, channel_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_channel_sync, str(channel_id))

    def _get_channel_sync(self, channel_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._decode_channel(self._channel_row(connection, channel_id))

    async def list_channels(
        self,
        *,
        status: str | Enum | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 5_000:
            raise ValueError("channel limit must be between 1 and 5000")
        return await asyncio.to_thread(
            self._list_channels_sync,
            (
                str(status.value if isinstance(status, Enum) else status)
                if status is not None
                else None
            ),
            limit,
        )

    def _list_channels_sync(self, status: str | None, limit: int) -> list[dict[str, Any]]:
        where = "WHERE status=?" if status is not None else ""
        parameters: tuple[Any, ...] = (status, limit) if status is not None else (limit,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM collaboration_channels {where}
                ORDER BY created_at,channel_id LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_channel(row) for row in rows]

    async def set_channel_state(
        self,
        channel_id: str | UUID,
        status: str | Enum,
        *,
        expected_status: str | Enum | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._set_channel_state_sync,
                str(channel_id),
                str(status.value if isinstance(status, Enum) else status),
                (
                    str(
                        expected_status.value
                        if isinstance(expected_status, Enum)
                        else expected_status
                    )
                    if expected_status is not None
                    else None
                ),
            )

    def _set_channel_state_sync(
        self, channel_id: str, status: str, expected_status: str | None
    ) -> dict[str, Any]:
        operation_now = _now()
        now = operation_now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._channel_row(connection, channel_id)
            current_status = str(current["status"])
            if expected_status is not None and str(current["status"]) != expected_status:
                raise CollaborationConflict("channel status compare-and-set failed")
            if expected_status is None:
                raise CollaborationConflict("channel state change requires expected_status")
            allowed = {
                ("active", "disconnected"),
                ("disconnected", "active"),
                ("closed", "archived"),
            }
            if current_status == status:
                return self._decode_channel(current)
            if (current_status, status) not in allowed:
                raise CollaborationConflict("unsupported collaboration channel transition")
            closed_at = current["closed_at"]
            try:
                connection.execute(
                    """
                    UPDATE collaboration_channels
                    SET status=?,revision=revision+1,updated_at=?,closed_at=?
                    WHERE channel_id=? AND revision=?
                    """,
                    (status, now, closed_at, channel_id, current["revision"]),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(f"unsupported collaboration channel status: {status}") from error
            return self._decode_channel(self._channel_row(connection, channel_id))

    async def set_channel_approval_mode(
        self,
        channel_id: str | UUID,
        *,
        expected_revision: int,
        idempotency_key: str,
        actor_id: str,
        message: Mapping[str, Any] | Any,
        audit: Mapping[str, Any] | Any,
        approval_mode: str | Enum | None = None,
        mode: str | Enum | None = None,
        resulting_revision: int | None = None,
    ) -> dict[str, Any]:
        selected_mode = approval_mode if approval_mode is not None else mode
        if selected_mode is None:
            raise ValueError("approval mode is required")
        data = {
            "expected_revision": expected_revision,
            "mode": (selected_mode.value if isinstance(selected_mode, Enum) else selected_mode),
            "resulting_revision": resulting_revision,
            "message": _record(message),
            "audit": _record(audit),
        }
        async with self._lock:
            result = await asyncio.to_thread(
                self._set_channel_approval_mode_sync,
                str(channel_id),
                idempotency_key,
                actor_id,
                data,
            )
        self._secure_database_files()
        return result

    def _set_channel_approval_mode_sync(
        self,
        channel_id: str,
        idempotency_key: str,
        actor_id: str,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        request_digest = _idempotency_digest(data)
        operation_digest = _validated_request_digest(
            data["message"].get("client_request_digest"), fallback=request_digest
        )
        actor_role = str(data["message"].get("sender_role", "user"))
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="channel.settings",
                scope_id=channel_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_channel_operations
                WHERE channel_id=? AND operation='approval_mode'
                  AND actor_id=? AND idempotency_key=?
                """,
                (channel_id, actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "channel operation idempotency key was reused with another payload"
                    )
                return self._decode_channel(self._channel_row(connection, channel_id))
            channel = self._writable_channel_row(connection, channel_id)
            if int(channel["revision"]) != int(data["expected_revision"]):
                raise CollaborationConflict("channel revision compare-and-set failed")
            message = self._append_message_conn(connection, data["message"])
            audit = self._append_audit_conn(connection, data["audit"])
            calculated_revision = int(channel["revision"]) + 1
            resulting_revision = data.get("resulting_revision")
            if resulting_revision is None:
                resulting_revision = calculated_revision
            resulting_revision = int(resulting_revision)
            if resulting_revision not in {int(channel["revision"]), calculated_revision}:
                raise CollaborationConflict("invalid channel resulting revision")
            try:
                connection.execute(
                    """
                    UPDATE collaboration_channels
                    SET approval_mode=?,revision=?,updated_at=?
                    WHERE channel_id=? AND revision=?
                    """,
                    (
                        str(data["mode"]),
                        resulting_revision,
                        now,
                        channel_id,
                        int(channel["revision"]),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(f"unsupported approval mode: {data['mode']}") from error
            connection.execute(
                """
                INSERT INTO collaboration_channel_operations(
                  channel_id,operation,actor_id,idempotency_key,request_digest,
                  resulting_revision,message_id,audit_id,created_at
                ) VALUES(?,'approval_mode',?,?,?,?,?,?,?)
                """,
                (
                    channel_id,
                    actor_id,
                    idempotency_key,
                    request_digest,
                    resulting_revision,
                    message["message_id"],
                    audit["audit_id"],
                    now,
                ),
            )
            response = self._decode_channel(self._channel_row(connection, channel_id))
            return self._insert_operation_receipt_conn(
                connection,
                operation="channel.settings",
                scope_id=channel_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
                response_kind="channel",
                response=response,
                created_at=now,
            )

    async def close_channel(
        self,
        channel_id: str | UUID,
        *,
        expected_revision: int | None = None,
        idempotency_key: str | None = None,
        actor_id: str = "platform",
        reason: str | None = None,
        message: Mapping[str, Any] | Any | None = None,
        audit: Mapping[str, Any] | Any | None = None,
    ) -> dict[str, Any]:
        normalized_message = _record(message) if message is not None else None
        normalized_audit = _record(audit) if audit is not None else None
        async with self._lock:
            result = await asyncio.to_thread(
                self._close_channel_sync,
                str(channel_id),
                expected_revision,
                idempotency_key or f"close:{channel_id}",
                actor_id,
                reason,
                normalized_message,
                normalized_audit,
            )
        self._secure_database_files()
        return result

    async def close_formal_channel_boundary(
        self,
        *,
        channel_id: str | UUID,
        task_id: str,
        task_revision: int,
        assignment_id: str | UUID,
        lilies_session_id: str | UUID,
        application_ids: Sequence[str | UUID],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Atomically close an exact formal channel or persist a close tombstone.

        A cancellation may win before channel activation commits in another
        platform worker. Persisting the closed identity in the channel table
        makes any later activation fail closed instead of recreating authority.
        """

        async with self._lock:
            result = await asyncio.to_thread(
                self._close_formal_channel_boundary_sync,
                str(channel_id),
                task_id,
                task_revision,
                str(assignment_id),
                str(lilies_session_id),
                [str(item) for item in application_ids],
                idempotency_key,
            )
        self._secure_database_files()
        return result

    def _close_formal_channel_boundary_sync(
        self,
        channel_id: str,
        task_id: str,
        task_revision: int,
        assignment_id: str,
        lilies_session_id: str,
        application_ids: list[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        normalized_applications = _normalized_application_ids(application_ids)
        if not normalized_applications:
            raise ValueError("formal channel close requires an application binding")
        semantic = {
            "channel_id": channel_id,
            "task_id": task_id,
            "task_revision": task_revision,
            "assignment_id": assignment_id,
            "lilies_session_id": lilies_session_id,
            "application_ids": normalized_applications,
        }
        request_digest = _idempotency_digest(semantic)
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collaboration_channels WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
            if row is not None:
                identity_matches = (
                    str(row["task_id"]) == task_id
                    and int(row["task_revision"]) == task_revision
                    and str(row["assignment_id"]) == assignment_id
                    and str(row["lilies_session_id"]) == lilies_session_id
                    and json.loads(str(row["application_ids_json"])) == normalized_applications
                )
                if not identity_matches:
                    raise CollaborationConflict(
                        "formal channel close resolved another frozen identity"
                    )
            operation = connection.execute(
                """
                SELECT * FROM collaboration_channel_operations
                WHERE channel_id=? AND operation='close'
                  AND actor_id='platform' AND idempotency_key=?
                """,
                (channel_id, idempotency_key),
            ).fetchone()
            if operation is not None:
                if not hmac.compare_digest(
                    str(operation["request_digest"]),
                    request_digest,
                ):
                    raise CollaborationConflict(
                        "formal channel close idempotency changed its identity"
                    )
                return self._decode_channel(self._channel_row(connection, channel_id))
            try:
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO collaboration_channels(
                          channel_id,task_id,task_revision,assignment_id,
                          lilies_session_id,application_ids_json,approval_mode,
                          status,revision,next_seq,metadata_json,created_at,
                          updated_at,closed_at
                        ) VALUES(?,?,?,?,?,?,'manual','closed',1,1,?,?,?,?)
                        """,
                        (
                            channel_id,
                            task_id,
                            task_revision,
                            assignment_id,
                            lilies_session_id,
                            _canonical_json(normalized_applications),
                            _canonical_json({"formal_close_tombstone": True}),
                            now,
                            now,
                            now,
                        ),
                    )
                    resulting_revision = 1
                elif str(row["status"]) in _CLOSED_CHANNEL_STATUSES:
                    resulting_revision = int(row["revision"])
                else:
                    resulting_revision = int(row["revision"]) + 1
                    cursor = connection.execute(
                        """
                        UPDATE collaboration_channels
                        SET status='closed',revision=?,closed_at=?,updated_at=?
                        WHERE channel_id=? AND revision=?
                        """,
                        (
                            resulting_revision,
                            now,
                            now,
                            channel_id,
                            int(row["revision"]),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise CollaborationConflict(
                            "formal channel changed before cancellation close"
                        )
                connection.execute(
                    """
                    UPDATE collaboration_credentials
                    SET revoked_at=COALESCE(revoked_at,?),
                        revocation_reason=COALESCE(
                          revocation_reason,
                          'formal assignment cancelled'
                        )
                    WHERE channel_id=? AND revoked_at IS NULL
                    """,
                    (now, channel_id),
                )
                connection.execute(
                    """
                    UPDATE collaboration_developer_leases
                    SET status='released',revision=revision+1,
                        renewed_at=?,released_at=?
                    WHERE channel_id=? AND status='active'
                    """,
                    (now, now, channel_id),
                )
                connection.execute(
                    """
                    INSERT INTO collaboration_channel_operations(
                      channel_id,operation,actor_id,idempotency_key,
                      request_digest,resulting_revision,message_id,audit_id,
                      created_at
                    ) VALUES(?,'close','platform',?,?,?,NULL,NULL,?)
                    """,
                    (
                        channel_id,
                        idempotency_key,
                        request_digest,
                        resulting_revision,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CollaborationConflict(
                    "formal channel close identity conflicts with persisted state"
                ) from error
            return self._decode_channel(self._channel_row(connection, channel_id))

    def _close_channel_sync(
        self,
        channel_id: str,
        expected_revision: int | None,
        idempotency_key: str,
        actor_id: str,
        reason: str | None,
        message: dict[str, Any] | None,
        audit: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = _now().isoformat()
        request_digest = _idempotency_digest(
            {
                "expected_revision": expected_revision,
                "reason": reason,
                "message": message,
                "audit": audit,
            }
        )
        operation_digest = _validated_request_digest(
            message.get("client_request_digest") if message is not None else None,
            fallback=request_digest,
        )
        actor_role = str(
            message.get("sender_role")
            if message is not None
            else ("platform" if actor_id == "platform" else "user")
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="channel.close",
                scope_id=channel_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_channel_operations
                WHERE channel_id=? AND operation='close'
                  AND actor_id=? AND idempotency_key=?
                """,
                (channel_id, actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "channel close idempotency key was reused with another payload"
                    )
                return self._decode_channel(self._channel_row(connection, channel_id))
            row = self._channel_row(connection, channel_id)
            if str(row["status"]) in {"closed", "archived"}:
                return self._decode_channel(row)
            if expected_revision is not None and int(row["revision"]) != expected_revision:
                raise CollaborationConflict("channel revision compare-and-set failed")
            message_row = (
                self._append_message_conn(connection, message) if message is not None else None
            )
            audit_row = self._append_audit_conn(connection, audit) if audit is not None else None
            resulting_revision = int(row["revision"]) + 1
            connection.execute(
                """
                UPDATE collaboration_channels
                SET status='closed',revision=?,closed_at=?,updated_at=?
                WHERE channel_id=? AND revision=?
                """,
                (resulting_revision, now, now, channel_id, int(row["revision"])),
            )
            connection.execute(
                """
                UPDATE collaboration_credentials
                SET revoked_at=COALESCE(revoked_at,?),
                    revocation_reason=COALESCE(revocation_reason,'channel closed')
                WHERE channel_id=? AND revoked_at IS NULL
                """,
                (now, channel_id),
            )
            connection.execute(
                """
                UPDATE collaboration_developer_leases
                SET status='released',revision=revision+1,
                    renewed_at=?,released_at=?
                WHERE channel_id=? AND status='active'
                """,
                (now, now, channel_id),
            )
            connection.execute(
                """
                INSERT INTO collaboration_channel_operations(
                  channel_id,operation,actor_id,idempotency_key,request_digest,
                  resulting_revision,message_id,audit_id,created_at
                ) VALUES(?,'close',?,?,?,?,?,?,?)
                """,
                (
                    channel_id,
                    actor_id,
                    idempotency_key,
                    request_digest,
                    resulting_revision,
                    message_row["message_id"] if message_row else None,
                    audit_row["audit_id"] if audit_row else None,
                    now,
                ),
            )
            response = self._decode_channel(self._channel_row(connection, channel_id))
            return self._insert_operation_receipt_conn(
                connection,
                operation="channel.close",
                scope_id=channel_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
                response_kind="channel",
                response=response,
                created_at=now,
            )

    @staticmethod
    def _credential_public(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        result = CollaborationStore._decode_row(row)
        result.pop("bearer_selector", None)
        result.pop("bearer_verifier", None)
        result.pop("request_digest", None)
        return result

    async def provision_credential(
        self, record: Mapping[str, Any] | Any, bearer: str
    ) -> dict[str, Any]:
        data = _record(record)
        async with self._lock:
            result = await asyncio.to_thread(self._provision_credential_sync, data, bearer)
        self._secure_database_files()
        return result

    def _provision_credential_sync(self, data: dict[str, Any], bearer: str) -> dict[str, Any]:
        credential_id = str(_required(data, "credential_id"))
        channel_id = str(_required(data, "channel_id"))
        assignment_id = str(_required(data, "assignment_id"))
        session_id = str(_required(data, "lilies_session_id"))
        role = str(_required(data, "role"))
        idempotency_key = str(_required(data, "idempotency_key"))
        self._reject_registered_secret_identifiers(
            {
                "credential_id": credential_id,
                "channel_id": channel_id,
                "assignment_id": assignment_id,
                "lilies_session_id": session_id,
                "role": role,
                "idempotency_key": idempotency_key,
            }
        )
        scopes = sorted({str(scope) for scope in data.get("scopes", [])})
        if not scopes:
            raise ValueError("collaboration credential scopes cannot be empty")
        expires_at = _iso(_required(data, "expires_at"))
        selector = _bearer_selector(bearer)
        semantic = {
            "credential_id": credential_id,
            "channel_id": channel_id,
            "assignment_id": assignment_id,
            "lilies_session_id": session_id,
            "role": role,
            "scopes": scopes,
            "expires_at": expires_at,
            "bearer_selector": selector,
        }
        request_digest = _idempotency_digest(semantic)
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                """
                SELECT * FROM collaboration_credentials
                WHERE channel_id=? AND role=? AND idempotency_key=?
                """,
                (channel_id, role, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "credential idempotency key was reused with another payload"
                    )
                return self._credential_public(replay)
            channel = self._writable_channel_row(connection, channel_id)
            if str(channel["assignment_id"]) != assignment_id:
                raise CollaborationConflict("credential assignment does not match its channel")
            if str(channel["lilies_session_id"]) != session_id:
                raise CollaborationConflict("credential session does not match its channel")
            if _as_utc(expires_at) <= _now():
                raise ValueError("collaboration credential expiry must be in the future")
            try:
                connection.execute(
                    """
                    INSERT INTO collaboration_credentials(
                      credential_id,channel_id,assignment_id,lilies_session_id,
                      bearer_selector,bearer_verifier,role,scopes_json,idempotency_key,
                      request_digest,expires_at,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        credential_id,
                        channel_id,
                        assignment_id,
                        session_id,
                        selector,
                        _encode_bearer(bearer),
                        role,
                        _canonical_json(scopes),
                        idempotency_key,
                        request_digest,
                        expires_at,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CollaborationConflict(
                    "collaboration credential identity is already used"
                ) from error
            row = connection.execute(
                "SELECT * FROM collaboration_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
            if row is None:  # pragma: no cover - protected by the insert
                raise CollaborationStorageError("credential insert did not persist")
            return self._credential_public(row)

    async def authenticate_credential(
        self,
        bearer: str,
        *,
        required_scope: str | Enum | None = None,
        channel_id: str | UUID | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._authenticate_credential_sync,
            bearer,
            (
                str(required_scope.value if isinstance(required_scope, Enum) else required_scope)
                if required_scope is not None
                else None
            ),
            str(channel_id) if channel_id is not None else None,
        )

    def _authenticate_credential_sync(
        self, bearer: str, required_scope: str | None, channel_id: str | None
    ) -> dict[str, Any]:
        selector = _bearer_selector(bearer)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM collaboration_credentials WHERE bearer_selector=?",
                (selector,),
            ).fetchone()
            if row is None or not _verify_bearer(bearer, str(row["bearer_verifier"])):
                raise CollaborationUnauthorized("invalid collaboration credential")
            channel = self._channel_row(connection, str(row["channel_id"]))
        if row["revoked_at"] is not None or str(channel["status"]) in _CLOSED_CHANNEL_STATUSES:
            raise CollaborationUnauthorized("collaboration credential is revoked")
        if _as_utc(str(row["expires_at"])) <= _now():
            raise CollaborationUnauthorized("collaboration credential is expired")
        if channel_id is not None and not hmac.compare_digest(str(row["channel_id"]), channel_id):
            raise CollaborationUnauthorized("collaboration credential is bound to another channel")
        scopes = json.loads(str(row["scopes_json"]))
        if required_scope is not None and required_scope not in scopes:
            raise CollaborationUnauthorized("collaboration credential lacks the required scope")
        self.register_secret_value(bearer)
        return self._credential_public(row)

    async def revoke_credential(self, credential_id: str | UUID, reason: str) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._revoke_credential_sync, str(credential_id), reason)

    def _revoke_credential_sync(self, credential_id: str, reason: str) -> dict[str, Any]:
        reason = str(self._safe_payload(reason))
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collaboration_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
            if row is None:
                raise CollaborationNotFound("collaboration credential not found")
            if row["revoked_at"] is None:
                connection.execute(
                    """
                    UPDATE collaboration_credentials
                    SET revoked_at=?,revocation_reason=? WHERE credential_id=?
                    """,
                    (now, reason, credential_id),
                )
            persisted = connection.execute(
                "SELECT * FROM collaboration_credentials WHERE credential_id=?",
                (credential_id,),
            ).fetchone()
            if persisted is None:  # pragma: no cover
                raise CollaborationStorageError("credential disappeared during revoke")
            return self._credential_public(persisted)

    async def revoke_channel_credentials(self, channel_id: str | UUID, reason: str) -> int:
        async with self._lock:
            return await asyncio.to_thread(
                self._revoke_channel_credentials_sync, str(channel_id), reason
            )

    def _revoke_channel_credentials_sync(self, channel_id: str, reason: str) -> int:
        reason = str(self._safe_payload(reason))
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._channel_row(connection, channel_id)
            cursor = connection.execute(
                """
                UPDATE collaboration_credentials
                SET revoked_at=?,revocation_reason=?
                WHERE channel_id=? AND revoked_at IS NULL
                """,
                (now, reason, channel_id),
            )
            return int(cursor.rowcount)

    def _message_semantic(self, data: Mapping[str, Any]) -> dict[str, Any]:
        projection = {
            "message_type": str(_required(data, "message_type")),
            "sender_role": str(_required(data, "sender_role")),
            "sender_id": str(_required(data, "sender_id")),
            "correlation_id": str(_required(data, "correlation_id")),
            "causal_parent_id": (
                str(data["causal_parent_id"]) if data.get("causal_parent_id") else None
            ),
            "visibility": str(_required(data, "visibility")),
            "payload_schema": str(_required(data, "payload_schema")),
            "payload": self._safe_payload(data.get("payload", {})),
            "evidence_refs": self._safe_payload(data.get("evidence_refs", [])),
        }
        return projection

    def _append_message_conn(
        self,
        connection: sqlite3.Connection,
        data: Mapping[str, Any],
        *,
        allow_closed_verification: bool = False,
        allow_closed_claim_invalidation: bool = False,
    ) -> dict[str, Any]:
        channel_id = str(_required(data, "channel_id"))
        message_id = str(_required(data, "message_id"))
        sender_role = str(_required(data, "sender_role"))
        sender_id = str(_required(data, "sender_id"))
        idempotency_key = str(_required(data, "idempotency_key"))
        self._reject_registered_secret_identifiers(
            {
                "message_id": message_id,
                "sender_role": sender_role,
                "sender_id": sender_id,
                "correlation_id": data.get("correlation_id"),
                "causal_parent_id": data.get("causal_parent_id"),
                "idempotency_key": idempotency_key,
                "evidence_refs": data.get("evidence_refs", []),
            }
        )
        semantic = self._message_semantic(data)
        request_digest = _idempotency_digest(semantic)
        client_request_digest = _validated_request_digest(
            data.get("client_request_digest"), fallback=request_digest
        )
        replay = connection.execute(
            """
            SELECT * FROM collaboration_messages
            WHERE channel_id=? AND sender_role=? AND sender_id=? AND idempotency_key=?
            """,
            (channel_id, sender_role, sender_id, idempotency_key),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                raise CollaborationConflict(
                    "message idempotency key was reused with another payload"
                )
            if not hmac.compare_digest(str(replay["client_request_digest"]), client_request_digest):
                raise CollaborationConflict(
                    "message idempotency key was reused with another client request"
                )
            return self._decode_message(replay)
        channel = self._channel_row(connection, channel_id)
        if str(channel["status"]) == "archived":
            raise CollaborationChannelClosed("archived collaboration channel is immutable")
        if str(channel["status"]) in _CLOSED_CHANNEL_STATUSES:
            if not allow_closed_verification and not allow_closed_claim_invalidation:
                raise CollaborationChannelClosed("collaboration channel no longer accepts writes")
            verification_allowed = (
                allow_closed_verification
                and semantic["message_type"] == "verification_result"
                and sender_role == "verifier"
            )
            invalidation_allowed = (
                allow_closed_claim_invalidation
                and semantic["message_type"] == "control"
                and sender_role == "platform"
            )
            if not verification_allowed and not invalidation_allowed:
                raise CollaborationChannelClosed(
                    "closed channel write is not an allowed verification security mutation"
                )
        elif str(channel["status"]) not in _WRITABLE_CHANNEL_STATUSES:
            raise CollaborationConflict("collaboration channel is not writable")
        prior_id = connection.execute(
            "SELECT * FROM collaboration_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if prior_id is not None:
            raise CollaborationConflict("message id is already used")
        causal_parent_id = semantic["causal_parent_id"]
        if causal_parent_id is not None:
            parent = connection.execute(
                "SELECT channel_id FROM collaboration_messages WHERE message_id=?",
                (causal_parent_id,),
            ).fetchone()
            if parent is None or str(parent["channel_id"]) != channel_id:
                raise CollaborationConflict(
                    "causal parent must be an existing message in the same channel"
                )
        seq = int(channel["next_seq"])
        created_at = _now().isoformat()
        try:
            connection.execute(
                """
                INSERT INTO collaboration_messages(
                  message_id,channel_id,seq,message_type,sender_role,sender_id,
                  correlation_id,causal_parent_id,idempotency_key,visibility,
                  payload_schema,payload_json,evidence_refs_json,request_digest,
                  client_request_digest,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message_id,
                    channel_id,
                    seq,
                    semantic["message_type"],
                    sender_role,
                    sender_id,
                    semantic["correlation_id"],
                    causal_parent_id,
                    idempotency_key,
                    semantic["visibility"],
                    semantic["payload_schema"],
                    _canonical_json(semantic["payload"]),
                    _canonical_json(semantic["evidence_refs"]),
                    request_digest,
                    client_request_digest,
                    created_at,
                ),
            )
            changed = connection.execute(
                """
                UPDATE collaboration_channels
                SET next_seq=?,updated_at=? WHERE channel_id=? AND next_seq=?
                """,
                (seq + 1, created_at, channel_id, seq),
            )
        except sqlite3.IntegrityError as error:
            raise CollaborationConflict("message identity or sequence conflicts") from error
        if changed.rowcount != 1:
            raise CollaborationConflict("channel sequence compare-and-set failed")
        row = connection.execute(
            "SELECT * FROM collaboration_messages WHERE message_id=?", (message_id,)
        ).fetchone()
        if row is None:  # pragma: no cover
            raise CollaborationStorageError("message insert did not persist")
        return self._decode_message(row)

    async def append_message(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        data = _record(record)
        async with self._lock:
            result = await asyncio.to_thread(self._append_message_sync, data)
        self._secure_database_files()
        return result

    def _append_message_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_message_conn(connection, data)

    async def get_message(self, message_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_message_sync, str(message_id))

    def _get_message_sync(self, message_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM collaboration_messages WHERE message_id=?", (message_id,)
            ).fetchone()
        if row is None:
            raise CollaborationNotFound("collaboration message not found")
        return self._decode_message(row)

    async def get_latest_report_chain_message(self, report_id: str | UUID) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._get_latest_report_chain_message_sync, str(report_id))

    def _get_latest_report_chain_message_sync(self, report_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            report = self._report_row(connection, report_id)
            row = connection.execute(
                """
                SELECT message.*
                FROM collaboration_report_revisions AS revision
                JOIN collaboration_messages AS message
                  ON message.message_id=revision.message_id
                WHERE revision.report_id=? AND revision.message_id IS NOT NULL
                ORDER BY revision.revision DESC LIMIT 1
                """,
                (report_id,),
            ).fetchone()
            if row is not None:
                return self._decode_message(row)
            row = connection.execute(
                """
                SELECT * FROM collaboration_messages
                WHERE channel_id=? AND correlation_id=?
                  AND (
                    message_type IN (
                      'report','approval','developer_response',
                      'task_amendment','environment_response'
                    )
                    OR (
                      message_type='control'
                      AND payload_schema='collaboration.lilies_reprobe_result.v1'
                    )
                  )
                ORDER BY seq DESC LIMIT 1
                """,
                (report["channel_id"], report_id),
            ).fetchone()
        return self._decode_message(row) if row is not None else None

    async def list_messages(
        self,
        channel_id: str | UUID,
        *,
        after_seq: int = 0,
        limit: int = 500,
        visibilities: Sequence[str | Enum] | None = None,
        lilies_claim_sender_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if after_seq < 0:
            raise ValueError("after_seq cannot be negative")
        if not 1 <= limit <= 5_000:
            raise ValueError("message limit must be between 1 and 5000")
        return await asyncio.to_thread(
            self._list_messages_sync,
            str(channel_id),
            after_seq,
            limit,
            (
                tuple(str(item.value if isinstance(item, Enum) else item) for item in visibilities)
                if visibilities is not None
                else None
            ),
            lilies_claim_sender_id,
        )

    def _list_messages_sync(
        self,
        channel_id: str,
        after_seq: int,
        limit: int,
        visibilities: tuple[str, ...] | None,
        lilies_claim_sender_id: str | None,
    ) -> list[dict[str, Any]]:
        visibility_sql = ""
        parameters: list[Any] = [channel_id, after_seq]
        if visibilities is not None:
            if not visibilities:
                return []
            visible_placeholders = ",".join("?" for _ in visibilities)
            visibility_sql = f" AND (visibility IN ({visible_placeholders})"
            parameters.extend(visibilities)
            if lilies_claim_sender_id is not None:
                visibility_sql += (
                    " OR (visibility='verifier' AND sender_role='lilies' "
                    "AND sender_id=? AND message_type='verification_claim')"
                )
                parameters.append(lilies_claim_sender_id)
            visibility_sql += ")"
        parameters.append(limit)
        with self._connect() as connection:
            self._channel_row(connection, channel_id)
            rows = connection.execute(
                f"""
                SELECT * FROM collaboration_messages
                WHERE channel_id=? AND seq>?{visibility_sql} ORDER BY seq LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_message(row) for row in rows]

    async def ack_reader(
        self,
        channel_id: str | UUID,
        reader_id: str,
        seq: int,
        *,
        idempotency_key: str,
        reader_role: str | Enum = "lilies",
        expected_cursor_revision: int | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        selected_revision = (
            expected_cursor_revision if expected_cursor_revision is not None else expected_revision
        )
        if selected_revision is None:
            raise ValueError("expected cursor revision is required")
        async with self._lock:
            result = await asyncio.to_thread(
                self._ack_reader_sync,
                str(channel_id),
                reader_id,
                seq,
                selected_revision,
                idempotency_key,
                str(reader_role.value if isinstance(reader_role, Enum) else reader_role),
            )
        self._secure_database_files()
        return result

    def _ack_reader_sync(
        self,
        channel_id: str,
        reader_id: str,
        seq: int,
        expected_cursor_revision: int,
        idempotency_key: str,
        reader_role: str,
    ) -> dict[str, Any]:
        if seq < 0:
            raise ValueError("reader ack cannot be negative")
        self._reject_registered_secret_identifiers(
            {
                "channel_id": channel_id,
                "reader_id": reader_id,
                "reader_role": reader_role,
                "idempotency_key": idempotency_key,
            }
        )
        request_digest = _idempotency_digest(
            {
                "seq": seq,
                "expected_cursor_revision": expected_cursor_revision,
                "reader_role": reader_role,
            }
        )
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            replay = connection.execute(
                """
                SELECT * FROM collaboration_reader_ack_receipts
                WHERE channel_id=? AND reader_id=? AND idempotency_key=?
                """,
                (channel_id, reader_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "reader ack idempotency key was reused with another payload"
                    )
                return _validated_projection(
                    ReaderCursor,
                    {
                        "channel_id": channel_id,
                        "reader_role": str(replay["reader_role"]),
                        "reader_id": reader_id,
                        "ack_seq": int(replay["resulting_ack_seq"]),
                        "revision": int(replay["resulting_revision"]),
                        "updated_at": (
                            str(replay["created_at"])
                            if int(replay["resulting_revision"]) > 0
                            else None
                        ),
                    },
                )
            channel = self._channel_row(connection, channel_id)
            highest_seq = int(channel["next_seq"]) - 1
            if seq > highest_seq:
                raise CollaborationConflict("reader cannot acknowledge an unseen sequence")
            cursor = connection.execute(
                """
                SELECT * FROM collaboration_reader_cursors
                WHERE channel_id=? AND reader_id=?
                """,
                (channel_id, reader_id),
            ).fetchone()
            current_seq = int(cursor["ack_seq"]) if cursor is not None else 0
            current_revision = int(cursor["revision"]) if cursor is not None else 0
            if cursor is not None and str(cursor["reader_role"]) != reader_role:
                raise CollaborationConflict("reader id is already bound to another role")
            if current_revision != expected_cursor_revision:
                raise CollaborationConflict("reader cursor revision compare-and-set failed")
            resulting_seq = max(current_seq, seq)
            resulting_revision = current_revision
            if resulting_seq > current_seq:
                resulting_revision += 1
                connection.execute(
                    """
                    INSERT INTO collaboration_reader_cursors(
                      channel_id,reader_role,reader_id,ack_seq,revision,updated_at
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(channel_id,reader_id) DO UPDATE SET
                      reader_role=excluded.reader_role,ack_seq=excluded.ack_seq,
                      revision=excluded.revision,
                      updated_at=excluded.updated_at
                    """,
                    (
                        channel_id,
                        reader_role,
                        reader_id,
                        resulting_seq,
                        resulting_revision,
                        now,
                    ),
                )
            connection.execute(
                """
                INSERT INTO collaboration_reader_ack_receipts(
                  channel_id,reader_role,reader_id,idempotency_key,request_digest,requested_seq,
                  resulting_ack_seq,resulting_revision,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    channel_id,
                    reader_role,
                    reader_id,
                    idempotency_key,
                    request_digest,
                    seq,
                    resulting_seq,
                    resulting_revision,
                    now,
                ),
            )
            return _validated_projection(
                ReaderCursor,
                {
                    "channel_id": channel_id,
                    "reader_role": reader_role,
                    "reader_id": reader_id,
                    "ack_seq": resulting_seq,
                    "revision": resulting_revision,
                    "updated_at": now if resulting_revision > 0 else None,
                },
            )

    async def get_reader_cursor(
        self,
        channel_id: str | UUID,
        reader_id: str,
        *,
        reader_role: str | Enum = "lilies",
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_reader_cursor_sync,
            str(channel_id),
            reader_id,
            str(reader_role.value if isinstance(reader_role, Enum) else reader_role),
        )

    def _get_reader_cursor_sync(
        self, channel_id: str, reader_id: str, reader_role: str
    ) -> dict[str, Any]:
        with self._connect() as connection:
            self._channel_row(connection, channel_id)
            row = connection.execute(
                """
                SELECT * FROM collaboration_reader_cursors
                WHERE channel_id=? AND reader_id=?
                """,
                (channel_id, reader_id),
            ).fetchone()
        if row is None:
            return _validated_projection(
                ReaderCursor,
                {
                    "channel_id": channel_id,
                    "reader_role": reader_role,
                    "reader_id": reader_id,
                    "ack_seq": 0,
                    "revision": 0,
                    "updated_at": None,
                },
            )
        return _validated_projection(ReaderCursor, dict(row))

    @staticmethod
    def _report_payload(data: Mapping[str, Any]) -> Any:
        if "payload" in data:
            nested = data["payload"]
            if not isinstance(nested, Mapping):
                raise ValueError("report payload must be an object")
            return {key: value for key, value in nested.items() if key in _REPORT_PAYLOAD_FIELDS}
        return {key: value for key, value in data.items() if key in _REPORT_PAYLOAD_FIELDS}

    @staticmethod
    def _report_row(connection: sqlite3.Connection, report_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM collaboration_reports WHERE report_id=?", (report_id,)
        ).fetchone()
        if row is None:
            raise CollaborationNotFound("collaboration report not found")
        return row

    @staticmethod
    def _assert_report_writes_not_frozen_conn(
        connection: sqlite3.Connection, channel_id: str
    ) -> None:
        active_claim = connection.execute(
            """
            SELECT claim_id FROM collaboration_verification_claims
            WHERE channel_id=?
              AND status IN ('frozen','ready_for_independent_verification',
                             'awaiting_independent_verification')
            ORDER BY frozen_at,claim_id LIMIT 1
            """,
            (channel_id,),
        ).fetchone()
        if active_claim is not None:
            raise CollaborationConflict(
                "collaboration reports are frozen while an independent "
                f"verification claim is active: {active_claim['claim_id']}"
            )

    def _insert_report_revision_conn(
        self,
        connection: sqlite3.Connection,
        *,
        report: sqlite3.Row | Mapping[str, Any],
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        message_id: str | None,
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO collaboration_report_revisions(
              report_id,revision,actor_role,actor_id,idempotency_key,request_digest,
              status,route,visibility,phase,severity,payload_json,payload_digest,
              message_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                report["report_id"],
                report["revision"],
                actor_role,
                actor_id,
                idempotency_key,
                request_digest,
                report["status"],
                report["route"],
                report["visibility"],
                report["phase"],
                report["severity"],
                report["payload_json"],
                report["payload_digest"],
                message_id,
                created_at,
            ),
        )

    def _exhaust_report_evidence_budget_conn(
        self,
        connection: sqlite3.Connection,
        *,
        budget: sqlite3.Row,
        report: sqlite3.Row,
        actor_role: str,
        actor_id: str,
        idempotency_key: str,
        request_digest: str,
        reason: str,
        now: str,
        attempted_rounds_used: int | None = None,
        attempted_unchanged_streak: int | None = None,
        attempted_evidence_digest: str | None = None,
    ) -> dict[str, Any]:
        report_id = str(report["report_id"])
        channel_id = str(report["channel_id"])
        rounds_used = (
            int(attempted_rounds_used)
            if attempted_rounds_used is not None
            else int(budget["rounds_used"])
        )
        connection.execute(
            """
            UPDATE collaboration_report_evidence_budgets
            SET rounds_used=?,status='budget_exhausted',exhausted_reason=?,
                exhausted_at=?,last_idempotency_key=?,last_request_digest=?,
                unchanged_evidence_streak=?,last_evidence_digest=?,updated_at=?
            WHERE report_id=? AND status='active'
            """,
            (
                rounds_used,
                reason,
                now,
                idempotency_key,
                request_digest,
                (
                    int(attempted_unchanged_streak)
                    if attempted_unchanged_streak is not None
                    else int(budget["unchanged_evidence_streak"])
                ),
                attempted_evidence_digest or str(budget["last_evidence_digest"]),
                now,
                report_id,
            ),
        )
        channel = self._channel_row(connection, channel_id)
        metadata = json.loads(str(channel["metadata_json"]))
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.update(
            {
                "closure_reason": "report_evidence_budget_exhausted",
                "budget_exhausted_report_id": report_id,
                "budget_exhausted_reason": reason,
            }
        )
        connection.execute(
            """
            UPDATE collaboration_channels
            SET status='closed',revision=revision+1,metadata_json=?,
                closed_at=COALESCE(closed_at,?),updated_at=?
            WHERE channel_id=? AND status IN ('active','disconnected')
            """,
            (_canonical_json(metadata), now, now, channel_id),
        )
        connection.execute(
            """
            UPDATE collaboration_credentials
            SET revoked_at=COALESCE(revoked_at,?),
                revocation_reason=COALESCE(
                  revocation_reason,
                  'formal report evidence budget exhausted'
                )
            WHERE channel_id=? AND revoked_at IS NULL
            """,
            (now, channel_id),
        )
        self._append_audit_conn(
            connection,
            {
                "channel_id": channel_id,
                "entity_kind": "report_evidence_budget",
                "entity_id": report_id,
                "event_type": "collaboration.report_evidence_budget_exhausted",
                "actor_role": actor_role,
                "actor_id": actor_id,
                "idempotency_key": f"budget-exhausted:{idempotency_key}",
                "details": {
                    "status": "budget_exhausted",
                    "reason": reason,
                    "rounds_used": rounds_used,
                    "max_rounds": int(budget["max_rounds"]),
                },
            },
        )
        response = {
            "budget_exhausted": True,
            "report_id": report_id,
            "channel_id": channel_id,
            "assignment_id": str(channel["assignment_id"]),
            "reason": reason,
            "rounds_used": rounds_used,
            "max_rounds": int(budget["max_rounds"]),
        }
        return self._insert_operation_receipt_conn(
            connection,
            operation="report.revise",
            scope_id=report_id,
            actor_role=actor_role,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            response_kind="report_evidence_budget_exhausted",
            response=response,
            created_at=now,
        )

    async def get_report_evidence_budget(
        self, report_id: str | UUID
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._get_report_evidence_budget_sync,
            str(report_id),
        )

    def _get_report_evidence_budget_sync(
        self, report_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM collaboration_report_evidence_budgets
                WHERE report_id=?
                """,
                (report_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    async def create_report(
        self,
        record: Mapping[str, Any] | Any,
        message: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        data = _record(record)
        message_data = _record(message)
        async with self._lock:
            result = await asyncio.to_thread(self._create_report_sync, data, message_data)
        self._secure_database_files()
        return result

    def _create_report_sync(self, data: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
        report_id = str(_required(data, "report_id"))
        channel_id = str(_required(data, "channel_id"))
        category = str(_required(data, "category"))
        phase = str(_required(data, "phase"))
        severity = str(_required(data, "severity"))
        status = str(_required(data, "status"))
        route = str(_required(data, "route"))
        visibility = str(data.get("visibility", message.get("visibility", "user_only")))
        payload = self._safe_payload(self._report_payload(data))
        payload_json = _canonical_json(payload)
        payload_digest = _digest(payload)
        expected_channel_revision = data.get("expected_channel_revision")
        intake_transitions = data.get("intake_transitions", [])
        auto_forward = data.get("auto_forward")
        initial_outbox = data.get("initial_outbox")
        semantic = {
            "report_id": report_id,
            "channel_id": channel_id,
            "category": category,
            "phase": phase,
            "severity": severity,
            "status": status,
            "route": route,
            "visibility": visibility,
            "payload_digest": payload_digest,
            "expected_channel_revision": expected_channel_revision,
            "intake_transitions": intake_transitions,
            "auto_forward": auto_forward,
            "initial_outbox": initial_outbox,
        }
        request_digest = _idempotency_digest(semantic)
        operation_digest = _validated_request_digest(
            message.get("client_request_digest"), fallback=request_digest
        )
        actor_role = str(_required(message, "sender_role"))
        actor_id = str(_required(message, "sender_id"))
        idempotency_key = str(_required(message, "idempotency_key"))
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="report.create",
                scope_id=channel_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
            )
            if operation_replay is not None:
                return operation_replay
            existing = connection.execute(
                "SELECT * FROM collaboration_reports WHERE report_id=?", (report_id,)
            ).fetchone()
            if existing is not None:
                first_revision = connection.execute(
                    """
                    SELECT request_digest FROM collaboration_report_revisions
                    WHERE report_id=? AND revision=1
                    """,
                    (report_id,),
                ).fetchone()
                if first_revision is None or not hmac.compare_digest(
                    str(first_revision["request_digest"]), request_digest
                ):
                    raise CollaborationConflict("report id was reused with another payload")
                return self._decode_report(existing)
            channel = self._writable_channel_row(connection, channel_id)
            self._assert_report_writes_not_frozen_conn(connection, channel_id)
            if expected_channel_revision is not None and int(channel["revision"]) != int(
                expected_channel_revision
            ):
                raise CollaborationConflict("channel revision compare-and-set failed")
            if str(message.get("channel_id")) != channel_id:
                raise CollaborationConflict("report message belongs to another channel")
            message_row = self._append_message_conn(connection, message)
            if str(message_row["message_type"]) != "report":
                raise CollaborationConflict("report source message must have report type")
            connection.execute(
                """
                INSERT INTO collaboration_reports(
                  report_id,channel_id,message_id,category,phase,severity,status,route,
                  visibility,revision,payload_json,payload_digest,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?)
                """,
                (
                    report_id,
                    channel_id,
                    message_row["message_id"],
                    category,
                    phase,
                    severity,
                    status,
                    route,
                    visibility,
                    payload_json,
                    payload_digest,
                    now,
                    now,
                ),
            )
            persisted = self._report_row(connection, report_id)
            if channel["max_report_evidence_rounds"] is not None:
                connection.execute(
                    """
                    INSERT INTO collaboration_report_evidence_budgets(
                      report_id,channel_id,max_rounds,rounds_used,
                      last_evidence_digest,unchanged_evidence_streak,status,updated_at
                    ) VALUES(?,?,?,0,?,0,'active',?)
                    """,
                    (
                        report_id,
                        channel_id,
                        int(channel["max_report_evidence_rounds"]),
                        _report_evidence_digest(payload),
                        now,
                    ),
                )
            self._insert_report_revision_conn(
                connection,
                report=persisted,
                actor_role=str(message_row["sender_role"]),
                actor_id=str(message_row["sender_id"]),
                idempotency_key=str(message_row["idempotency_key"]),
                request_digest=request_digest,
                message_id=str(message_row["message_id"]),
                created_at=now,
            )
            if intake_transitions:
                if (
                    category
                    not in {
                        "platform_capability_gap",
                        "platform_defect_suspected",
                    }
                    or status != "observed"
                ):
                    raise CollaborationConflict(
                        "only an observed platform report may record intake transitions"
                    )
                if not isinstance(intake_transitions, list) or len(intake_transitions) != 2:
                    raise CollaborationConflict(
                        "platform report intake requires exactly two state transitions"
                    )
                for index, raw_transition in enumerate(intake_transitions, start=1):
                    transition = _record(raw_transition)
                    transition_status = str(_required(transition, "status"))
                    transition_route = str(transition.get("route", "capability_approval"))
                    transition_visibility = str(transition.get("visibility", "user_and_lilies"))
                    transition_actor_role = str(_required(transition, "actor_role"))
                    transition_actor_id = str(_required(transition, "actor_id"))
                    transition_key = str(_required(transition, "idempotency_key"))
                    expected_status = "evidence_collecting" if index == 1 else None
                    if (
                        transition_route != "capability_approval"
                        or transition_visibility != "user_and_lilies"
                        or (expected_status is not None and transition_status != expected_status)
                        or (
                            index == 2
                            and transition_status
                            not in {"needs_more_evidence", "awaiting_user_review"}
                        )
                        or (
                            index == 1
                            and (
                                transition_actor_role != "lilies" or transition_actor_id != actor_id
                            )
                        )
                        or (index == 2 and transition_actor_role != "platform")
                    ):
                        raise CollaborationConflict(
                            "platform report intake transition violates the state authority matrix"
                        )
                    prior_revision = int(persisted["revision"])
                    changed = connection.execute(
                        """
                        UPDATE collaboration_reports
                        SET status=?,route=?,visibility=?,revision=?,updated_at=?
                        WHERE report_id=? AND revision=?
                        """,
                        (
                            transition_status,
                            transition_route,
                            transition_visibility,
                            prior_revision + 1,
                            now,
                            report_id,
                            prior_revision,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise CollaborationConflict(
                            "report intake transition compare-and-set failed"
                        )
                    persisted = self._report_row(connection, report_id)
                    self._insert_report_revision_conn(
                        connection,
                        report=persisted,
                        actor_role=transition_actor_role,
                        actor_id=transition_actor_id,
                        idempotency_key=transition_key,
                        request_digest=_idempotency_digest(transition),
                        message_id=None,
                        created_at=now,
                    )
            if expected_channel_revision is not None:
                changed = connection.execute(
                    """
                    UPDATE collaboration_channels SET revision=revision+1,updated_at=?
                    WHERE channel_id=? AND revision=?
                    """,
                    (now, channel_id, int(expected_channel_revision)),
                )
                if changed.rowcount != 1:
                    raise CollaborationConflict("channel revision compare-and-set failed")
            if initial_outbox is not None:
                if not isinstance(initial_outbox, Mapping):
                    raise ValueError("initial_outbox must be a mapping")
                self._enqueue_outbox_conn(connection, initial_outbox)
            if auto_forward is not None:
                if not isinstance(auto_forward, Mapping):
                    raise ValueError("auto_forward must be a mapping")
                approval = _record(_required(auto_forward, "approval"))
                audit = _record(_required(auto_forward, "audit"))
                outbox = _record(_required(auto_forward, "outbox"))
                approval_message_data = auto_forward.get("message")
                approval_message = (
                    self._append_message_conn(connection, _record(approval_message_data))
                    if approval_message_data is not None
                    else None
                )
                approval_id = str(_required(approval, "approval_id"))
                approval_actor = str(approval.get("actor_id", "platform-auto-forward"))
                approval_key = str(_required(approval, "idempotency_key"))
                approval_decision = str(approval.get("decision", "approve"))
                approval_expected = int(
                    approval.get("expected_report_revision", int(persisted["revision"]))
                )
                if approval_expected != int(persisted["revision"]):
                    raise CollaborationConflict(
                        "auto-forward approval report revision does not match intake"
                    )
                approval_resulting = approval_expected + 1
                auto_status = str(auto_forward.get("next_report_status", "approved_for_codex"))
                auto_route = str(auto_forward.get("next_report_route", "developer"))
                auto_visibility = str(auto_forward.get("next_visibility", "approved_developer"))
                approval_digest = _digest(
                    {
                        "approval": approval,
                        "status": auto_status,
                        "route": auto_route,
                        "visibility": auto_visibility,
                    }
                )
                connection.execute(
                    """
                    UPDATE collaboration_reports
                    SET status=?,route=?,visibility=?,revision=?,updated_at=?
                    WHERE report_id=? AND revision=?
                    """,
                    (
                        auto_status,
                        auto_route,
                        auto_visibility,
                        approval_resulting,
                        now,
                        report_id,
                        approval_expected,
                    ),
                )
                forwarded = self._report_row(connection, report_id)
                self._insert_report_revision_conn(
                    connection,
                    report=forwarded,
                    actor_role="platform",
                    actor_id=approval_actor,
                    idempotency_key=approval_key,
                    request_digest=approval_digest,
                    message_id=(
                        str(approval_message["message_id"])
                        if approval_message is not None
                        else None
                    ),
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO collaboration_approvals(
                      approval_id,channel_id,report_id,decision,
                      expected_report_revision,resulting_report_revision,actor_role,
                      actor_id,idempotency_key,request_digest,reason,message_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        approval_id,
                        channel_id,
                        report_id,
                        approval_decision,
                        approval_expected,
                        approval_resulting,
                        "platform",
                        approval_actor,
                        approval_key,
                        approval_digest,
                        approval.get("reason"),
                        (approval_message["message_id"] if approval_message is not None else None),
                        now,
                    ),
                )
                self._append_audit_conn(connection, audit)
                self._enqueue_outbox_conn(connection, outbox)
                persisted = forwarded
            response = self._decode_report(persisted)
            return self._insert_operation_receipt_conn(
                connection,
                operation="report.create",
                scope_id=channel_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
                response_kind="report",
                response=response,
                created_at=now,
            )

    async def get_report(self, report_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_report_sync, str(report_id))

    def _get_report_sync(self, report_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._decode_report(self._report_row(connection, report_id))

    async def list_reports(
        self,
        *,
        channel_id: str | UUID | None = None,
        statuses: Sequence[str | Enum] | None = None,
        developer_visible_only: bool = False,
        route: str | Enum | None = None,
        after: int = 0,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 5_000:
            raise ValueError("report pagination is out of range")
        normalized_statuses = (
            tuple(str(item.value if isinstance(item, Enum) else item) for item in statuses)
            if statuses is not None
            else None
        )
        return await asyncio.to_thread(
            self._list_reports_sync,
            str(channel_id) if channel_id is not None else None,
            normalized_statuses,
            developer_visible_only,
            (str(route.value if isinstance(route, Enum) else route) if route is not None else None),
            after,
            limit,
        )

    def _list_reports_sync(
        self,
        channel_id: str | None,
        statuses: tuple[str, ...] | None,
        developer_visible_only: bool,
        route: str | None,
        after: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if channel_id is not None:
            clauses.append("channel_id=?")
            parameters.append(channel_id)
        if statuses is not None:
            if not statuses:
                return []
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(statuses)
        if developer_visible_only:
            visible_statuses = tuple(sorted(_DEVELOPER_VISIBLE_REPORT_STATUSES))
            direct_routes = tuple(sorted(_DIRECT_DEVELOPER_ROUTES))
            clauses.append(
                f"(status IN ({','.join('?' for _ in visible_statuses)}) "
                f"OR route IN ({','.join('?' for _ in direct_routes)}))"
            )
            parameters.extend((*visible_statuses, *direct_routes))
        if route is not None:
            clauses.append("route=?")
            parameters.append(route)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, after))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM collaboration_reports {where}
                ORDER BY updated_at,report_id LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_report(row) for row in rows]

    async def revise_report(
        self,
        report_id: str | UUID,
        expected_revision: int,
        idempotency_key: str,
        actor_role: str | Enum,
        actor_id: str,
        changes: Mapping[str, Any],
        message: Mapping[str, Any] | Any | None = None,
        auto_forward: Mapping[str, Any] | Any | None = None,
        validation_transition: Mapping[str, Any] | Any | None = None,
        audit: Mapping[str, Any] | Any | None = None,
        expected_channel_revision: int | None = None,
        expected_approval_mode: str | None = None,
        consume_evidence_budget: bool = False,
    ) -> dict[str, Any]:
        normalized = _record(changes)
        normalized_message = _record(message) if message is not None else None
        normalized_auto_forward = _record(auto_forward) if auto_forward is not None else None
        normalized_validation_transition = (
            _record(validation_transition) if validation_transition is not None else None
        )
        normalized_audit = _record(audit) if audit is not None else None
        async with self._lock:
            result = await asyncio.to_thread(
                self._revise_report_sync,
                str(report_id),
                expected_revision,
                idempotency_key,
                str(actor_role.value if isinstance(actor_role, Enum) else actor_role),
                actor_id,
                normalized,
                normalized_message,
                normalized_auto_forward,
                normalized_validation_transition,
                normalized_audit,
                expected_channel_revision,
                expected_approval_mode,
                consume_evidence_budget,
            )
        self._secure_database_files()
        return result

    def _revise_report_sync(
        self,
        report_id: str,
        expected_revision: int,
        idempotency_key: str,
        actor_role: str,
        actor_id: str,
        changes: dict[str, Any],
        message: dict[str, Any] | None,
        auto_forward: dict[str, Any] | None,
        validation_transition: dict[str, Any] | None,
        audit: dict[str, Any] | None,
        expected_channel_revision: int | None,
        expected_approval_mode: str | None,
        consume_evidence_budget: bool,
    ) -> dict[str, Any]:
        request_digest = _idempotency_digest(
            {
                "expected_revision": expected_revision,
                "changes": changes,
                "message": message,
                "auto_forward": auto_forward,
                "validation_transition": validation_transition,
                "audit": audit,
                "expected_channel_revision": expected_channel_revision,
                "expected_approval_mode": expected_approval_mode,
                "consume_evidence_budget": consume_evidence_budget,
            }
        )
        operation_digest = _validated_request_digest(
            message.get("client_request_digest") if message is not None else None,
            fallback=request_digest,
        )
        operation_now = _now()
        now = operation_now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="report.revise",
                scope_id=report_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_report_revisions
                WHERE report_id=? AND actor_role=? AND actor_id=? AND idempotency_key=?
                """,
                (report_id, actor_role, actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "report revision idempotency key was reused with another payload"
                    )
                return self._decode_report(self._report_row(connection, report_id))
            self._expire_developer_leases_conn(
                connection,
                operation_now,
                fallback_report_status="approved_for_codex",
            )
            current = self._report_row(connection, report_id)
            channel = self._writable_channel_row(connection, str(current["channel_id"]))
            if auto_forward is not None and (
                expected_channel_revision is None
                or int(channel["revision"]) != expected_channel_revision
                or expected_approval_mode != "auto_forward"
                or str(channel["approval_mode"]) != expected_approval_mode
            ):
                raise CollaborationConflict(
                    "auto-forward approval mode changed before revision commit"
                )
            self._assert_report_writes_not_frozen_conn(connection, str(current["channel_id"]))
            if int(current["revision"]) != expected_revision:
                raise CollaborationConflict("report revision compare-and-set failed")
            active_lease = connection.execute(
                """
                SELECT 1 FROM collaboration_developer_leases
                WHERE report_id=? AND status='active' LIMIT 1
                """,
                (report_id,),
            ).fetchone()
            if active_lease is not None:
                raise CollaborationConflict(
                    "report cannot be revised while a developer lease is active"
                )
            replacement_payload = self._report_payload(changes)
            current_payload = json.loads(str(current["payload_json"]))
            if not isinstance(current_payload, dict):
                raise CollaborationStorageError("persisted report payload must be an object")
            if replacement_payload:
                safe_replacement_payload = self._safe_payload(replacement_payload)
                for immutable_field in ("original_goal", "requirement_digest"):
                    if safe_replacement_payload.get(immutable_field) != current_payload.get(
                        immutable_field
                    ):
                        raise CollaborationConflict(
                            f"report {immutable_field} cannot change during revision"
                        )
                payload = safe_replacement_payload
            else:
                payload = self._safe_payload(current_payload)
            payload_json = _canonical_json(payload)
            payload_digest = _digest(payload)
            if consume_evidence_budget:
                if actor_role != "lilies" or message is None:
                    raise CollaborationConflict(
                        "only a Lilies evidence supplementation may consume "
                        "the report evidence budget"
                    )
                budget = connection.execute(
                    """
                    SELECT * FROM collaboration_report_evidence_budgets
                    WHERE report_id=?
                    """,
                    (report_id,),
                ).fetchone()
                if budget is not None:
                    if str(budget["status"]) == "budget_exhausted":
                        return self._exhaust_report_evidence_budget_conn(
                            connection,
                            budget=budget,
                            report=current,
                            actor_role=actor_role,
                            actor_id=actor_id,
                            idempotency_key=idempotency_key,
                            request_digest=operation_digest,
                            reason=str(
                                budget["exhausted_reason"]
                                or "max_report_evidence_rounds"
                            ),
                            now=now,
                        )
                    if int(budget["rounds_used"]) >= int(budget["max_rounds"]):
                        return self._exhaust_report_evidence_budget_conn(
                            connection,
                            budget=budget,
                            report=current,
                            actor_role=actor_role,
                            actor_id=actor_id,
                            idempotency_key=idempotency_key,
                            request_digest=operation_digest,
                            reason="max_report_evidence_rounds",
                            now=now,
                        )
                    evidence_digest = _report_evidence_digest(payload)
                    unchanged_streak = (
                        int(budget["unchanged_evidence_streak"]) + 1
                        if hmac.compare_digest(
                            str(budget["last_evidence_digest"]),
                            evidence_digest,
                        )
                        else 0
                    )
                    if unchanged_streak >= 3:
                        return self._exhaust_report_evidence_budget_conn(
                            connection,
                            budget=budget,
                            report=current,
                            actor_role=actor_role,
                            actor_id=actor_id,
                            idempotency_key=idempotency_key,
                            request_digest=operation_digest,
                            reason="unchanged_evidence_digest_three_times",
                            now=now,
                            attempted_rounds_used=int(budget["rounds_used"]) + 1,
                            attempted_unchanged_streak=unchanged_streak,
                            attempted_evidence_digest=evidence_digest,
                        )
                    connection.execute(
                        """
                        UPDATE collaboration_report_evidence_budgets
                        SET rounds_used=rounds_used+1,last_evidence_digest=?,
                            unchanged_evidence_streak=?,last_idempotency_key=?,
                            last_request_digest=?,updated_at=?
                        WHERE report_id=? AND status='active'
                        """,
                        (
                            evidence_digest,
                            unchanged_streak,
                            idempotency_key,
                            operation_digest,
                            now,
                            report_id,
                        ),
                    )
            message_row = (
                self._append_message_conn(connection, message) if message is not None else None
            )
            new_revision = expected_revision + 1
            connection.execute(
                """
                UPDATE collaboration_reports
                SET message_id=?,status=?,route=?,visibility=?,phase=?,severity=?,revision=?,
                    payload_json=?,payload_digest=?,updated_at=?
                WHERE report_id=? AND revision=?
                """,
                (
                    (
                        str(message_row["message_id"])
                        if message_row is not None
                        else str(current["message_id"])
                    ),
                    str(changes.get("status", current["status"])),
                    str(changes.get("route", current["route"])),
                    str(changes.get("visibility", current["visibility"])),
                    str(changes.get("phase", current["phase"])),
                    str(changes.get("severity", current["severity"])),
                    new_revision,
                    payload_json,
                    payload_digest,
                    now,
                    report_id,
                    expected_revision,
                ),
            )
            persisted = self._report_row(connection, report_id)
            self._insert_report_revision_conn(
                connection,
                report=persisted,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                message_id=(str(message_row["message_id"]) if message_row else None),
                created_at=now,
            )
            if validation_transition is not None:
                transition_status = str(_required(validation_transition, "status"))
                transition_route = str(validation_transition.get("route", "capability_approval"))
                transition_visibility = str(
                    validation_transition.get("visibility", "user_and_lilies")
                )
                transition_actor_role = str(_required(validation_transition, "actor_role"))
                transition_actor_id = str(_required(validation_transition, "actor_id"))
                transition_key = str(_required(validation_transition, "idempotency_key"))
                if (
                    str(current["category"])
                    not in {
                        "platform_capability_gap",
                        "platform_defect_suspected",
                    }
                    or str(persisted["status"]) != "evidence_collecting"
                    or actor_role != "lilies"
                    or transition_actor_role != "platform"
                    or transition_status not in {"needs_more_evidence", "awaiting_user_review"}
                    or transition_route != "capability_approval"
                    or transition_visibility != "user_and_lilies"
                ):
                    raise CollaborationConflict(
                        "report validation transition violates the state authority matrix"
                    )
                validation_expected = int(persisted["revision"])
                changed = connection.execute(
                    """
                    UPDATE collaboration_reports
                    SET status=?,route=?,visibility=?,revision=?,updated_at=?
                    WHERE report_id=? AND revision=?
                    """,
                    (
                        transition_status,
                        transition_route,
                        transition_visibility,
                        validation_expected + 1,
                        now,
                        report_id,
                        validation_expected,
                    ),
                )
                if changed.rowcount != 1:
                    raise CollaborationConflict(
                        "report validation transition compare-and-set failed"
                    )
                persisted = self._report_row(connection, report_id)
                self._insert_report_revision_conn(
                    connection,
                    report=persisted,
                    actor_role=transition_actor_role,
                    actor_id=transition_actor_id,
                    idempotency_key=transition_key,
                    request_digest=_idempotency_digest(validation_transition),
                    message_id=None,
                    created_at=now,
                )
            if auto_forward is not None:
                approval = _record(_required(auto_forward, "approval"))
                auto_audit = _record(_required(auto_forward, "audit"))
                outbox = _record(_required(auto_forward, "outbox"))
                approval_message = self._append_message_conn(
                    connection, _record(_required(auto_forward, "message"))
                )
                approval_id = str(_required(approval, "approval_id"))
                approval_actor = str(approval.get("actor_id", "platform-auto-forward"))
                approval_key = str(_required(approval, "idempotency_key"))
                approval_decision = str(approval.get("decision", "approve"))
                approval_expected = int(
                    approval.get("expected_report_revision", int(persisted["revision"]))
                )
                if approval_expected != int(persisted["revision"]):
                    raise CollaborationConflict(
                        "auto-forward approval report revision does not match revision"
                    )
                approval_resulting = approval_expected + 1
                auto_status = str(auto_forward.get("next_report_status", "approved_for_codex"))
                auto_route = str(auto_forward.get("next_report_route", "developer"))
                auto_visibility = str(auto_forward.get("next_visibility", "approved_developer"))
                approval_reason = (
                    str(self._safe_payload(approval["reason"]))
                    if approval.get("reason") is not None
                    else None
                )
                approval_digest = _idempotency_digest(
                    {
                        "approval": {**approval, "reason": approval_reason},
                        "status": auto_status,
                        "route": auto_route,
                        "visibility": auto_visibility,
                    }
                )
                changed = connection.execute(
                    """
                    UPDATE collaboration_reports
                    SET status=?,route=?,visibility=?,revision=?,updated_at=?
                    WHERE report_id=? AND revision=?
                    """,
                    (
                        auto_status,
                        auto_route,
                        auto_visibility,
                        approval_resulting,
                        now,
                        report_id,
                        approval_expected,
                    ),
                )
                if changed.rowcount != 1:
                    raise CollaborationConflict(
                        "auto-forward report revision compare-and-set failed"
                    )
                persisted = self._report_row(connection, report_id)
                self._decode_report(persisted)
                self._insert_report_revision_conn(
                    connection,
                    report=persisted,
                    actor_role="platform",
                    actor_id=approval_actor,
                    idempotency_key=approval_key,
                    request_digest=approval_digest,
                    message_id=str(approval_message["message_id"]),
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO collaboration_approvals(
                      approval_id,channel_id,report_id,decision,
                      expected_report_revision,resulting_report_revision,actor_role,
                      actor_id,idempotency_key,request_digest,reason,message_id,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        approval_id,
                        current["channel_id"],
                        report_id,
                        approval_decision,
                        approval_expected,
                        approval_resulting,
                        "platform",
                        approval_actor,
                        approval_key,
                        approval_digest,
                        approval_reason,
                        approval_message["message_id"],
                        now,
                    ),
                )
                self._append_audit_conn(connection, auto_audit)
                self._enqueue_outbox_conn(connection, outbox)
            if audit is not None:
                self._append_audit_conn(connection, audit)
            response = self._decode_report(persisted)
            return self._insert_operation_receipt_conn(
                connection,
                operation="report.revise",
                scope_id=report_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
                response_kind="report",
                response=response,
                created_at=now,
            )

    async def record_approval(
        self,
        record: Mapping[str, Any] | Any,
        next_report_status: str | Enum,
        message: Mapping[str, Any] | Any | None = None,
        outbox: Mapping[str, Any] | Any | None = None,
    ) -> dict[str, Any]:
        data = _record(record)
        message_data = _record(message) if message is not None else None
        outbox_data = _record(outbox) if outbox is not None else None
        async with self._lock:
            result = await asyncio.to_thread(
                self._record_approval_sync,
                data,
                str(
                    next_report_status.value
                    if isinstance(next_report_status, Enum)
                    else next_report_status
                ),
                message_data,
                outbox_data,
            )
        self._secure_database_files()
        return result

    def _record_approval_sync(
        self,
        data: dict[str, Any],
        next_report_status: str,
        message: dict[str, Any] | None,
        outbox: dict[str, Any] | None,
    ) -> dict[str, Any]:
        approval_id = str(_required(data, "approval_id"))
        channel_id = str(_required(data, "channel_id"))
        report_id = str(_required(data, "report_id"))
        expected_revision = int(_required(data, "expected_report_revision"))
        decision = str(_required(data, "decision"))
        actor_role = str(data.get("actor_role", "user"))
        actor_id = str(_required(data, "actor_id"))
        idempotency_key = str(_required(data, "idempotency_key"))
        reason = str(self._safe_payload(data["reason"])) if data.get("reason") is not None else None
        semantic = {
            "approval_id": approval_id,
            "channel_id": channel_id,
            "report_id": report_id,
            "expected_report_revision": expected_revision,
            "decision": decision,
            "actor_role": actor_role,
            "actor_id": actor_id,
            "reason": reason,
            "next_report_status": next_report_status,
            "message": message,
            "outbox": outbox,
        }
        request_digest = _idempotency_digest(semantic)
        operation_digest = _validated_request_digest(
            message.get("client_request_digest") if message is not None else None,
            fallback=request_digest,
        )
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="report.approval",
                scope_id=report_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_approvals
                WHERE report_id=? AND actor_role=? AND actor_id=? AND idempotency_key=?
                """,
                (report_id, actor_role, actor_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "approval idempotency key was reused with another payload"
                    )
                return self._decode_approval(replay)
            current = self._report_row(connection, report_id)
            self._writable_channel_row(connection, channel_id)
            if str(current["channel_id"]) != channel_id:
                raise CollaborationConflict("approval report belongs to another channel")
            if int(current["revision"]) != expected_revision:
                raise CollaborationConflict("report revision compare-and-set failed")
            message_row = (
                self._append_message_conn(connection, message) if message is not None else None
            )
            resulting_revision = expected_revision + 1
            next_route = str(data.get("next_report_route", current["route"]))
            next_visibility = str(data.get("next_visibility", current["visibility"]))
            connection.execute(
                """
                UPDATE collaboration_reports
                SET status=?,route=?,visibility=?,revision=?,updated_at=?
                WHERE report_id=? AND revision=?
                """,
                (
                    next_report_status,
                    next_route,
                    next_visibility,
                    resulting_revision,
                    now,
                    report_id,
                    expected_revision,
                ),
            )
            persisted = self._report_row(connection, report_id)
            revision_digest = _digest(
                {
                    "approval_id": approval_id,
                    "status": next_report_status,
                    "route": next_route,
                    "visibility": next_visibility,
                }
            )
            self._insert_report_revision_conn(
                connection,
                report=persisted,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=revision_digest,
                message_id=(str(message_row["message_id"]) if message_row else None),
                created_at=now,
            )
            connection.execute(
                """
                INSERT INTO collaboration_approvals(
                  approval_id,channel_id,report_id,decision,expected_report_revision,
                  resulting_report_revision,actor_role,actor_id,idempotency_key,
                  request_digest,reason,message_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    approval_id,
                    channel_id,
                    report_id,
                    decision,
                    expected_revision,
                    resulting_revision,
                    actor_role,
                    actor_id,
                    idempotency_key,
                    request_digest,
                    reason,
                    message_row["message_id"] if message_row else None,
                    now,
                ),
            )
            if outbox is not None:
                self._enqueue_outbox_conn(connection, outbox)
            row = connection.execute(
                "SELECT * FROM collaboration_approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise CollaborationStorageError("approval insert did not persist")
            response = self._decode_approval(row)
            return self._insert_operation_receipt_conn(
                connection,
                operation="report.approval",
                scope_id=report_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
                response_kind="approval",
                response=response,
                created_at=now,
            )

    async def get_approval(self, approval_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_approval_sync, str(approval_id))

    def _get_approval_sync(self, approval_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM collaboration_approvals WHERE approval_id=?",
                (approval_id,),
            ).fetchone()
        if row is None:
            raise CollaborationNotFound("collaboration approval not found")
        return self._decode_approval(row)

    @staticmethod
    def _decode_lease(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        projection = {
            "schema_version": "1.0",
            "lease_id": str(data["lease_id"]),
            "report_id": str(data["report_id"]),
            "report_revision": int(data["report_revision"]),
            "owner_id": str(data["owner_id"]),
            "status": str(data["status"]),
            "revision": int(data["revision"]),
            "acquired_at": str(data["acquired_at"]),
            "heartbeat_at": str(data["renewed_at"]),
            "expires_at": str(data["expires_at"]),
            "released_at": data["released_at"],
        }
        return _validated_projection(DeveloperLease, projection)

    @staticmethod
    def _lease_row(connection: sqlite3.Connection, lease_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM collaboration_developer_leases WHERE lease_id=?", (lease_id,)
        ).fetchone()
        if row is None:
            raise CollaborationNotFound("developer lease not found")
        return row

    @staticmethod
    def _active_lease_reference(
        connection: sqlite3.Connection, lease_or_report_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM collaboration_developer_leases WHERE lease_id=?",
            (lease_or_report_id,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT * FROM collaboration_developer_leases
                WHERE report_id=? AND status='active'
                """,
                (lease_or_report_id,),
            ).fetchone()
        if row is None:
            raise CollaborationNotFound("active developer lease not found")
        return row

    @staticmethod
    def _lease_reference(connection: sqlite3.Connection, lease_or_report_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM collaboration_developer_leases WHERE lease_id=?",
            (lease_or_report_id,),
        ).fetchone()
        if row is None:
            row = connection.execute(
                """
                SELECT * FROM collaboration_developer_leases
                WHERE report_id=? ORDER BY acquired_at DESC,lease_id DESC LIMIT 1
                """,
                (lease_or_report_id,),
            ).fetchone()
        if row is None:
            raise CollaborationNotFound("developer lease not found")
        return row

    def _expire_developer_leases_conn(
        self,
        connection: sqlite3.Connection,
        now: datetime,
        fallback_report_status: str,
    ) -> int:
        now_iso = now.isoformat()
        rows = connection.execute(
            """
            SELECT * FROM collaboration_developer_leases
            WHERE status='active' AND expires_at<=? ORDER BY expires_at,lease_id
            """,
            (now_iso,),
        ).fetchall()
        expired = 0
        for lease in rows:
            changed = connection.execute(
                """
                UPDATE collaboration_developer_leases
                SET status='expired',revision=revision+1
                WHERE lease_id=? AND status='active' AND revision=?
                """,
                (lease["lease_id"], lease["revision"]),
            )
            if changed.rowcount != 1:
                continue
            expired += 1
            report = self._report_row(connection, str(lease["report_id"]))
            if (
                str(report["status"]) == "implementing"
                or (
                    str(report["category"]) == "environment_gap"
                    and str(report["status"]) == "routed_to_task_author"
                )
                or (
                    str(report["category"]) == "task_spec_gap"
                    and str(report["status"]) == "routed_to_task_author"
                )
            ) and int(report["revision"]) == int(lease["report_revision"]):
                target_status = fallback_report_status
                if str(report["category"]) == "environment_gap":
                    target_status = "environment_failed"
                elif str(report["category"]) == "task_spec_gap":
                    target_status = "routed_to_task_author"
                next_revision = int(report["revision"]) + 1
                connection.execute(
                    """
                    UPDATE collaboration_reports
                    SET status=?,revision=?,updated_at=?
                    WHERE report_id=? AND revision=?
                    """,
                    (
                        target_status,
                        next_revision,
                        now_iso,
                        report["report_id"],
                        report["revision"],
                    ),
                )
                persisted = self._report_row(connection, str(report["report_id"]))
                receipt_key = f"lease-expired:{lease['lease_id']}:{lease['revision']}"
                self._insert_report_revision_conn(
                    connection,
                    report=persisted,
                    actor_role="platform",
                    actor_id="collaboration-lease-reaper",
                    idempotency_key=receipt_key,
                    request_digest=_idempotency_digest(
                        {
                            "lease_id": lease["lease_id"],
                            "fallback_status": target_status,
                        }
                    ),
                    message_id=None,
                    created_at=now_iso,
                )
                self._enqueue_outbox_conn(
                    connection,
                    {
                        "channel_id": str(lease["channel_id"]),
                        "message_id": None,
                        "destination": "developer_inbox",
                        "idempotency_key": f"inbox:{receipt_key}",
                        "payload": {"report_id": str(report["report_id"])},
                    },
                )
        return expired

    async def acquire_developer_lease(
        self,
        record: Mapping[str, Any] | Any,
        ttl_seconds: int = 900,
        now: datetime | str | None = None,
        next_report_status: str | Enum | None = None,
    ) -> dict[str, Any]:
        data = _record(record)
        effective_now = _as_utc(now, default=_now())
        async with self._lock:
            result = await asyncio.to_thread(
                self._acquire_developer_lease_sync,
                data,
                ttl_seconds,
                effective_now,
                (
                    str(
                        next_report_status.value
                        if isinstance(next_report_status, Enum)
                        else next_report_status
                    )
                    if next_report_status is not None
                    else None
                ),
            )
        self._secure_database_files()
        return result

    def _acquire_developer_lease_sync(
        self,
        data: dict[str, Any],
        ttl_seconds: int,
        now: datetime,
        next_report_status: str | None,
    ) -> dict[str, Any]:
        if not 1 <= ttl_seconds <= 900:
            raise ValueError("lease ttl_seconds must be between 1 and 900")
        lease_id = str(_required(data, "lease_id"))
        report_id = str(_required(data, "report_id"))
        owner_id = str(_required(data, "owner_id"))
        expected_revision = int(
            data.get("expected_report_revision", data.get("report_revision", 0))
        )
        if expected_revision < 1:
            raise ValueError("expected_report_revision is required")
        idempotency_key = str(_required(data, "idempotency_key"))
        semantic = {
            "report_id": report_id,
            "owner_id": owner_id,
            "expected_report_revision": expected_revision,
            "ttl_seconds": ttl_seconds,
        }
        request_digest = _idempotency_digest(semantic)
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="lease.acquire",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_developer_leases
                WHERE report_id=? AND owner_id=? AND idempotency_key=?
                """,
                (report_id, owner_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "lease idempotency key was reused with another payload"
                    )
                return self._decode_lease(replay)
            self._expire_developer_leases_conn(
                connection, now, fallback_report_status="approved_for_codex"
            )
            report = self._report_row(connection, report_id)
            self._writable_channel_row(connection, str(report["channel_id"]))
            if int(report["revision"]) != expected_revision:
                raise CollaborationConflict("report revision compare-and-set failed")
            active = connection.execute(
                """
                SELECT * FROM collaboration_developer_leases
                WHERE report_id=? AND status='active'
                """,
                (report_id,),
            ).fetchone()
            if active is not None:
                raise CollaborationConflict("report already has an active developer lease")
            operation_now = max(now, _as_utc(str(report["updated_at"])))
            now_iso = operation_now.isoformat()
            expires_at = (operation_now + timedelta(seconds=ttl_seconds)).isoformat()
            if (
                next_report_status is None
                and str(report["category"]) == "environment_gap"
                and str(report["status"]) == "environment_failed"
            ):
                next_report_status = "routed_to_task_author"
            lease_report_revision = expected_revision
            if next_report_status is not None:
                lease_report_revision += 1
                connection.execute(
                    """
                    UPDATE collaboration_reports
                    SET status=?,revision=?,updated_at=?
                    WHERE report_id=? AND revision=?
                    """,
                    (
                        next_report_status,
                        lease_report_revision,
                        now_iso,
                        report_id,
                        expected_revision,
                    ),
                )
                persisted = self._report_row(connection, report_id)
                self._insert_report_revision_conn(
                    connection,
                    report=persisted,
                    actor_role="codex",
                    actor_id=owner_id,
                    idempotency_key=f"lease-acquire:{idempotency_key}",
                    request_digest=_idempotency_digest(
                        {"lease_id": lease_id, "status": next_report_status}
                    ),
                    message_id=None,
                    created_at=now_iso,
                )
            try:
                connection.execute(
                    """
                    INSERT INTO collaboration_developer_leases(
                      lease_id,channel_id,report_id,owner_id,report_revision,status,
                      revision,idempotency_key,request_digest,acquired_at,renewed_at,expires_at
                    ) VALUES(?,?,?,?,?,'active',1,?,?,?,?,?)
                    """,
                    (
                        lease_id,
                        report["channel_id"],
                        report_id,
                        owner_id,
                        lease_report_revision,
                        idempotency_key,
                        request_digest,
                        now_iso,
                        now_iso,
                        expires_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CollaborationConflict("developer lease identity conflicts") from error
            response = self._decode_lease(self._lease_row(connection, lease_id))
            return self._insert_operation_receipt_conn(
                connection,
                operation="lease.acquire",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response_kind="lease",
                response=response,
                created_at=now_iso,
            )

    async def get_active_lease(
        self, report_id: str | UUID, *, now: datetime | str | None = None
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._get_active_lease_sync,
            str(report_id),
            _as_utc(now, default=_now()),
        )

    def _get_active_lease_sync(self, report_id: str, now: datetime) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._report_row(connection, report_id)
            self._expire_developer_leases_conn(
                connection, now, fallback_report_status="approved_for_codex"
            )
            row = connection.execute(
                """
                SELECT * FROM collaboration_developer_leases
                WHERE report_id=? AND status='active'
                """,
                (report_id,),
            ).fetchone()
        if row is None:
            return None
        return self._decode_lease(row)

    async def renew_developer_lease(
        self,
        lease_id: str | UUID,
        owner_id: str,
        expected_revision: int,
        idempotency_key: str,
        ttl_seconds: int = 900,
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        effective_now = _as_utc(now, default=_now())
        async with self._lock:
            result = await asyncio.to_thread(
                self._renew_developer_lease_sync,
                str(lease_id),
                owner_id,
                expected_revision,
                idempotency_key,
                ttl_seconds,
                effective_now,
            )
        self._secure_database_files()
        return result

    def _renew_developer_lease_sync(
        self,
        lease_id: str,
        owner_id: str,
        expected_revision: int,
        idempotency_key: str,
        ttl_seconds: int,
        now: datetime,
    ) -> dict[str, Any]:
        if not 1 <= ttl_seconds <= 900:
            raise ValueError("lease ttl_seconds must be between 1 and 900")
        semantic = {
            "expected_revision": expected_revision,
            "ttl_seconds": ttl_seconds,
        }
        request_digest = _idempotency_digest(semantic)
        now_iso = now.isoformat()
        expires_at = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = self._lease_reference(connection, lease_id)
            lease_id = str(lease["lease_id"])
            report_id = str(lease["report_id"])
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="lease.renew",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_lease_operations
                WHERE lease_id=? AND operation='renew' AND owner_id=? AND idempotency_key=?
                """,
                (lease_id, owner_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "lease renewal idempotency key was reused with another payload"
                    )
                return self._decode_lease(self._lease_row(connection, lease_id))
            self._writable_channel_row(connection, str(lease["channel_id"]))
            if str(lease["owner_id"]) != owner_id:
                raise CollaborationUnauthorized("only the lease owner may renew it")
            if str(lease["status"]) != "active" or _as_utc(str(lease["expires_at"])) <= now:
                raise CollaborationConflict("developer lease is not active")
            if int(lease["revision"]) != expected_revision:
                raise CollaborationConflict("lease revision compare-and-set failed")
            next_revision = expected_revision + 1
            connection.execute(
                """
                UPDATE collaboration_developer_leases
                SET revision=?,renewed_at=?,expires_at=?
                WHERE lease_id=? AND status='active' AND revision=?
                """,
                (next_revision, now_iso, expires_at, lease_id, expected_revision),
            )
            connection.execute(
                """
                INSERT INTO collaboration_lease_operations(
                  lease_id,operation,owner_id,idempotency_key,request_digest,
                  resulting_revision,created_at
                ) VALUES(?,'renew',?,?,?,?,?)
                """,
                (
                    lease_id,
                    owner_id,
                    idempotency_key,
                    request_digest,
                    next_revision,
                    now_iso,
                ),
            )
            response = self._decode_lease(self._lease_row(connection, lease_id))
            return self._insert_operation_receipt_conn(
                connection,
                operation="lease.renew",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response_kind="lease",
                response=response,
                created_at=now_iso,
            )

    def _restore_report_after_lease_release_conn(
        self,
        connection: sqlite3.Connection,
        lease: sqlite3.Row,
        *,
        owner_id: str,
        idempotency_key: str,
        now: str,
    ) -> None:
        report = self._report_row(connection, str(lease["report_id"]))
        if not (
            str(report["category"]) in {"platform_capability_gap", "platform_defect_suspected"}
            and str(report["status"]) == "implementing"
            and int(report["revision"]) == int(lease["report_revision"])
        ):
            return
        next_report_revision = int(report["revision"]) + 1
        connection.execute(
            """
            UPDATE collaboration_reports
            SET status='approved_for_codex',revision=?,updated_at=?
            WHERE report_id=? AND revision=?
            """,
            (
                next_report_revision,
                now,
                report["report_id"],
                report["revision"],
            ),
        )
        persisted_report = self._report_row(connection, str(report["report_id"]))
        self._insert_report_revision_conn(
            connection,
            report=persisted_report,
            actor_role="codex",
            actor_id=owner_id,
            idempotency_key=f"lease-release-report:{idempotency_key}",
            request_digest=_idempotency_digest(
                {"lease_id": lease["lease_id"], "status": "approved_for_codex"}
            ),
            message_id=None,
            created_at=now,
        )

    async def release_developer_lease(
        self,
        lease_id: str | UUID,
        owner_id: str,
        expected_revision: int,
        idempotency_key: str,
        *,
        reason: str = "released by owner",
        now: datetime | str | None = None,
    ) -> dict[str, Any]:
        effective_now = _as_utc(now, default=_now())
        async with self._lock:
            result = await asyncio.to_thread(
                self._release_developer_lease_sync,
                str(lease_id),
                owner_id,
                expected_revision,
                idempotency_key,
                reason,
                effective_now,
            )
        self._secure_database_files()
        return result

    def _release_developer_lease_sync(
        self,
        lease_id: str,
        owner_id: str,
        expected_revision: int,
        idempotency_key: str,
        reason: str,
        now: datetime,
    ) -> dict[str, Any]:
        reason = str(self._safe_payload(reason))
        semantic = {"expected_revision": expected_revision, "reason": reason}
        request_digest = _idempotency_digest(semantic)
        now_iso = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = self._lease_reference(connection, lease_id)
            lease_id = str(lease["lease_id"])
            report_id = str(lease["report_id"])
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="lease.release",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_lease_operations
                WHERE lease_id=? AND operation='release'
                  AND owner_id=? AND idempotency_key=?
                """,
                (lease_id, owner_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "lease release idempotency key was reused with another payload"
                    )
                return self._decode_lease(self._lease_row(connection, lease_id))
            self._writable_channel_row(connection, str(lease["channel_id"]))
            if str(lease["owner_id"]) != owner_id:
                raise CollaborationUnauthorized("only the lease owner may release it")
            if int(lease["revision"]) != expected_revision:
                raise CollaborationConflict("lease revision compare-and-set failed")
            if str(lease["status"]) != "active" or _as_utc(str(lease["expires_at"])) <= now:
                raise CollaborationConflict("developer lease is not active")
            next_revision = expected_revision + 1
            connection.execute(
                """
                UPDATE collaboration_developer_leases
                SET status='released',revision=?,renewed_at=?,released_at=?
                WHERE lease_id=? AND status='active' AND revision=?
                """,
                (next_revision, now_iso, now_iso, lease_id, expected_revision),
            )
            connection.execute(
                """
                INSERT INTO collaboration_lease_operations(
                  lease_id,operation,owner_id,idempotency_key,request_digest,
                  resulting_revision,created_at
                ) VALUES(?,'release',?,?,?,?,?)
                """,
                (
                    lease_id,
                    owner_id,
                    idempotency_key,
                    request_digest,
                    next_revision,
                    now_iso,
                ),
            )
            self._restore_report_after_lease_release_conn(
                connection,
                lease,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
                now=now_iso,
            )
            self._enqueue_outbox_conn(
                connection,
                {
                    "channel_id": str(lease["channel_id"]),
                    "message_id": None,
                    "destination": "developer_inbox",
                    "idempotency_key": (f"inbox:lease-release:{lease_id}:{idempotency_key}"),
                    "payload": {"report_id": report_id},
                },
            )
            response = self._decode_lease(self._lease_row(connection, lease_id))
            return self._insert_operation_receipt_conn(
                connection,
                operation="lease.release",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=request_digest,
                response_kind="lease",
                response=response,
                created_at=now_iso,
            )

    async def expire_developer_leases(
        self,
        now: datetime | str | None = None,
        fallback_report_status: str | Enum = "approved_for_codex",
    ) -> int:
        effective_now = _as_utc(now, default=_now())
        normalized_status = str(
            fallback_report_status.value
            if isinstance(fallback_report_status, Enum)
            else fallback_report_status
        )
        async with self._lock:
            result = await asyncio.to_thread(
                self._expire_developer_leases_sync,
                effective_now,
                normalized_status,
            )
        self._secure_database_files()
        return result

    def _expire_developer_leases_sync(self, now: datetime, fallback_report_status: str) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._expire_developer_leases_conn(connection, now, fallback_report_status)

    async def record_developer_response(
        self,
        record: Mapping[str, Any] | Any,
        next_report_status: str | Enum,
        message: Mapping[str, Any] | Any,
        outbox: Mapping[str, Any] | Any | None = None,
    ) -> dict[str, Any]:
        data = _record(record)
        message_data = _record(message)
        outbox_data = _record(outbox) if outbox is not None else None
        async with self._lock:
            result = await asyncio.to_thread(
                self._record_developer_response_sync,
                data,
                str(
                    next_report_status.value
                    if isinstance(next_report_status, Enum)
                    else next_report_status
                ),
                message_data,
                outbox_data,
            )
        self._secure_database_files()
        return result

    @staticmethod
    def _decode_typed_payload(row: sqlite3.Row | Mapping[str, Any], model: Any) -> dict[str, Any]:
        data = dict(row)
        payload = json.loads(str(data["payload_json"]))
        if not isinstance(payload, dict):
            raise CollaborationStorageError("persisted domain payload must be an object")
        for key in _DOMAIN_TRANSPORT_FIELDS:
            payload.pop(key, None)
        payload["schema_version"] = str(payload.get("schema_version", "1.0"))
        payload["created_at"] = str(data["created_at"])
        return _validated_projection(model, payload)

    def _safe_domain_payload(self, data: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._safe_payload(data.get("payload", data))
        if not isinstance(payload, dict):
            raise ValueError("collaboration domain payload must be an object")
        return {key: value for key, value in payload.items() if key not in _DOMAIN_TRANSPORT_FIELDS}

    def _record_developer_response_sync(
        self,
        data: dict[str, Any],
        next_report_status: str,
        message: dict[str, Any],
        outbox: dict[str, Any] | None,
    ) -> dict[str, Any]:
        response_id = str(_required(data, "response_id"))
        channel_id = str(_required(data, "channel_id"))
        report_id = str(_required(data, "report_id"))
        lease_id = str(_required(data, "lease_id"))
        owner_id = str(data.get("owner_id", data.get("lease_owner_id", "")))
        if not owner_id:
            raise ValueError("lease owner is required")
        expected_revision = int(
            data.get("expected_report_revision", data.get("report_revision", 0))
        )
        idempotency_key = str(_required(data, "idempotency_key"))
        outcome = str(_required(data, "outcome"))
        payload = self._safe_domain_payload(data)
        semantic = {
            "response_id": response_id,
            "channel_id": channel_id,
            "report_id": report_id,
            "lease_id": lease_id,
            "owner_id": owner_id,
            "expected_report_revision": expected_revision,
            "outcome": outcome,
            "payload": payload,
            "next_report_status": next_report_status,
            "message": message,
            "outbox": outbox,
        }
        request_digest = _idempotency_digest(semantic)
        operation_digest = _validated_request_digest(
            message.get("client_request_digest"), fallback=request_digest
        )
        now = _now()
        now_iso = now.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="developer.response",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_developer_responses
                WHERE report_id=? AND owner_id=? AND idempotency_key=?
                """,
                (report_id, owner_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "developer response idempotency key was reused with another payload"
                    )
                return self._decode_typed_payload(replay, DeveloperResponse)
            report = self._report_row(connection, report_id)
            self._writable_channel_row(connection, channel_id)
            if str(report["channel_id"]) != channel_id:
                raise CollaborationConflict("developer response report belongs to another channel")
            if int(report["revision"]) != expected_revision:
                raise CollaborationConflict("report revision compare-and-set failed")
            lease = self._lease_row(connection, lease_id)
            if (
                str(lease["report_id"]) != report_id
                or str(lease["owner_id"]) != owner_id
                or str(lease["status"]) != "active"
            ):
                raise CollaborationUnauthorized(
                    "developer response requires the report's active lease owner"
                )
            if int(lease["report_revision"]) != expected_revision:
                raise CollaborationConflict("developer lease is bound to another report revision")
            if _as_utc(str(lease["expires_at"])) <= now:
                raise CollaborationConflict("developer lease has expired")
            message_row = self._append_message_conn(connection, message)
            if str(message_row["message_type"]) != "developer_response":
                raise CollaborationConflict("developer response message has the wrong type")
            resulting_revision = expected_revision + 1
            next_route = str(
                data.get("next_report_route")
                or (
                    "capability_approval"
                    if next_report_status == "evidence_collecting"
                    else report["route"]
                )
            )
            next_visibility = str(data.get("next_visibility") or "user_and_lilies")
            connection.execute(
                """
                UPDATE collaboration_reports
                SET status=?,route=?,visibility=?,revision=?,updated_at=?
                WHERE report_id=? AND revision=?
                """,
                (
                    next_report_status,
                    next_route,
                    next_visibility,
                    resulting_revision,
                    now_iso,
                    report_id,
                    expected_revision,
                ),
            )
            persisted = self._report_row(connection, report_id)
            self._insert_report_revision_conn(
                connection,
                report=persisted,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=_idempotency_digest(
                    {"response_id": response_id, "status": next_report_status}
                ),
                message_id=str(message_row["message_id"]),
                created_at=now_iso,
            )
            connection.execute(
                """
                INSERT INTO collaboration_developer_responses(
                  response_id,channel_id,report_id,lease_id,owner_id,
                  expected_report_revision,resulting_report_revision,outcome,
                  idempotency_key,request_digest,payload_json,message_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    response_id,
                    channel_id,
                    report_id,
                    lease_id,
                    owner_id,
                    expected_revision,
                    resulting_revision,
                    outcome,
                    idempotency_key,
                    request_digest,
                    _canonical_json(payload),
                    message_row["message_id"],
                    now_iso,
                ),
            )
            connection.execute(
                """
                UPDATE collaboration_developer_leases
                SET status='released',revision=revision+1,renewed_at=?,released_at=?
                WHERE lease_id=? AND status='active'
                """,
                (now_iso, now_iso, lease_id),
            )
            if outbox is not None:
                self._enqueue_outbox_conn(connection, outbox)
            row = connection.execute(
                "SELECT * FROM collaboration_developer_responses WHERE response_id=?",
                (response_id,),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise CollaborationStorageError("developer response insert did not persist")
            response = self._decode_typed_payload(row, DeveloperResponse)
            return self._insert_operation_receipt_conn(
                connection,
                operation="developer.response",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
                response_kind="developer_response",
                response=response,
                created_at=now_iso,
            )

    async def get_latest_developer_response(self, report_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_latest_developer_response_sync, str(report_id))

    def _get_latest_developer_response_sync(self, report_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            self._report_row(connection, report_id)
            row = connection.execute(
                """
                SELECT * FROM collaboration_developer_responses
                WHERE report_id=? ORDER BY created_at DESC,response_id DESC LIMIT 1
                """,
                (report_id,),
            ).fetchone()
        if row is None:
            raise CollaborationNotFound("developer response not found")
        return self._decode_typed_payload(row, DeveloperResponse)

    def _get_typed_domain_record_sync(
        self,
        *,
        table: str,
        id_column: str,
        row_id: str,
        model: Any,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {id_column}=?", (row_id,)
            ).fetchone()
        if row is None:
            raise CollaborationNotFound("collaboration domain response not found")
        return self._decode_typed_payload(row, model)

    async def get_developer_response(self, response_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_typed_domain_record_sync,
            table="collaboration_developer_responses",
            id_column="response_id",
            row_id=str(response_id),
            model=DeveloperResponse,
        )

    async def get_task_amendment(self, amendment_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_typed_domain_record_sync,
            table="collaboration_task_amendments",
            id_column="amendment_id",
            row_id=str(amendment_id),
            model=TaskPackageAmendment,
        )

    async def get_environment_response(self, response_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_typed_domain_record_sync,
            table="collaboration_environment_responses",
            id_column="response_id",
            row_id=str(response_id),
            model=EnvironmentResponse,
        )

    async def get_reprobe(self, reprobe_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_typed_domain_record_sync,
            table="collaboration_reprobes",
            id_column="reprobe_id",
            row_id=str(reprobe_id),
            model=LiliesReprobeResult,
        )

    def _record_related_domain_conn(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        row_id: str,
        data: dict[str, Any],
        message: dict[str, Any],
        actor_role: str,
        next_report_status: str,
        extra_columns: Sequence[str],
        extra_values: Sequence[Any],
        model: Any,
    ) -> dict[str, Any]:
        channel_id = str(_required(data, "channel_id"))
        report_id = str(_required(data, "report_id"))
        expected_revision = int(
            data.get("expected_report_revision", data.get("report_revision", 0))
        )
        if expected_revision < 1:
            raise ValueError("expected report revision is required")
        idempotency_key = str(_required(data, "idempotency_key"))
        payload = self._safe_domain_payload(data)
        request_digest = _idempotency_digest(
            {
                "record": data,
                "message": message,
                "next_report_status": next_report_status,
            }
        )
        operation = {
            "collaboration_task_amendments": "task.amendment",
            "collaboration_environment_responses": "environment.response",
            "collaboration_reprobes": "lilies.reprobe",
        }[table]
        operation_digest = _validated_request_digest(
            message.get("client_request_digest"), fallback=request_digest
        )
        actor_id = str(_required(message, "sender_id"))
        operation_replay = self._get_operation_receipt_conn(
            connection,
            operation=operation,
            scope_id=report_id,
            actor_role=actor_role,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_digest=operation_digest,
        )
        if operation_replay is not None:
            return operation_replay
        replay = connection.execute(
            f"SELECT * FROM {table} WHERE report_id=? AND idempotency_key=?",
            (report_id, idempotency_key),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                raise CollaborationConflict(
                    "domain response idempotency key was reused with another payload"
                )
            return self._decode_typed_payload(replay, model)
        report = self._report_row(connection, report_id)
        self._writable_channel_row(connection, channel_id)
        if str(report["channel_id"]) != channel_id:
            raise CollaborationConflict("domain response report belongs to another channel")
        if int(report["revision"]) != expected_revision:
            raise CollaborationConflict("report revision compare-and-set failed")
        now = _now()
        now_iso = now.isoformat()
        lease_id: str | None = None
        lease_owner_id: str | None = None
        if table in {
            "collaboration_task_amendments",
            "collaboration_environment_responses",
        }:
            lease_id = str(_required(data, "lease_id"))
            lease_owner_id = str(_required(data, "lease_owner_id"))
            lease = self._lease_row(connection, lease_id)
            if (
                str(lease["channel_id"]) != channel_id
                or str(lease["report_id"]) != report_id
                or str(lease["owner_id"]) != lease_owner_id
                or str(lease["status"]) != "active"
            ):
                raise CollaborationUnauthorized(
                    "domain response requires the report's active lease owner"
                )
            if int(lease["report_revision"]) != expected_revision:
                raise CollaborationConflict("developer lease is bound to another report revision")
            if _as_utc(str(lease["expires_at"])) <= now:
                raise CollaborationConflict("developer lease has expired")
        message_row = self._append_message_conn(connection, message)
        resulting_revision = expected_revision + 1
        connection.execute(
            """
            UPDATE collaboration_reports SET status=?,revision=?,updated_at=?
            WHERE report_id=? AND revision=?
            """,
            (next_report_status, resulting_revision, now_iso, report_id, expected_revision),
        )
        persisted = self._report_row(connection, report_id)
        self._insert_report_revision_conn(
            connection,
            report=persisted,
            actor_role=actor_role,
            actor_id=str(message_row["sender_id"]),
            idempotency_key=idempotency_key,
            request_digest=_idempotency_digest({"record_id": row_id, "status": next_report_status}),
            message_id=str(message_row["message_id"]),
            created_at=now_iso,
        )
        columns = [
            id_column,
            "channel_id",
            "report_id",
            *extra_columns,
            "idempotency_key",
            "request_digest",
            "payload_json",
            "message_id",
            "created_at",
        ]
        values = [
            row_id,
            channel_id,
            report_id,
            *extra_values,
            idempotency_key,
            request_digest,
            _canonical_json(payload),
            message_row["message_id"],
            now_iso,
        ]
        connection.execute(
            f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in values)})",
            values,
        )
        if lease_id is not None and lease_owner_id is not None:
            changed = connection.execute(
                """
                UPDATE collaboration_developer_leases
                SET status='released',revision=revision+1,renewed_at=?,released_at=?
                WHERE lease_id=? AND report_id=? AND owner_id=? AND status='active'
                """,
                (
                    now_iso,
                    now_iso,
                    lease_id,
                    report_id,
                    lease_owner_id,
                ),
            ).rowcount
            if changed != 1:
                raise CollaborationConflict("developer lease changed before domain response commit")
        if next_report_status in {
            "approved_for_codex",
            "verification_failed",
            "routed_to_task_author",
            "environment_failed",
            "unresolved",
        }:
            inbox_key = (
                f"inbox:actionable:{operation}:{report_id}:{resulting_revision}:{idempotency_key}"
            )
            self._enqueue_outbox_conn(
                connection,
                {
                    "outbox_id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"lilies:collaboration:{inbox_key}",
                        )
                    ),
                    "channel_id": channel_id,
                    "message_id": str(message_row["message_id"]),
                    "destination": "developer_inbox",
                    "idempotency_key": inbox_key,
                    "payload": {
                        "kind": "report",
                        "report_id": report_id,
                        "status": next_report_status,
                        "revision": resulting_revision,
                    },
                },
            )
        row = connection.execute(f"SELECT * FROM {table} WHERE {id_column}=?", (row_id,)).fetchone()
        if row is None:  # pragma: no cover
            raise CollaborationStorageError("domain response insert did not persist")
        response = self._decode_typed_payload(row, model)
        return self._insert_operation_receipt_conn(
            connection,
            operation=operation,
            scope_id=report_id,
            actor_role=actor_role,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            request_digest=operation_digest,
            response_kind=operation,
            response=response,
            created_at=now_iso,
        )

    async def record_task_amendment(
        self,
        record: Mapping[str, Any] | Any,
        message: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        data = _record(record)
        message_data = _record(message)
        async with self._lock:
            result = await asyncio.to_thread(self._record_task_amendment_sync, data, message_data)
        self._secure_database_files()
        return result

    def _record_task_amendment_sync(
        self, data: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        outcome = str(_required(data, "outcome"))
        next_status = str(
            data.get("next_report_status")
            or ("task_package_amended" if outcome == "amended" else "rejected_with_evidence")
        )
        prior_revision = int(data.get("prior_task_revision", data.get("previous_task_revision", 0)))
        task_revision = data.get("task_revision", data.get("new_task_revision"))
        persisted_revision = int(task_revision or prior_revision)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._record_related_domain_conn(
                connection,
                table="collaboration_task_amendments",
                id_column="amendment_id",
                row_id=str(_required(data, "amendment_id")),
                data=data,
                message=message,
                actor_role="task_author",
                next_report_status=next_status,
                extra_columns=(
                    "task_id",
                    "prior_task_revision",
                    "task_revision",
                ),
                extra_values=(
                    str(_required(data, "task_id")),
                    prior_revision,
                    persisted_revision,
                ),
                model=TaskPackageAmendment,
            )

    async def record_environment_response(
        self,
        record: Mapping[str, Any] | Any,
        message: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        data = _record(record)
        message_data = _record(message)
        async with self._lock:
            result = await asyncio.to_thread(
                self._record_environment_response_sync, data, message_data
            )
        self._secure_database_files()
        return result

    def _record_environment_response_sync(
        self, data: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        outcome = str(_required(data, "outcome"))
        next_status = str(
            data.get("next_report_status")
            or ("environment_restored" if outcome == "restored" else "unresolved")
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._record_related_domain_conn(
                connection,
                table="collaboration_environment_responses",
                id_column="response_id",
                row_id=str(_required(data, "response_id")),
                data=data,
                message=message,
                actor_role="task_author",
                next_report_status=next_status,
                extra_columns=("outcome",),
                extra_values=(outcome,),
                model=EnvironmentResponse,
            )

    async def record_reprobe(
        self,
        record: Mapping[str, Any] | Any,
        message: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        data = _record(record)
        message_data = _record(message)
        async with self._lock:
            result = await asyncio.to_thread(self._record_reprobe_sync, data, message_data)
        self._secure_database_files()
        return result

    def _record_reprobe_sync(self, data: dict[str, Any], message: dict[str, Any]) -> dict[str, Any]:
        outcome = str(_required(data, "outcome"))
        if outcome not in {"lilies_verified", "verification_failed"}:
            raise ValueError("unsupported Lilies reprobe outcome")
        contract_digest = data.get("observed_contract_digest", data.get("contract_digest"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._record_related_domain_conn(
                connection,
                table="collaboration_reprobes",
                id_column="reprobe_id",
                row_id=str(_required(data, "reprobe_id")),
                data=data,
                message=message,
                actor_role="lilies",
                next_report_status=str(data.get("next_report_status") or outcome),
                extra_columns=("outcome", "observed_contract_digest"),
                extra_values=(outcome, contract_digest),
                model=LiliesReprobeResult,
            )

    @staticmethod
    def _claim_row(connection: sqlite3.Connection, claim_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM collaboration_verification_claims WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        if row is None:
            raise CollaborationNotFound("verification claim not found")
        return row

    @staticmethod
    def _decode_claim(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        payload = json.loads(str(data["payload_json"]))
        if not isinstance(payload, dict):
            raise CollaborationStorageError("persisted claim payload must be an object")
        for transport_key in (
            "idempotency_key",
            "expected_channel_revision",
            "payload",
            "message",
            "outbox",
        ):
            payload.pop(transport_key, None)
        payload.update(
            {
                "schema_version": str(payload.get("schema_version", "1.0")),
                "claim_id": str(data["claim_id"]),
                "channel_id": str(data["channel_id"]),
                "assignment_id": str(data["assignment_id"]),
                "application_id": str(data["application_id"]),
                "claim_revision": int(data["claim_revision"]),
                "draft_revision": int(data["draft_revision"]),
                "content_hash": str(data["content_hash"]),
                "published_version": data["published_version"],
                "status": str(data["status"]),
                "created_at": str(data["frozen_at"]),
                "invalidated_at": data["invalidated_at"],
                "invalidation_reason": data["invalidation_reason"],
            }
        )
        return _validated_projection(VerificationClaim, payload)

    @staticmethod
    def _verification_projection(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        data = dict(row)
        payload = json.loads(str(data["payload_json"]))
        if not isinstance(payload, dict):
            raise CollaborationStorageError("persisted verification payload must be an object")
        for transport_key in (
            "idempotency_key",
            "expected_claim_revision",
            "payload",
            "message",
            "next_claim_status",
        ):
            payload.pop(transport_key, None)
        payload.update(
            {
                "schema_version": str(payload.get("schema_version", "1.0")),
                "verification_id": str(data["verification_id"]),
                "channel_id": str(data["channel_id"]),
                "claim_id": str(data["claim_id"]),
                "claim_revision": int(data["claim_revision"]),
                "verifier_id": str(data["verifier_id"]),
                "oracle_digest": str(data["oracle_digest"]),
                "verdict": str(data["verdict"]),
                "created_at": str(data["created_at"]),
            }
        )
        return _validated_projection(VerificationResult, payload)

    def _append_claim_invalidation_message_conn(
        self,
        connection: sqlite3.Connection,
        claim: sqlite3.Row | Mapping[str, Any],
        *,
        reason: str,
        now: str,
        cause_key: str,
    ) -> dict[str, Any]:
        claim_id = str(claim["claim_id"])
        channel_id = str(claim["channel_id"])
        control_id = uuid5(
            NAMESPACE_URL, f"lilies:claim-invalidation-control:{claim_id}:{cause_key}"
        )
        message_id = uuid5(
            NAMESPACE_URL, f"lilies:claim-invalidation-message:{claim_id}:{cause_key}"
        )
        latest_verification = connection.execute(
            """
            SELECT message_id FROM collaboration_verifications
            WHERE claim_id=? ORDER BY created_at DESC,verification_id DESC LIMIT 1
            """,
            (claim_id,),
        ).fetchone()
        causal_parent_id = (
            str(latest_verification["message_id"])
            if latest_verification is not None
            else str(claim["message_id"])
        )
        return self._append_message_conn(
            connection,
            {
                "message_id": str(message_id),
                "channel_id": channel_id,
                "message_type": "control",
                "sender_role": "platform",
                "sender_id": "workflow-draft-hook",
                "correlation_id": claim_id,
                "causal_parent_id": causal_parent_id,
                "idempotency_key": f"claim-invalidated:{claim_id}:{cause_key}",
                "visibility": "user_and_lilies",
                "payload_schema": "collaboration.control.v1",
                "payload": {
                    "schema_version": "1.0",
                    "control_id": str(control_id),
                    "channel_id": channel_id,
                    "kind": "claim_invalidated",
                    "actor_id": "workflow-draft-hook",
                    "reason": reason,
                    "claim_id": claim_id,
                    "created_at": now,
                },
                "evidence_refs": [],
            },
            allow_closed_claim_invalidation=True,
        )

    @staticmethod
    def _assert_current_draft_matches_claim_conn(
        connection: sqlite3.Connection,
        *,
        application_id: str,
        draft_revision: int,
        content_hash: str,
    ) -> None:
        managed = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type='table' AND name='application_drafts'
            """
        ).fetchone()
        if managed is None:
            # Storage-only tests and standalone collaboration deployments do
            # not necessarily host workflow state in this database.
            return
        current = connection.execute(
            """
            SELECT revision,content_hash FROM application_drafts
            WHERE application_id=?
            """,
            (application_id,),
        ).fetchone()
        if (
            current is None
            or int(current["revision"]) != draft_revision
            or not hmac.compare_digest(
                _canonical_content_hash(current["content_hash"]),
                _canonical_content_hash(content_hash),
            )
        ):
            raise CollaborationConflict(
                "verification claim does not match the current application draft"
            )

    @staticmethod
    def _assert_claim_report_accounting_conn(
        connection: sqlite3.Connection,
        *,
        channel_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        raw_report_ids = payload.get("resolved_report_ids", [])
        if not isinstance(raw_report_ids, list):
            raise CollaborationConflict("claim resolved_report_ids must be a list")
        submitted_report_ids = [str(item) for item in raw_report_ids]
        if len(submitted_report_ids) != len(set(submitted_report_ids)):
            raise CollaborationConflict("claim resolved_report_ids must be unique")
        reports = connection.execute(
            """
            SELECT report_id,category,status FROM collaboration_reports
            WHERE channel_id=? ORDER BY report_id
            """,
            (channel_id,),
        ).fetchall()
        persisted_report_ids = {str(report["report_id"]) for report in reports}
        if set(submitted_report_ids) != persisted_report_ids:
            raise CollaborationConflict(
                "claim must account for every report in its collaboration channel"
            )
        for report in reports:
            category = str(report["category"])
            allowed = _CLAIM_RESOLVED_REPORT_STATUSES.get(category, frozenset())
            if str(report["status"]) not in allowed:
                raise CollaborationConflict(
                    "claim cannot resolve a report that has not reached an allowed "
                    f"terminal or Lilies-verified status: {report['report_id']}"
                )

    async def create_verification_claim(
        self,
        record: Mapping[str, Any] | Any,
        message: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        data = _record(record)
        message_data = _record(message)
        async with self._lock:
            result = await asyncio.to_thread(
                self._create_verification_claim_sync, data, message_data
            )
        self._secure_database_files()
        return result

    def _create_verification_claim_sync(
        self, data: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        claim_id = str(_required(data, "claim_id"))
        channel_id = str(_required(data, "channel_id"))
        assignment_id = str(_required(data, "assignment_id"))
        application_id = str(_required(data, "application_id"))
        draft_revision = int(_required(data, "draft_revision"))
        content_hash = str(_required(data, "content_hash"))
        published_version = data.get("published_version")
        status = str(data.get("status", "frozen"))
        if status != "frozen":
            raise CollaborationConflict("new verification claim must be frozen")
        idempotency_key = str(_required(data, "idempotency_key"))
        expected_channel_revision = int(
            data.get("expected_channel_revision", data.get("channel_revision", 0))
        )
        if expected_channel_revision < 1:
            raise ValueError("expected_channel_revision is required")
        payload = self._safe_payload(data.get("payload", data))
        if isinstance(payload, dict):
            payload.pop("outbox", None)
        outbox = data.get("outbox")
        semantic = {
            "claim_id": claim_id,
            "channel_id": channel_id,
            "assignment_id": assignment_id,
            "application_id": application_id,
            "draft_revision": draft_revision,
            "content_hash": content_hash,
            "published_version": published_version,
            "status": status,
            "expected_channel_revision": expected_channel_revision,
            "payload": payload,
            "message": message,
            "outbox": outbox,
        }
        request_digest = _idempotency_digest(semantic)
        operation_digest = _validated_request_digest(
            message.get("client_request_digest"), fallback=request_digest
        )
        actor_role = str(_required(message, "sender_role"))
        actor_id = str(_required(message, "sender_id"))
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="verification.claim",
                scope_id=channel_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                """
                SELECT * FROM collaboration_verification_claims
                WHERE channel_id=? AND assignment_id=? AND idempotency_key=?
                """,
                (channel_id, assignment_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "claim idempotency key was reused with another payload"
                    )
                return self._decode_claim(replay)
            channel = self._writable_channel_row(connection, channel_id)
            if str(channel["assignment_id"]) != assignment_id:
                raise CollaborationConflict("verification claim belongs to another assignment")
            bound_application_ids = json.loads(str(channel["application_ids_json"]))
            if application_id not in bound_application_ids:
                raise CollaborationConflict(
                    "verification claim application is not bound to this channel"
                )
            if int(channel["revision"]) != expected_channel_revision:
                raise CollaborationConflict("channel revision compare-and-set failed")
            self._assert_current_draft_matches_claim_conn(
                connection,
                application_id=application_id,
                draft_revision=draft_revision,
                content_hash=content_hash,
            )
            self._assert_claim_report_accounting_conn(
                connection,
                channel_id=channel_id,
                payload=payload,
            )
            active_claims = connection.execute(
                """
                SELECT * FROM collaboration_verification_claims
                WHERE channel_id=? AND assignment_id=?
                  AND status IN ('frozen','ready_for_independent_verification',
                                 'awaiting_independent_verification')
                ORDER BY frozen_at,claim_id
                """,
                (channel_id, assignment_id),
            ).fetchall()
            if any(
                int(item["draft_revision"]) == draft_revision
                and str(item["content_hash"]) == content_hash
                for item in active_claims
            ):
                raise CollaborationConflict(
                    "an active claim already freezes this draft revision and hash"
                )
            message_row = self._append_message_conn(connection, message)
            if str(message_row["message_type"]) != "verification_claim":
                raise CollaborationConflict("claim message has the wrong type")
            for prior in active_claims:
                reason = f"superseded by claim {claim_id} for a changed frozen draft"
                connection.execute(
                    """
                    UPDATE collaboration_verification_claims
                    SET status='invalidated',claim_revision=claim_revision+1,
                        invalidated_at=?,invalidation_reason=?,updated_at=?
                    WHERE claim_id=? AND claim_revision=?
                    """,
                    (now, reason, now, prior["claim_id"], prior["claim_revision"]),
                )
                cause_key = f"superseded:{claim_id}"
                self._append_claim_invalidation_message_conn(
                    connection,
                    prior,
                    reason=reason,
                    now=now,
                    cause_key=cause_key,
                )
                self._append_audit_conn(
                    connection,
                    {
                        "audit_id": f"claim-invalidated:{prior['claim_id']}:{claim_id}",
                        "channel_id": channel_id,
                        "entity_kind": "verification_claim",
                        "entity_id": prior["claim_id"],
                        "event_type": "claim_invalidated",
                        "actor_role": "platform",
                        "actor_id": "workflow-draft-hook",
                        "idempotency_key": cause_key,
                        "details": {"reason": reason, "superseding_claim_id": claim_id},
                    },
                )
            try:
                connection.execute(
                    """
                    INSERT INTO collaboration_verification_claims(
                      claim_id,channel_id,assignment_id,application_id,draft_revision,
                      content_hash,published_version,status,claim_revision,idempotency_key,
                      request_digest,payload_json,message_id,frozen_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)
                    """,
                    (
                        claim_id,
                        channel_id,
                        assignment_id,
                        application_id,
                        draft_revision,
                        content_hash,
                        published_version,
                        status,
                        idempotency_key,
                        request_digest,
                        _canonical_json(payload),
                        message_row["message_id"],
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise CollaborationConflict("verification claim identity conflicts") from error
            changed = connection.execute(
                """
                UPDATE collaboration_channels SET revision=revision+1,updated_at=?
                WHERE channel_id=? AND revision=?
                """,
                (now, channel_id, expected_channel_revision),
            )
            if changed.rowcount != 1:
                raise CollaborationConflict("channel revision compare-and-set failed")
            if outbox is not None:
                if not isinstance(outbox, Mapping):
                    raise ValueError("claim outbox must be a mapping")
                self._enqueue_outbox_conn(connection, outbox)
            response = self._decode_claim(self._claim_row(connection, claim_id))
            return self._insert_operation_receipt_conn(
                connection,
                operation="verification.claim",
                scope_id=channel_id,
                actor_role=actor_role,
                actor_id=actor_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
                response_kind="verification_claim",
                response=response,
                created_at=now,
            )

    async def get_claim(self, claim_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_claim_sync, str(claim_id))

    def _get_claim_sync(self, claim_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return self._decode_claim(self._claim_row(connection, claim_id))

    async def get_latest_claim(
        self,
        *,
        channel_id: str | UUID,
        assignment_id: str | UUID,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._get_latest_claim_sync,
            str(channel_id),
            str(assignment_id),
        )

    def _get_latest_claim_sync(
        self,
        channel_id: str,
        assignment_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT claim.*
                FROM collaboration_verification_claims AS claim
                JOIN collaboration_messages AS message
                  ON message.message_id=claim.message_id
                WHERE claim.channel_id=? AND claim.assignment_id=?
                ORDER BY message.seq DESC LIMIT 1
                """,
                (channel_id, assignment_id),
            ).fetchone()
        return self._decode_claim(row) if row is not None else None

    async def list_claims(
        self,
        *,
        channel_id: str | UUID | None = None,
        assignment_id: str | UUID | None = None,
        statuses: Sequence[str | Enum] | None = None,
        after: int = 0,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        if after < 0 or not 1 <= limit <= 5_000:
            raise ValueError("claim pagination is out of range")
        normalized = (
            tuple(str(item.value if isinstance(item, Enum) else item) for item in statuses)
            if statuses is not None
            else None
        )
        return await asyncio.to_thread(
            self._list_claims_sync,
            str(channel_id) if channel_id is not None else None,
            str(assignment_id) if assignment_id is not None else None,
            normalized,
            after,
            limit,
        )

    def _list_claims_sync(
        self,
        channel_id: str | None,
        assignment_id: str | None,
        statuses: tuple[str, ...] | None,
        after: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if channel_id is not None:
            clauses.append("channel_id=?")
            parameters.append(channel_id)
        if assignment_id is not None:
            clauses.append("assignment_id=?")
            parameters.append(assignment_id)
        if statuses is not None:
            if not statuses:
                return []
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            parameters.extend(statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, after))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM collaboration_verification_claims {where}
                ORDER BY frozen_at,claim_id LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._decode_claim(row) for row in rows]

    async def invalidate_verification_claims(
        self,
        *,
        application_id: str | UUID,
        current_draft_revision: int,
        current_content_hash: str,
        reason: str,
        assignment_id: str | UUID | None = None,
        now: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        effective_now = _as_utc(now, default=_now())
        async with self._lock:
            result = await asyncio.to_thread(
                self._invalidate_verification_claims_sync,
                str(application_id),
                str(assignment_id) if assignment_id is not None else None,
                current_draft_revision,
                current_content_hash,
                reason,
                effective_now,
            )
        self._secure_database_files()
        return result

    def _invalidate_verification_claims_sync(
        self,
        application_id: str,
        assignment_id: str | None,
        current_draft_revision: int,
        current_content_hash: str,
        reason: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self.invalidate_verification_claims_in_transaction(
                connection,
                application_id=application_id,
                assignment_id=assignment_id,
                current_draft_revision=current_draft_revision,
                current_content_hash=current_content_hash,
                reason=reason,
                now=now,
            )

    def invalidate_verification_claims_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        application_id: str | UUID,
        current_draft_revision: int,
        current_content_hash: str,
        reason: str,
        assignment_id: str | UUID | None = None,
        now: datetime | str | None = None,
    ) -> list[dict[str, Any]]:
        """Invalidate stale claims inside the caller's existing SQLite tx.

        Workflow draft persistence uses this narrow synchronous boundary so the
        new draft snapshot, claim state, control message, and audit fact either
        all commit or all roll back together.
        """

        if not connection.in_transaction:
            raise CollaborationStorageError(
                "claim invalidation requires an existing SQLite transaction"
            )
        application_id = str(application_id)
        assignment_id = str(assignment_id) if assignment_id is not None else None
        current_content_hash = _canonical_content_hash(current_content_hash)
        effective_now = _as_utc(now, default=_now())
        reason = str(self._safe_payload(reason))
        now_iso = effective_now.isoformat()
        assignment_clause = " AND claim.assignment_id=?" if assignment_id is not None else ""
        parameters: list[Any] = [
            application_id,
            current_draft_revision,
            current_content_hash,
        ]
        if assignment_id is not None:
            parameters.append(assignment_id)
        rows = connection.execute(
            f"""
            SELECT claim.* FROM collaboration_verification_claims AS claim
            JOIN collaboration_channels AS channel
              ON channel.channel_id=claim.channel_id
            WHERE claim.application_id=?
              AND (claim.draft_revision<>? OR claim.content_hash<>?)
              {assignment_clause}
              AND claim.status<>'invalidated'
              AND channel.status<>'archived'
            ORDER BY claim.frozen_at,claim.claim_id
            """,
            parameters,
        ).fetchall()
        invalidated: list[dict[str, Any]] = []
        for claim in rows:
            changed = connection.execute(
                """
                UPDATE collaboration_verification_claims
                SET status='invalidated',claim_revision=claim_revision+1,
                    invalidated_at=?,invalidation_reason=?,updated_at=?
                WHERE claim_id=? AND claim_revision=?
                """,
                (
                    now_iso,
                    reason,
                    now_iso,
                    claim["claim_id"],
                    claim["claim_revision"],
                ),
            )
            if changed.rowcount != 1:
                continue
            persisted = self._claim_row(connection, str(claim["claim_id"]))
            cause_key = str(
                uuid5(
                    NAMESPACE_URL,
                    "lilies:claim-invalidation-cause:"
                    f"{claim['claim_id']}:{claim['claim_revision']}:"
                    f"{current_draft_revision}:{current_content_hash}",
                )
            )
            self._append_claim_invalidation_message_conn(
                connection,
                claim,
                reason=reason,
                now=now_iso,
                cause_key=cause_key,
            )
            self._append_audit_conn(
                connection,
                {
                    "audit_id": f"claim-invalidated:{claim['claim_id']}:{claim['claim_revision']}",
                    "channel_id": claim["channel_id"],
                    "entity_kind": "verification_claim",
                    "entity_id": claim["claim_id"],
                    "event_type": "claim_invalidated",
                    "actor_role": "platform",
                    "actor_id": "workflow-draft-hook",
                    "idempotency_key": (
                        f"claim-invalidated:{claim['claim_revision']}:{current_content_hash}"
                    ),
                    "details": {
                        "reason": reason,
                        "current_draft_revision": current_draft_revision,
                        "current_content_hash": current_content_hash,
                    },
                },
            )
            invalidated.append(self._decode_claim(persisted))
        return invalidated

    async def record_verification(
        self,
        record: Mapping[str, Any] | Any,
        message: Mapping[str, Any] | Any,
    ) -> dict[str, Any]:
        data = _record(record)
        message_data = _record(message)
        async with self._lock:
            result = await asyncio.to_thread(self._record_verification_sync, data, message_data)
        self._secure_database_files()
        return result

    def _resolve_capability_reports_conn(
        self,
        connection: sqlite3.Connection,
        *,
        claim: sqlite3.Row,
        verification_id: str,
        verifier_id: str,
        verdict: str,
        message_id: str,
        now: str,
    ) -> None:
        if verdict not in {"independently_verified", "verification_failed"}:
            raise CollaborationConflict("unsupported independent verification verdict")
        claim_payload = json.loads(str(claim["payload_json"]))
        if not isinstance(claim_payload, dict):
            raise CollaborationStorageError("persisted claim payload must be an object")
        raw_report_ids = claim_payload.get("resolved_report_ids", [])
        if not isinstance(raw_report_ids, list):
            raise CollaborationStorageError("persisted claim resolved_report_ids must be a list")
        for report_id in sorted(str(item) for item in raw_report_ids):
            report = self._report_row(connection, report_id)
            if str(report["channel_id"]) != str(claim["channel_id"]):
                raise CollaborationConflict(
                    "verification claim references a report from another channel"
                )
            if str(report["category"]) not in _CAPABILITY_REPORT_CATEGORIES:
                continue
            # A later application claim may cite capability evidence that was
            # independently verified by an earlier claim.  That terminal fact
            # remains valid even if the later, broader application claim fails.
            if str(report["status"]) in {
                "independently_verified",
                "rejected",
                "withdrawn",
            }:
                continue
            if str(report["status"]) != "lilies_verified":
                raise CollaborationConflict("resolved capability report is not lilies_verified")
            previous_revision = int(report["revision"])
            next_revision = previous_revision + 1
            changed = connection.execute(
                """
                UPDATE collaboration_reports
                SET status=?,route='verifier',revision=?,updated_at=?
                WHERE report_id=? AND revision=? AND status='lilies_verified'
                """,
                (
                    verdict,
                    next_revision,
                    now,
                    report_id,
                    previous_revision,
                ),
            )
            if changed.rowcount != 1:
                raise CollaborationConflict("capability report verification compare-and-set failed")
            persisted = self._report_row(connection, report_id)
            # Validate the route/status state before exposing or recording it.
            self._decode_report(persisted)
            self._insert_report_revision_conn(
                connection,
                report=persisted,
                actor_role="verifier",
                actor_id=verifier_id,
                idempotency_key=(f"independent-verification:{verification_id}:{report_id}"),
                request_digest=_idempotency_digest(
                    {
                        "verification_id": verification_id,
                        "report_id": report_id,
                        "expected_report_revision": previous_revision,
                        "status": verdict,
                        "route": "verifier",
                    }
                ),
                message_id=message_id,
                created_at=now,
            )

    def _record_verification_sync(
        self, data: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        verification_id = str(_required(data, "verification_id"))
        channel_id = str(_required(data, "channel_id"))
        claim_id = str(_required(data, "claim_id"))
        verifier_id = str(_required(data, "verifier_id"))
        oracle_digest = str(_required(data, "oracle_digest"))
        verdict = str(_required(data, "verdict"))
        idempotency_key = str(_required(data, "idempotency_key"))
        expected_claim_revision = int(
            data.get("expected_claim_revision", data.get("claim_revision", 0))
        )
        if expected_claim_revision < 1:
            raise ValueError("expected_claim_revision is required")
        payload = self._safe_payload(data.get("payload", data))
        semantic = {
            "verification_id": verification_id,
            "channel_id": channel_id,
            "claim_id": claim_id,
            "verifier_id": verifier_id,
            "oracle_digest": oracle_digest,
            "verdict": verdict,
            "expected_claim_revision": expected_claim_revision,
            "payload": payload,
            "message": message,
        }
        request_digest = _idempotency_digest(semantic)
        operation_digest = _validated_request_digest(
            message.get("client_request_digest"), fallback=request_digest
        )
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            operation_replay = self._get_operation_receipt_conn(
                connection,
                operation="verification.result",
                scope_id=claim_id,
                actor_role="verifier",
                actor_id=verifier_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
            )
            if operation_replay is not None:
                return operation_replay
            replay = connection.execute(
                "SELECT * FROM collaboration_verifications WHERE claim_id=?",
                (claim_id,),
            ).fetchone()
            if replay is not None:
                if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                    raise CollaborationConflict(
                        "claim already has a different independent verification"
                    )
                return self._verification_projection(replay)
            claim = self._claim_row(connection, claim_id)
            channel = self._channel_row(connection, channel_id)
            if str(claim["channel_id"]) != channel_id:
                raise CollaborationConflict("verification claim belongs to another channel")
            if int(claim["claim_revision"]) != expected_claim_revision:
                raise CollaborationConflict("claim revision compare-and-set failed")
            if str(claim["status"]) not in _ACTIVE_CLAIM_STATUSES:
                raise CollaborationConflict("verification claim is not active")
            self._assert_current_draft_matches_claim_conn(
                connection,
                application_id=str(claim["application_id"]),
                draft_revision=int(claim["draft_revision"]),
                content_hash=str(claim["content_hash"]),
            )
            claim_payload = json.loads(str(claim["payload_json"]))
            if not isinstance(claim_payload, dict):
                raise CollaborationStorageError("persisted claim payload must be an object")
            self._assert_claim_report_accounting_conn(
                connection,
                channel_id=channel_id,
                payload=claim_payload,
            )
            if str(channel["status"]) == "archived":
                raise CollaborationChannelClosed("archived claim can no longer be verified")
            if str(channel["status"]) in _CLOSED_CHANNEL_STATUSES:
                if channel["closed_at"] is None or _as_utc(str(claim["frozen_at"])) > _as_utc(
                    str(channel["closed_at"])
                ):
                    raise CollaborationChannelClosed(
                        "post-close verification requires a claim frozen before close"
                    )
            if str(message.get("sender_id")) != verifier_id:
                raise CollaborationUnauthorized(
                    "verification message sender must match verifier identity"
                )
            if str(message.get("causal_parent_id") or "") != str(claim["message_id"]):
                raise CollaborationConflict(
                    "verification result must causally reference the frozen claim message"
                )
            message_row = self._append_message_conn(
                connection, message, allow_closed_verification=True
            )
            if str(message_row["sender_role"]) != "verifier":
                raise CollaborationUnauthorized("verification result requires verifier sender")
            self._resolve_capability_reports_conn(
                connection,
                claim=claim,
                verification_id=verification_id,
                verifier_id=verifier_id,
                verdict=verdict,
                message_id=str(message_row["message_id"]),
                now=now,
            )
            connection.execute(
                """
                INSERT INTO collaboration_verifications(
                  verification_id,channel_id,claim_id,claim_revision,verifier_id,
                  oracle_digest,verdict,
                  idempotency_key,request_digest,payload_json,message_id,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    verification_id,
                    channel_id,
                    claim_id,
                    expected_claim_revision,
                    verifier_id,
                    oracle_digest,
                    verdict,
                    idempotency_key,
                    request_digest,
                    _canonical_json(payload),
                    message_row["message_id"],
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE collaboration_verification_claims
                SET status=?,claim_revision=claim_revision+1,updated_at=?
                WHERE claim_id=? AND claim_revision=?
                """,
                (verdict, now, claim_id, expected_claim_revision),
            )
            row = connection.execute(
                "SELECT * FROM collaboration_verifications WHERE verification_id=?",
                (verification_id,),
            ).fetchone()
            if row is None:  # pragma: no cover
                raise CollaborationStorageError("verification result did not persist")
            response = self._verification_projection(row)
            return self._insert_operation_receipt_conn(
                connection,
                operation="verification.result",
                scope_id=claim_id,
                actor_role="verifier",
                actor_id=verifier_id,
                idempotency_key=idempotency_key,
                request_digest=operation_digest,
                response_kind="verification_result",
                response=response,
                created_at=now,
            )

    async def get_verification(self, verification_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_verification_sync, str(verification_id))

    def _get_verification_sync(self, verification_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM collaboration_verifications WHERE verification_id=?",
                (verification_id,),
            ).fetchone()
        if row is None:
            raise CollaborationNotFound("independent verification result not found")
        return self._verification_projection(row)

    def _append_audit_conn(
        self, connection: sqlite3.Connection, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        normalized = _record(data)
        channel_id = str(normalized["channel_id"]) if normalized.get("channel_id") else None
        entity_kind = str(normalized.get("entity_kind", "collaboration_channel"))
        entity_id = str(
            normalized.get("entity_id") or channel_id or normalized.get("task_id") or "global"
        )
        event_type = str(
            normalized.get("event_type") or normalized.get("action") or "collaboration.audit"
        )
        actor_role = str(normalized.get("actor_role", "user"))
        actor_id = str(normalized.get("actor_id", "platform"))
        idempotency_key = str(
            normalized.get("idempotency_key")
            or f"{event_type}:{normalized.get('task_revision', '0')}"
        )
        self._reject_registered_secret_identifiers(
            {
                "channel_id": channel_id,
                "entity_kind": entity_kind,
                "entity_id": entity_id,
                "event_type": event_type,
                "actor_role": actor_role,
                "actor_id": actor_id,
                "idempotency_key": idempotency_key,
            }
        )
        details = self._safe_payload(
            normalized.get(
                "details",
                {
                    key: value
                    for key, value in normalized.items()
                    if key
                    not in {
                        "audit_id",
                        "channel_id",
                        "entity_kind",
                        "entity_id",
                        "event_type",
                        "action",
                        "actor_role",
                        "actor_id",
                        "idempotency_key",
                        "created_at",
                    }
                },
            )
        )
        semantic = {
            "channel_id": channel_id,
            "entity_kind": entity_kind,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_role": actor_role,
            "actor_id": actor_id,
            "details": details,
        }
        request_digest = _idempotency_digest(semantic)
        audit_id = str(
            normalized.get("audit_id")
            or f"audit:{hashlib.sha256((entity_kind + entity_id + actor_id + idempotency_key).encode()).hexdigest()}"
        )
        replay = connection.execute(
            """
            SELECT * FROM collaboration_audit
            WHERE entity_kind=? AND entity_id=? AND actor_role=?
              AND actor_id=? AND idempotency_key=?
            """,
            (entity_kind, entity_id, actor_role, actor_id, idempotency_key),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                raise CollaborationConflict("audit idempotency key was reused with another payload")
            return self._decode_row(replay)
        created_at = _now().isoformat()
        connection.execute(
            """
            INSERT INTO collaboration_audit(
              audit_id,channel_id,entity_kind,entity_id,event_type,actor_role,
              actor_id,idempotency_key,request_digest,details_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                audit_id,
                channel_id,
                entity_kind,
                entity_id,
                event_type,
                actor_role,
                actor_id,
                idempotency_key,
                request_digest,
                _canonical_json(details),
                created_at,
            ),
        )
        row = connection.execute(
            "SELECT * FROM collaboration_audit WHERE audit_id=?", (audit_id,)
        ).fetchone()
        if row is None:  # pragma: no cover
            raise CollaborationStorageError("audit record did not persist")
        return self._decode_row(row)

    async def append_audit(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        data = _record(record)
        async with self._lock:
            result = await asyncio.to_thread(self._append_audit_sync, data)
        self._secure_database_files()
        return result

    def _append_audit_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._append_audit_conn(connection, data)

    def _enqueue_outbox_conn(
        self, connection: sqlite3.Connection, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        normalized = _record(data)
        destination = str(normalized.get("destination") or normalized.get("kind") or "developer")
        idempotency_key = str(_required(normalized, "idempotency_key"))
        self._reject_registered_secret_identifiers(
            {
                "destination": destination,
                "idempotency_key": idempotency_key,
                "aggregate_id": normalized.get("aggregate_id"),
                "message_id": normalized.get("message_id"),
            }
        )
        aggregate_id = normalized.get("aggregate_id")
        channel_id = normalized.get("channel_id")
        if channel_id is None and aggregate_id is not None:
            report = connection.execute(
                "SELECT channel_id FROM collaboration_reports WHERE report_id=?",
                (str(aggregate_id),),
            ).fetchone()
            if report is not None:
                channel_id = report["channel_id"]
            else:
                claim = connection.execute(
                    """
                    SELECT channel_id FROM collaboration_verification_claims
                    WHERE claim_id=?
                    """,
                    (str(aggregate_id),),
                ).fetchone()
                if claim is not None:
                    channel_id = claim["channel_id"]
        if channel_id is None:
            raise ValueError("outbox record requires a channel or known aggregate")
        channel_id = str(channel_id)
        self._channel_row(connection, channel_id)
        payload = self._safe_payload(normalized.get("payload", normalized))
        semantic = {
            "channel_id": channel_id,
            "message_id": normalized.get("message_id"),
            "destination": destination,
            "payload": payload,
        }
        request_digest = _idempotency_digest(semantic)
        replay = connection.execute(
            """
            SELECT * FROM collaboration_outbox
            WHERE channel_id=? AND destination=? AND idempotency_key=?
            """,
            (channel_id, destination, idempotency_key),
        ).fetchone()
        if replay is not None:
            if not hmac.compare_digest(str(replay["request_digest"]), request_digest):
                raise CollaborationConflict(
                    "outbox idempotency key was reused with another payload"
                )
            return self._decode_row(replay)
        now = _now().isoformat()
        available_at = _iso(normalized.get("available_at"), default=_now())
        outbox_id = str(
            normalized.get("outbox_id")
            or f"outbox:{hashlib.sha256((channel_id + destination + idempotency_key).encode()).hexdigest()}"
        )
        connection.execute(
            """
            INSERT INTO collaboration_outbox(
              outbox_id,channel_id,message_id,destination,idempotency_key,
              request_digest,payload_json,status,attempts,available_at,
              created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'pending',0,?,?,?)
            """,
            (
                outbox_id,
                channel_id,
                normalized.get("message_id"),
                destination,
                idempotency_key,
                request_digest,
                _canonical_json(payload),
                available_at,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM collaboration_outbox WHERE outbox_id=?", (outbox_id,)
        ).fetchone()
        if row is None:  # pragma: no cover
            raise CollaborationStorageError("outbox record did not persist")
        return self._decode_row(row)

    async def enqueue_outbox(self, record: Mapping[str, Any] | Any) -> dict[str, Any]:
        data = _record(record)
        async with self._lock:
            result = await asyncio.to_thread(self._enqueue_outbox_sync, data)
        self._secure_database_files()
        return result

    def _enqueue_outbox_sync(self, data: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            return self._enqueue_outbox_conn(connection, data)

    async def list_pending_outbox(
        self, *, now: datetime | str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 5_000:
            raise ValueError("outbox limit must be between 1 and 5000")
        return await asyncio.to_thread(
            self._list_pending_outbox_sync, _as_utc(now, default=_now()), limit
        )

    async def list_developer_inbox_deliveries(
        self,
        *,
        after: int = 0,
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        """Read immutable global outbox rowids as the developer inbox cursor."""

        if after < 0 or not 1 <= limit <= 5_000:
            raise ValueError("developer inbox pagination is out of range")
        return await asyncio.to_thread(
            self._list_developer_inbox_deliveries_sync,
            after,
            limit,
        )

    def _list_developer_inbox_deliveries_sync(
        self,
        after: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                """
                SELECT rowid AS delivery_seq,* FROM collaboration_outbox
                WHERE destination='developer_inbox' ORDER BY rowid
                """,
            ).fetchall()
            latest: dict[tuple[str, str], int] = {}
            for row in rows:
                payload = json.loads(str(row["payload_json"]))
                if not isinstance(payload, dict):
                    continue
                for kind, field in (("report", "report_id"), ("claim", "claim_id")):
                    identifier = payload.get(field)
                    if identifier is not None:
                        latest[(kind, str(identifier))] = int(row["delivery_seq"])
                        break
            selected: list[dict[str, Any]] = []
            for row in rows:
                sequence = int(row["delivery_seq"])
                if sequence <= after:
                    continue
                payload = json.loads(str(row["payload_json"]))
                if not isinstance(payload, dict):
                    continue
                aggregate: tuple[str, str] | None = None
                if payload.get("report_id") is not None:
                    aggregate = ("report", str(payload["report_id"]))
                elif payload.get("claim_id") is not None:
                    aggregate = ("claim", str(payload["claim_id"]))
                if aggregate is None or latest.get(aggregate) != sequence:
                    continue
                delivery = self._decode_row(row)
                if aggregate[0] == "report":
                    report_row = connection.execute(
                        "SELECT * FROM collaboration_reports WHERE report_id=?",
                        (aggregate[1],),
                    ).fetchone()
                    if report_row is not None:
                        delivery["report_snapshot"] = self._decode_report(report_row)
                else:
                    claim_row = connection.execute(
                        """
                        SELECT * FROM collaboration_verification_claims WHERE claim_id=?
                        """,
                        (aggregate[1],),
                    ).fetchone()
                    if claim_row is not None:
                        delivery["claim_snapshot"] = self._decode_claim(claim_row)
                selected.append(delivery)
                if len(selected) >= limit:
                    break
            return selected
        finally:
            connection.close()

    def _list_pending_outbox_sync(self, now: datetime, limit: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM collaboration_outbox
                WHERE status='pending' AND available_at<=?
                ORDER BY available_at,created_at,outbox_id LIMIT ?
                """,
                (now.isoformat(), limit),
            ).fetchall()
        return [self._decode_row(row) for row in rows]

    async def mark_outbox_delivered(
        self, outbox_id: str, *, delivered_at: datetime | str | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._mark_outbox_delivered_sync,
                outbox_id,
                _as_utc(delivered_at, default=_now()),
            )

    def _mark_outbox_delivered_sync(self, outbox_id: str, delivered_at: datetime) -> dict[str, Any]:
        now = delivered_at.isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collaboration_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise CollaborationNotFound("outbox record not found")
            if str(row["status"]) != "delivered":
                connection.execute(
                    """
                    UPDATE collaboration_outbox
                    SET status='delivered',attempts=attempts+1,updated_at=?,
                        delivered_at=?,last_error=NULL WHERE outbox_id=?
                    """,
                    (now, now, outbox_id),
                )
            persisted = connection.execute(
                "SELECT * FROM collaboration_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if persisted is None:  # pragma: no cover
                raise CollaborationStorageError("outbox record disappeared")
            return self._decode_row(persisted)

    async def mark_outbox_failed(
        self,
        outbox_id: str,
        error: str,
        *,
        retry_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._mark_outbox_failed_sync,
                outbox_id,
                error,
                _iso(retry_at),
            )

    def _mark_outbox_failed_sync(
        self, outbox_id: str, error: str, retry_at: str | None
    ) -> dict[str, Any]:
        error = str(self._safe_payload(error))
        now = _now().isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM collaboration_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if row is None:
                raise CollaborationNotFound("outbox record not found")
            if str(row["status"]) == "delivered":
                return self._decode_row(row)
            connection.execute(
                """
                UPDATE collaboration_outbox
                SET status=?,attempts=attempts+1,available_at=COALESCE(?,available_at),
                    updated_at=?,last_error=? WHERE outbox_id=?
                """,
                ("pending" if retry_at is not None else "failed", retry_at, now, error, outbox_id),
            )
            persisted = connection.execute(
                "SELECT * FROM collaboration_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            if persisted is None:  # pragma: no cover
                raise CollaborationStorageError("outbox record disappeared")
            return self._decode_row(persisted)

    async def has_pending_user_action(self) -> bool:
        return await asyncio.to_thread(self._has_pending_user_action_sync)

    def _has_pending_user_action_sync(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT EXISTS(
                  SELECT 1 FROM collaboration_reports
                  WHERE status='awaiting_user_review'
                ) AS pending
                """
            ).fetchone()
        return bool(row["pending"])

    async def get_message_by_idempotency(
        self,
        channel_id: str | UUID,
        sender_role: str | Enum,
        sender_id: str,
        key: str,
        *,
        client_request_digest: str | None = None,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._get_message_by_idempotency_sync,
            str(channel_id),
            str(sender_role.value if isinstance(sender_role, Enum) else sender_role),
            sender_id,
            key,
            (
                _validated_request_digest(client_request_digest)
                if client_request_digest is not None
                else None
            ),
        )

    def _get_message_by_idempotency_sync(
        self,
        channel_id: str,
        sender_role: str,
        sender_id: str,
        key: str,
        client_request_digest: str | None,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            self._channel_row(connection, channel_id)
            row = connection.execute(
                """
                SELECT * FROM collaboration_messages
                WHERE channel_id=? AND sender_role=? AND sender_id=? AND idempotency_key=?
                """,
                (channel_id, sender_role, sender_id, key),
            ).fetchone()
        if (
            row is not None
            and client_request_digest is not None
            and not hmac.compare_digest(str(row["client_request_digest"]), client_request_digest)
        ):
            raise CollaborationConflict(
                "message idempotency key was reused with another client request"
            )
        return self._decode_message(row) if row is not None else None

    async def get_developer_lease_receipt(
        self,
        report_id: str | UUID,
        operation: str,
        owner_id: str,
        idempotency_key: str,
        *,
        expected_revision: int | None = None,
        ttl_seconds: int | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(
            self._get_developer_lease_receipt_sync,
            str(report_id),
            operation,
            owner_id,
            idempotency_key,
            expected_revision,
            ttl_seconds,
            reason,
        )

    def _get_developer_lease_receipt_sync(
        self,
        report_id: str,
        operation: str,
        owner_id: str,
        idempotency_key: str,
        expected_revision: int | None,
        ttl_seconds: int | None,
        reason: str | None,
    ) -> dict[str, Any] | None:
        expected_digest: str | None = None
        if expected_revision is not None:
            if operation == "acquire":
                if ttl_seconds is None:
                    raise ValueError("acquire receipt lookup requires ttl_seconds")
                expected_digest = _idempotency_digest(
                    {
                        "report_id": report_id,
                        "owner_id": owner_id,
                        "expected_report_revision": expected_revision,
                        "ttl_seconds": ttl_seconds,
                    }
                )
            elif operation == "renew":
                if ttl_seconds is None:
                    raise ValueError("renew receipt lookup requires ttl_seconds")
                expected_digest = _idempotency_digest(
                    {
                        "expected_revision": expected_revision,
                        "ttl_seconds": ttl_seconds,
                    }
                )
            elif operation == "release":
                if reason is None:
                    raise ValueError("release receipt lookup requires reason")
                expected_digest = _idempotency_digest(
                    {
                        "expected_revision": expected_revision,
                        "reason": str(self._safe_payload(reason)),
                    }
                )
        with self._connect() as connection:
            self._report_row(connection, report_id)
            if operation not in {"acquire", "renew", "release"}:
                raise ValueError("unsupported developer lease operation")
            operation_receipt = self._get_operation_receipt_conn(
                connection,
                operation=f"lease.{operation}",
                scope_id=report_id,
                actor_role="codex",
                actor_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=expected_digest,
            )
            if operation_receipt is not None:
                return operation_receipt
            if operation == "acquire":
                row = connection.execute(
                    """
                    SELECT * FROM collaboration_developer_leases
                    WHERE report_id=? AND owner_id=? AND idempotency_key=?
                    """,
                    (report_id, owner_id, idempotency_key),
                ).fetchone()
            elif operation in {"renew", "release"}:
                row = connection.execute(
                    """
                    SELECT lease.*,receipt.request_digest AS receipt_request_digest
                    FROM collaboration_lease_operations AS receipt
                    JOIN collaboration_developer_leases AS lease
                      ON lease.lease_id=receipt.lease_id
                    WHERE lease.report_id=? AND receipt.operation=?
                      AND receipt.owner_id=? AND receipt.idempotency_key=?
                    """,
                    (report_id, operation, owner_id, idempotency_key),
                ).fetchone()
        if row is not None and expected_digest is not None:
            persisted_digest = str(
                row["request_digest"] if operation == "acquire" else row["receipt_request_digest"]
            )
            if not hmac.compare_digest(persisted_digest, expected_digest):
                raise CollaborationConflict(
                    f"lease {operation} idempotency key was reused with another payload"
                )
        return self._decode_lease(row) if row is not None else None

    async def export_channel(self, channel_id: str | UUID) -> dict[str, Any]:
        return await asyncio.to_thread(self._export_channel_sync, str(channel_id))

    def _export_channel_sync(self, channel_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            # Every table in a causal export must come from one SQLite snapshot.
            # An explicit read transaction is required under WAL; otherwise each
            # SELECT can observe a later writer commit and produce a torn export.
            connection.execute("BEGIN")
            channel = self._channel_row(connection, channel_id)
            messages = connection.execute(
                """
                SELECT * FROM collaboration_messages
                WHERE channel_id=? ORDER BY seq
                """,
                (channel_id,),
            ).fetchall()
            reports = connection.execute(
                """
                SELECT * FROM collaboration_reports
                WHERE channel_id=? ORDER BY created_at,report_id
                """,
                (channel_id,),
            ).fetchall()
            report_revisions = connection.execute(
                """
                SELECT revision.* FROM collaboration_report_revisions AS revision
                JOIN collaboration_reports AS report ON report.report_id=revision.report_id
                WHERE report.channel_id=? ORDER BY revision.report_id,revision.revision
                """,
                (channel_id,),
            ).fetchall()
            report_evidence_budgets = connection.execute(
                """
                SELECT * FROM collaboration_report_evidence_budgets
                WHERE channel_id=? ORDER BY report_id
                """,
                (channel_id,),
            ).fetchall()
            approvals = connection.execute(
                """
                SELECT * FROM collaboration_approvals
                WHERE channel_id=? ORDER BY created_at,approval_id
                """,
                (channel_id,),
            ).fetchall()
            credentials = connection.execute(
                """
                SELECT * FROM collaboration_credentials
                WHERE channel_id=? ORDER BY created_at,credential_id
                """,
                (channel_id,),
            ).fetchall()
            cursors = connection.execute(
                """
                SELECT * FROM collaboration_reader_cursors
                WHERE channel_id=? ORDER BY reader_role,reader_id
                """,
                (channel_id,),
            ).fetchall()
            cursor_receipts = connection.execute(
                """
                SELECT * FROM collaboration_reader_ack_receipts
                WHERE channel_id=? ORDER BY created_at,reader_id,idempotency_key
                """,
                (channel_id,),
            ).fetchall()
            leases = connection.execute(
                """
                SELECT * FROM collaboration_developer_leases
                WHERE channel_id=? ORDER BY acquired_at,lease_id
                """,
                (channel_id,),
            ).fetchall()
            lease_operations = connection.execute(
                """
                SELECT operation.* FROM collaboration_lease_operations AS operation
                JOIN collaboration_developer_leases AS lease
                  ON lease.lease_id=operation.lease_id
                WHERE lease.channel_id=?
                ORDER BY operation.created_at,operation.lease_id,operation.operation
                """,
                (channel_id,),
            ).fetchall()

            def rows(table: str, order: str) -> list[sqlite3.Row]:
                return list(
                    connection.execute(
                        f"SELECT * FROM {table} WHERE channel_id=? ORDER BY {order}",
                        (channel_id,),
                    ).fetchall()
                )

            developer_responses = rows(
                "collaboration_developer_responses", "created_at,response_id"
            )
            task_amendments = rows("collaboration_task_amendments", "created_at,amendment_id")
            environment_responses = rows(
                "collaboration_environment_responses", "created_at,response_id"
            )
            reprobes = rows("collaboration_reprobes", "created_at,reprobe_id")
            claims = rows("collaboration_verification_claims", "frozen_at,claim_id")
            verifications = rows("collaboration_verifications", "created_at,verification_id")
            audit = rows("collaboration_audit", "created_at,audit_id")
            outbox = rows("collaboration_outbox", "created_at,outbox_id")
            channel_operations = rows(
                "collaboration_channel_operations", "created_at,operation,idempotency_key"
            )
            operation_receipts = connection.execute(
                """
                SELECT * FROM collaboration_operation_receipts
                WHERE scope_id=?
                   OR scope_id IN (
                     SELECT report_id FROM collaboration_reports WHERE channel_id=?
                   )
                   OR scope_id IN (
                     SELECT claim_id FROM collaboration_verification_claims
                     WHERE channel_id=?
                   )
                ORDER BY created_at,operation,scope_id,actor_id,idempotency_key
                """,
                (channel_id, channel_id, channel_id),
            ).fetchall()
        message_seqs = [int(item["seq"]) for item in messages]
        next_seq = int(channel["next_seq"])
        complete = message_seqs == list(range(1, next_seq))
        counts = {
            "credentials": len(credentials),
            "messages": len(messages),
            "reports": len(reports),
            "report_revisions": len(report_revisions),
            "report_evidence_budgets": len(report_evidence_budgets),
            "approvals": len(approvals),
            "reader_cursors": len(cursors),
            "reader_ack_receipts": len(cursor_receipts),
            "developer_leases": len(leases),
            "lease_operations": len(lease_operations),
            "developer_responses": len(developer_responses),
            "task_amendments": len(task_amendments),
            "environment_responses": len(environment_responses),
            "reprobes": len(reprobes),
            "claims": len(claims),
            "verifications": len(verifications),
            "audit": len(audit),
            "outbox": len(outbox),
            "channel_operations": len(channel_operations),
            "operation_receipts": len(operation_receipts),
        }
        return {
            "schema_version": "1.0",
            "complete": complete,
            "counts": counts,
            "watermark": {
                "min_message_seq": message_seqs[0] if message_seqs else None,
                "max_message_seq": message_seqs[-1] if message_seqs else None,
                "next_seq": next_seq,
                "max_report_evidence_rounds": (
                    int(channel["max_report_evidence_rounds"])
                    if channel["max_report_evidence_rounds"] is not None
                    else None
                ),
                "report_evidence_rounds_used_total": sum(
                    int(item["rounds_used"]) for item in report_evidence_budgets
                ),
                "max_report_evidence_rounds_used": max(
                    (int(item["rounds_used"]) for item in report_evidence_budgets),
                    default=0,
                ),
                "budget_exhausted_reports": sum(
                    str(item["status"]) == "budget_exhausted"
                    for item in report_evidence_budgets
                ),
            },
            "channel": self._decode_channel(channel),
            "credentials": [self._credential_public(item) for item in credentials],
            "messages": [self._decode_message(item) for item in messages],
            "reports": [self._decode_report(item) for item in reports],
            "report_revisions": [self._decode_row(item) for item in report_revisions],
            "report_evidence_budgets": [
                self._decode_row(item) for item in report_evidence_budgets
            ],
            "approvals": [self._decode_approval(item) for item in approvals],
            "reader_cursors": [_validated_projection(ReaderCursor, dict(item)) for item in cursors],
            "reader_ack_receipts": [self._decode_row(item) for item in cursor_receipts],
            "developer_leases": [self._decode_lease(item) for item in leases],
            "lease_operations": [self._decode_row(item) for item in lease_operations],
            "developer_responses": [
                self._decode_typed_payload(item, DeveloperResponse) for item in developer_responses
            ],
            "task_amendments": [
                self._decode_typed_payload(item, TaskPackageAmendment) for item in task_amendments
            ],
            "environment_responses": [
                self._decode_typed_payload(item, EnvironmentResponse)
                for item in environment_responses
            ],
            "reprobes": [
                self._decode_typed_payload(item, LiliesReprobeResult) for item in reprobes
            ],
            "claims": [self._decode_claim(item) for item in claims],
            "verifications": [self._verification_projection(item) for item in verifications],
            "audit": [self._decode_row(item) for item in audit],
            "outbox": [self._decode_row(item) for item in outbox],
            "channel_operations": [self._decode_row(item) for item in channel_operations],
            "operation_receipts": [
                self._decode_operation_receipt(item) for item in operation_receipts
            ],
        }


# Compatibility alias for callers that prefer the module's full noun.
CollaborationStorage = CollaborationStore
