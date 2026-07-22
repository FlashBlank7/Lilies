from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 3
SESSION_STATUSES = frozenset(
    {
        "ready",
        "running",
        "waiting_permission",
        "waiting_collaboration",
        "interrupted",
        "error",
        "cancelled",
        "completed",
        "closed",
    }
)
TURN_STATUSES = frozenset(
    {
        "running",
        "waiting_permission",
        "waiting_collaboration",
        "interrupted",
        "error",
        "cancelled",
        "completed",
    }
)
PERMISSION_STATUSES = frozenset({"pending", "allowed", "denied", "cancelled"})
ALLOWED_CLIENT_SCOPES = frozenset(
    {
        "lilies.session:read",
        "lilies.session:write",
        "lilies.permission:resolve",
        "lilies.daemon:control",
        "lilies.credential:write",
    }
)

_SESSION_TRANSITIONS: dict[str, frozenset[str]] = {
    "ready": frozenset({"running", "error", "cancelled", "completed", "closed"}),
    "running": frozenset(
        {
            "ready",
            "waiting_permission",
            "waiting_collaboration",
            "interrupted",
            "error",
            "cancelled",
            "completed",
        }
    ),
    "waiting_permission": frozenset({"running", "interrupted", "error", "cancelled"}),
    "waiting_collaboration": frozenset(
        {"running", "ready", "interrupted", "error", "cancelled"}
    ),
    "interrupted": frozenset({"running", "error", "cancelled", "closed"}),
    "error": frozenset({"ready", "running", "cancelled", "closed"}),
    "cancelled": frozenset({"closed"}),
    "completed": frozenset({"closed"}),
    "closed": frozenset(),
}

_SECRET_EVENT_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "credential_value",
        "pairing_code",
        "password",
        "secret",
        "secret_value",
        "token",
    }
)


class LiliesStorageError(RuntimeError):
    """Base error for the standalone Lilies store."""


class LiliesNotFoundError(LiliesStorageError):
    """A requested local record does not exist."""


class LiliesConflictError(LiliesStorageError):
    """A compare-and-set or idempotency invariant was violated."""


class LiliesAccessDeniedError(LiliesStorageError):
    """A client cannot access the requested local resource."""


class LiliesAuthenticationError(LiliesStorageError):
    """A local client credential is invalid, expired, or revoked."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _normalise_datetime(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _redact_event_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if str(key).lower() in _SECRET_EVENT_KEYS else _redact_event_data(item)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_event_data(item) for item in value]
    return value


def _digest(secret_value: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", secret_value.encode("utf-8"), salt, 120_000).hex()


def _payload_hash(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_json_dump(value).encode("utf-8")).hexdigest()


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _non_negative_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if result >= 0 and result < float("inf") else 0.0


def _checkpoint_metrics(checkpoint_json: str) -> dict[str, int | float]:
    checkpoint = _json_load(checkpoint_json, {})
    metrics = checkpoint.get("metrics", {}) if isinstance(checkpoint, Mapping) else {}
    metrics = metrics if isinstance(metrics, Mapping) else {}
    usage = metrics.get("usage", {})
    usage = usage if isinstance(usage, Mapping) else {}
    return {
        "token_count": _non_negative_int(usage.get("input_tokens"))
        + _non_negative_int(usage.get("output_tokens")),
        "cost_usd": _non_negative_float(usage.get("cost_usd")),
        "tool_count": _non_negative_int(metrics.get("tool_calls")),
        "model_call_count": _non_negative_int(metrics.get("model_calls")),
    }


class LiliesStorage:
    """Independent SQLite/WAL persistence for the loopback Lilies daemon.

    The module intentionally has no dependency on platform Storage, WorkflowStorage,
    Builder, or any platform service. Every state change and its stream event share one
    SQLite transaction. Secret-bearing operations use the separate security-event stream.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        allowed_scopes: frozenset[str] = ALLOWED_CLIENT_SCOPES,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "lilies.db"
        self.allowed_scopes = allowed_scopes
        self._lock = asyncio.Lock()

    async def initialize(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._initialize_sync)

    def _prepare_paths(self) -> None:
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        descriptor = os.open(self.db_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        os.close(descriptor)
        os.chmod(self.db_path, 0o600)

    def _secure_database_files(self) -> None:
        for path in (self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            if path.exists():
                os.chmod(path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        self._prepare_paths()
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        self._secure_database_files()
        return conn

    def _initialize_sync(self) -> dict[str, int]:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version INTEGER PRIMARY KEY,
                  applied_at TEXT NOT NULL
                )
                """
            )
            current = int(
                conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations")
                .fetchone()["version"]
            )
            if current > SCHEMA_VERSION:
                raise LiliesStorageError(
                    f"lilies.db schema {current} is newer than supported {SCHEMA_VERSION}"
                )
            if current < 1:
                self._migrate_v1(conn)
                current = 1
            if current < 2:
                self._migrate_v2(conn)
                current = 2
            if current < 3:
                self._migrate_v3(conn)
            recovery = self._recover_sync(conn)
        self._secure_database_files()
        return {"schema_version": SCHEMA_VERSION, **recovery}

    def _migrate_v1(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY,
              status TEXT NOT NULL CHECK(status IN (
                'ready','running','waiting_permission','waiting_collaboration','interrupted',
                'error','cancelled','completed','closed'
              )),
              agent_version TEXT NOT NULL,
              model_profile TEXT NOT NULL,
              system_identity_version TEXT NOT NULL,
              config_json TEXT NOT NULL DEFAULT '{}',
              profile_json TEXT NOT NULL DEFAULT '{}',
              context_summary TEXT NOT NULL DEFAULT '',
              summary_through_event_seq INTEGER NOT NULL DEFAULT 0 CHECK(summary_through_event_seq >= 0),
              token_count INTEGER NOT NULL DEFAULT 0 CHECK(token_count >= 0),
              cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd >= 0),
              tool_count INTEGER NOT NULL DEFAULT 0 CHECK(tool_count >= 0),
              model_call_count INTEGER NOT NULL DEFAULT 0 CHECK(model_call_count >= 0),
              assignment_id TEXT,
              assignment_json TEXT,
              platform_contract_digest TEXT,
              waiting_permission_id TEXT,
              waiting_collaboration_id TEXT,
              last_platform_cursor INTEGER NOT NULL DEFAULT 0 CHECK(last_platform_cursor >= 0),
              last_pipeline_cursor INTEGER NOT NULL DEFAULT 0 CHECK(last_pipeline_cursor >= 0),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE messages (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              turn_id TEXT,
              role TEXT NOT NULL CHECK(role IN ('system','user','assistant','tool')),
              content_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE turns (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              request_id TEXT NOT NULL,
              idempotency_key TEXT NOT NULL,
              input_message_id TEXT,
              status TEXT NOT NULL CHECK(status IN (
                'running','waiting_permission','waiting_collaboration','interrupted','error',
                'cancelled','completed'
              )),
              phase TEXT NOT NULL,
              checkpoint_json TEXT NOT NULL DEFAULT '{}',
              side_effect_state TEXT NOT NULL DEFAULT 'none',
              token_count INTEGER NOT NULL DEFAULT 0 CHECK(token_count >= 0),
              cost_usd REAL NOT NULL DEFAULT 0 CHECK(cost_usd >= 0),
              tool_count INTEGER NOT NULL DEFAULT 0 CHECK(tool_count >= 0),
              model_call_count INTEGER NOT NULL DEFAULT 0 CHECK(model_call_count >= 0),
              error TEXT,
              interruption_reason TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              finished_at TEXT,
              UNIQUE(session_id, idempotency_key),
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
              FOREIGN KEY(input_message_id) REFERENCES messages(id) ON DELETE SET NULL
            );
            CREATE TABLE events (
              session_id TEXT NOT NULL,
              seq INTEGER NOT NULL CHECK(seq > 0),
              event_type TEXT NOT NULL,
              data_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(session_id, seq),
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE pairing_codes (
              id TEXT PRIMARY KEY,
              salt_hex TEXT NOT NULL,
              digest TEXT NOT NULL,
              allowed_scopes_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              redeemed_at TEXT,
              redeemed_by_client_id TEXT,
              client_nonce_digest TEXT UNIQUE
            );
            CREATE TABLE clients (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              token_salt_hex TEXT NOT NULL,
              token_digest TEXT NOT NULL,
              scopes_json TEXT NOT NULL,
              daemon_fingerprint TEXT NOT NULL,
              expires_at TEXT,
              revoked_at TEXT,
              created_at TEXT NOT NULL,
              last_seen_at TEXT
            );
            CREATE TABLE client_session_acl (
              client_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              created_at TEXT NOT NULL,
              PRIMARY KEY(client_id, session_id),
              FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE reader_acks (
              client_id TEXT NOT NULL,
              session_id TEXT NOT NULL,
              cursor INTEGER NOT NULL CHECK(cursor >= 0),
              updated_at TEXT NOT NULL,
              PRIMARY KEY(client_id, session_id),
              FOREIGN KEY(client_id, session_id)
                REFERENCES client_session_acl(client_id, session_id) ON DELETE CASCADE
            );
            CREATE TABLE permission_requests (
              id TEXT PRIMARY KEY,
              session_id TEXT NOT NULL,
              turn_id TEXT NOT NULL,
              tool_call_id TEXT NOT NULL,
              tool_name TEXT NOT NULL,
              input_digest TEXT NOT NULL,
              tool_input_json TEXT NOT NULL DEFAULT '{}',
              checkpoint_json TEXT NOT NULL DEFAULT '{}',
              input_summary_json TEXT NOT NULL DEFAULT '{}',
              status TEXT NOT NULL CHECK(status IN ('pending','allowed','denied','cancelled')),
              decision_client_id TEXT,
              decision_input_json TEXT,
              decision_input_digest TEXT,
              decision_message TEXT,
              decision_idempotency_key TEXT,
              decision_payload_hash TEXT,
              created_at TEXT NOT NULL,
              resolved_at TEXT,
              FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE,
              FOREIGN KEY(turn_id) REFERENCES turns(id) ON DELETE CASCADE,
              FOREIGN KEY(decision_client_id) REFERENCES clients(id) ON DELETE SET NULL
            );
            CREATE UNIQUE INDEX idx_permission_one_pending_session
              ON permission_requests(session_id) WHERE status='pending';
            CREATE TABLE credentials (
              ref TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              secret_value TEXT NOT NULL,
              client_id TEXT,
              assignment_id TEXT,
              expires_at TEXT,
              revoked_at TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL
            );
            CREATE TABLE security_events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              event_type TEXT NOT NULL,
              client_id TEXT,
              details_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE SET NULL
            );
            CREATE INDEX idx_sessions_updated ON sessions(updated_at DESC);
            CREATE INDEX idx_messages_session_created ON messages(session_id, created_at);
            CREATE INDEX idx_turns_session_created ON turns(session_id, created_at);
            CREATE INDEX idx_events_session_seq ON events(session_id, seq);
            CREATE INDEX idx_pairing_codes_expiry ON pairing_codes(expires_at);
            CREATE INDEX idx_clients_expiry ON clients(expires_at);
            CREATE INDEX idx_permission_session_status
              ON permission_requests(session_id, status, created_at);
            CREATE INDEX idx_credentials_assignment ON credentials(assignment_id);
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (1, utc_now()),
        )
        conn.execute("PRAGMA user_version=1")

    def _migrate_v2(self, conn: sqlite3.Connection) -> None:
        """Add durable credential contracts without invalidating v1 records."""

        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE credentials ADD COLUMN kind TEXT")
        conn.execute("ALTER TABLE credentials ADD COLUMN scopes_json TEXT NOT NULL DEFAULT '[]'")
        conn.execute("ALTER TABLE credentials ADD COLUMN provision_idempotency_key TEXT")
        conn.execute("ALTER TABLE credentials ADD COLUMN provision_payload_hash TEXT")
        conn.execute("ALTER TABLE credentials ADD COLUMN revoke_idempotency_key TEXT")
        conn.execute("ALTER TABLE credentials ADD COLUMN revoke_payload_hash TEXT")
        conn.execute("ALTER TABLE credentials ADD COLUMN revoke_reason TEXT")
        conn.execute("UPDATE credentials SET kind=name WHERE kind IS NULL")
        conn.execute(
            """
            CREATE UNIQUE INDEX idx_credentials_provision_idempotency
            ON credentials(COALESCE(client_id,''), provision_idempotency_key)
            WHERE provision_idempotency_key IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX idx_credentials_revoke_idempotency
            ON credentials(COALESCE(client_id,''), revoke_idempotency_key)
            WHERE revoke_idempotency_key IS NOT NULL
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (2, utc_now()),
        )
        conn.execute("PRAGMA user_version=2")

    def _migrate_v3(self, conn: sqlite3.Connection) -> None:
        """Record the one-time publication of active-turn checkpoint metrics."""

        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("ALTER TABLE turns ADD COLUMN metrics_settled_at TEXT")
        conn.execute(
            """
            UPDATE turns SET metrics_settled_at=COALESCE(finished_at,updated_at)
            WHERE status IN ('interrupted','error','cancelled','completed')
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (3, utc_now()),
        )
        conn.execute("PRAGMA user_version=3")

    def _recover_sync(self, conn: sqlite3.Connection) -> dict[str, int]:
        now = utc_now()
        running_turns = conn.execute(
            "SELECT * FROM turns WHERE status='running' ORDER BY created_at"
        ).fetchall()
        for row in running_turns:
            metrics = _checkpoint_metrics(row["checkpoint_json"])
            should_settle = row["metrics_settled_at"] is None
            published_metrics = metrics if should_settle else {
                "token_count": row["token_count"],
                "cost_usd": row["cost_usd"],
                "tool_count": row["tool_count"],
                "model_call_count": row["model_call_count"],
            }
            conn.execute(
                """
                UPDATE turns
                SET status='interrupted', phase='interrupted', interruption_reason='daemon_restart',
                    token_count=?,cost_usd=?,tool_count=?,model_call_count=?,
                    metrics_settled_at=COALESCE(metrics_settled_at,?),updated_at=?, finished_at=?
                WHERE id=? AND status='running'
                """,
                (
                    published_metrics["token_count"],
                    published_metrics["cost_usd"],
                    published_metrics["tool_count"],
                    published_metrics["model_call_count"],
                    now,
                    now,
                    now,
                    row["id"],
                ),
            )
            if should_settle:
                conn.execute(
                    """
                    UPDATE sessions SET token_count=token_count+?,cost_usd=cost_usd+?,
                      tool_count=tool_count+?,model_call_count=model_call_count+?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        metrics["token_count"],
                        metrics["cost_usd"],
                        metrics["tool_count"],
                        metrics["model_call_count"],
                        now,
                        row["session_id"],
                    ),
                )
            self._append_event_conn(
                conn,
                row["session_id"],
                "turn.interrupted",
                {
                    "turn_id": row["id"],
                    "reason": "daemon_restart",
                    "metrics_settled": should_settle,
                },
                created_at=now,
            )
        running_sessions = conn.execute(
            "SELECT id FROM sessions WHERE status='running' ORDER BY created_at"
        ).fetchall()
        for row in running_sessions:
            conn.execute(
                "UPDATE sessions SET status='interrupted', updated_at=? WHERE id=? AND status='running'",
                (now, row["id"]),
            )
            self._append_event_conn(
                conn,
                row["id"],
                "session.interrupted",
                {"from_status": "running", "to_status": "interrupted", "reason": "daemon_restart"},
                created_at=now,
            )
        return {
            "interrupted_sessions": len(running_sessions),
            "interrupted_turns": len(running_turns),
        }

    # Sessions -----------------------------------------------------------------

    async def create_session(
        self,
        *,
        session_id: str | None = None,
        agent_version: str = "lilies-local-v1",
        model_profile: str = "default",
        system_identity_version: str = "lilies-v1",
        config: Mapping[str, Any] | None = None,
        profile: Mapping[str, Any] | None = None,
        client_id: str | None = None,
        assignment_id: str | None = None,
        assignment: Mapping[str, Any] | None = None,
        platform_contract_digest: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._create_session_sync,
                session_id,
                agent_version,
                model_profile,
                system_identity_version,
                dict(config or {}),
                dict(profile or {}),
                client_id,
                assignment_id,
                dict(assignment) if assignment is not None else None,
                platform_contract_digest,
            )

    def _create_session_sync(
        self,
        session_id: str | None,
        agent_version: str,
        model_profile: str,
        system_identity_version: str,
        config: dict[str, Any],
        profile: dict[str, Any],
        client_id: str | None,
        assignment_id: str | None,
        assignment: dict[str, Any] | None,
        platform_contract_digest: str | None,
    ) -> dict[str, Any]:
        session_id = session_id or str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            if client_id is not None:
                self._require_client_conn(conn, client_id)
            try:
                conn.execute(
                    """
                    INSERT INTO sessions(
                      id,status,agent_version,model_profile,system_identity_version,
                      config_json,profile_json,assignment_id,assignment_json,
                      platform_contract_digest,created_at,updated_at
                    ) VALUES (?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        agent_version,
                        model_profile,
                        system_identity_version,
                        _json_dump(config),
                        _json_dump(profile),
                        assignment_id,
                        _json_dump(assignment) if assignment is not None else None,
                        platform_contract_digest,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LiliesConflictError(f"session already exists: {session_id}") from exc
            if client_id is not None:
                conn.execute(
                    "INSERT INTO client_session_acl(client_id,session_id,created_at) VALUES (?,?,?)",
                    (client_id, session_id, now),
                )
            self._append_event_conn(
                conn,
                session_id,
                "session.created",
                {
                    "session_id": session_id,
                    "status": "ready",
                    "assignment_id": assignment_id,
                    "platform_contract_digest": platform_contract_digest,
                },
                created_at=now,
            )
            row = self._require_session_conn(conn, session_id)
        return self._session_from_row(row)

    async def get_session(
        self, session_id: str, *, client_id: str | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_session_sync, session_id, client_id)

    def _get_session_sync(self, session_id: str, client_id: str | None) -> dict[str, Any]:
        with self._connect() as conn:
            if client_id is not None:
                self._require_session_access_conn(conn, client_id, session_id)
            return self._session_from_row(self._require_session_conn(conn, session_id))

    async def list_sessions(self, *, client_id: str | None = None) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_sessions_sync, client_id)

    def _list_sessions_sync(self, client_id: str | None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if client_id is None:
                rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC, id").fetchall()
            else:
                self._require_client_conn(conn, client_id)
                rows = conn.execute(
                    """
                    SELECT s.* FROM sessions s
                    JOIN client_session_acl a ON a.session_id=s.id
                    WHERE a.client_id=? ORDER BY s.updated_at DESC, s.id
                    """,
                    (client_id,),
                ).fetchall()
        return [self._session_from_row(row) for row in rows]

    async def transition_session(
        self,
        session_id: str,
        to_status: str,
        *,
        reason: str | None = None,
        expected_status: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._transition_session_sync, session_id, to_status, reason, expected_status
            )

    def _transition_session_sync(
        self,
        session_id: str,
        to_status: str,
        reason: str | None,
        expected_status: str | None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            self._transition_session_conn(
                conn,
                session_id,
                to_status,
                reason=reason,
                expected_status=expected_status,
            )
            return self._session_from_row(self._require_session_conn(conn, session_id))

    def _transition_session_conn(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        to_status: str,
        *,
        reason: str | None = None,
        expected_status: str | None = None,
    ) -> None:
        if to_status not in SESSION_STATUSES:
            raise ValueError(f"unknown session status: {to_status}")
        row = self._require_session_conn(conn, session_id)
        from_status = str(row["status"])
        if expected_status is not None and from_status != expected_status:
            raise LiliesConflictError(
                f"session {session_id} expected {expected_status}, found {from_status}"
            )
        if from_status == to_status:
            return
        if to_status not in _SESSION_TRANSITIONS[from_status]:
            raise LiliesConflictError(f"invalid session transition: {from_status} -> {to_status}")
        now = utc_now()
        conn.execute(
            "UPDATE sessions SET status=?, updated_at=? WHERE id=?",
            (to_status, now, session_id),
        )
        self._append_event_conn(
            conn,
            session_id,
            "session.status_changed",
            {"from_status": from_status, "to_status": to_status, "reason": reason},
            created_at=now,
        )

    async def update_session_context(
        self,
        session_id: str,
        **changes: Any,
    ) -> dict[str, Any]:
        allowed = {
            "config",
            "profile",
            "context_summary",
            "summary_through_event_seq",
            "token_count",
            "cost_usd",
            "tool_count",
            "model_call_count",
            "assignment_id",
            "assignment",
            "platform_contract_digest",
            "waiting_permission_id",
            "waiting_collaboration_id",
            "last_platform_cursor",
            "last_pipeline_cursor",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"unknown session context fields: {sorted(unknown)}")
        async with self._lock:
            return await asyncio.to_thread(self._update_session_context_sync, session_id, changes)

    def _update_session_context_sync(
        self, session_id: str, changes: Mapping[str, Any]
    ) -> dict[str, Any]:
        column_names = {
            "config": "config_json",
            "profile": "profile_json",
            "assignment": "assignment_json",
        }
        json_fields = frozenset(column_names)
        assignments: list[str] = []
        values: list[Any] = []
        for field, value in changes.items():
            column = column_names.get(field, field)
            assignments.append(f"{column}=?")
            values.append(_json_dump(value) if field in json_fields and value is not None else value)
        with self._connect() as conn:
            self._require_session_conn(conn, session_id)
            if assignments:
                now = utc_now()
                assignments.append("updated_at=?")
                values.extend([now, session_id])
                conn.execute(
                    f"UPDATE sessions SET {', '.join(assignments)} WHERE id=?",  # noqa: S608
                    values,
                )
                self._append_event_conn(
                    conn,
                    session_id,
                    "session.context_updated",
                    {"fields": sorted(changes)},
                    created_at=now,
                )
            return self._session_from_row(self._require_session_conn(conn, session_id))

    # Messages and turns -------------------------------------------------------

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        *,
        turn_id: str | None = None,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._add_message_sync, session_id, role, content, turn_id, message_id
            )

    def _add_message_sync(
        self,
        session_id: str,
        role: str,
        content: Any,
        turn_id: str | None,
        message_id: str | None,
    ) -> dict[str, Any]:
        if role not in {"system", "user", "assistant", "tool"}:
            raise ValueError(f"unknown message role: {role}")
        message_id = message_id or str(uuid.uuid4())
        now = utc_now()
        with self._connect() as conn:
            self._require_session_conn(conn, session_id)
            if turn_id is not None:
                self._require_turn_conn(conn, turn_id, session_id=session_id)
            try:
                conn.execute(
                    """
                    INSERT INTO messages(id,session_id,turn_id,role,content_json,created_at)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (message_id, session_id, turn_id, role, _json_dump(content), now),
                )
            except sqlite3.IntegrityError as exc:
                raise LiliesConflictError(f"message already exists: {message_id}") from exc
            self._append_event_conn(
                conn,
                session_id,
                "message.created",
                {"message_id": message_id, "turn_id": turn_id, "role": role, "content": content},
                created_at=now,
            )
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return self._message_from_row(row)

    async def list_messages(
        self,
        session_id: str,
        *,
        after_created_at: str | None = None,
        limit: int = 1000,
        client_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_messages_sync, session_id, after_created_at, limit, client_id
        )

    def _list_messages_sync(
        self,
        session_id: str,
        after_created_at: str | None,
        limit: int,
        client_id: str | None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._require_session_conn(conn, session_id)
            if client_id is not None:
                self._require_session_access_conn(conn, client_id, session_id)
            if after_created_at is None:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id=? ORDER BY created_at,id LIMIT ?",
                    (session_id, max(1, min(limit, 5000))),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM messages WHERE session_id=? AND created_at>?
                    ORDER BY created_at,id LIMIT ?
                    """,
                    (session_id, after_created_at, max(1, min(limit, 5000))),
                ).fetchall()
        return [self._message_from_row(row) for row in rows]

    async def create_turn(
        self,
        session_id: str,
        request_id: str,
        idempotency_key: str,
        *,
        input_message_id: str | None = None,
        turn_id: str | None = None,
        phase: str = "model",
        checkpoint: Mapping[str, Any] | None = None,
        side_effect_state: str = "none",
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._create_turn_sync,
                session_id,
                request_id,
                idempotency_key,
                input_message_id,
                turn_id,
                phase,
                dict(checkpoint or {}),
                side_effect_state,
                False,
            )

    async def create_resume_turn(
        self,
        session_id: str,
        request_id: str,
        idempotency_key: str,
        *,
        input_message_id: str | None = None,
        turn_id: str | None = None,
        checkpoint: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Atomically start an explicitly requested turn from interrupted state."""

        async with self._lock:
            return await asyncio.to_thread(
                self._create_turn_sync,
                session_id,
                request_id,
                idempotency_key,
                input_message_id,
                turn_id,
                "resume",
                dict(checkpoint or {}),
                "none",
                True,
            )

    def _create_turn_sync(
        self,
        session_id: str,
        request_id: str,
        idempotency_key: str,
        input_message_id: str | None,
        turn_id: str | None,
        phase: str,
        checkpoint: dict[str, Any],
        side_effect_state: str,
        resume_interrupted: bool,
    ) -> dict[str, Any]:
        turn_id = turn_id or str(uuid.uuid4())
        with self._connect() as conn:
            session = self._require_session_conn(conn, session_id)
            replay = conn.execute(
                "SELECT * FROM turns WHERE session_id=? AND idempotency_key=?",
                (session_id, idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["request_id"] != request_id or replay["input_message_id"] != input_message_id:
                    raise LiliesConflictError("idempotency key reused with a different turn payload")
                result = self._turn_from_row(replay)
                result["replayed"] = True
                return result
            allowed_statuses = {"interrupted"} if resume_interrupted else {"ready", "error"}
            if session["status"] not in allowed_statuses:
                raise LiliesConflictError(
                    f"session {session_id} cannot start a turn from {session['status']}"
                )
            active = conn.execute(
                """
                SELECT id FROM turns WHERE session_id=?
                AND status IN ('running','waiting_permission','waiting_collaboration') LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if active is not None:
                raise LiliesConflictError(f"session already has active turn {active['id']}")
            if input_message_id is not None:
                row = conn.execute(
                    "SELECT session_id FROM messages WHERE id=?", (input_message_id,)
                ).fetchone()
                if row is None or row["session_id"] != session_id:
                    raise LiliesNotFoundError(f"input message not found in session: {input_message_id}")
            now = utc_now()
            conn.execute(
                """
                INSERT INTO turns(
                  id,session_id,request_id,idempotency_key,input_message_id,status,phase,
                  checkpoint_json,side_effect_state,created_at,updated_at
                ) VALUES (?,?,?,?,?,'running',?,?,?,?,?)
                """,
                (
                    turn_id,
                    session_id,
                    request_id,
                    idempotency_key,
                    input_message_id,
                    phase,
                    _json_dump(checkpoint),
                    side_effect_state,
                    now,
                    now,
                ),
            )
            self._transition_session_conn(conn, session_id, "running", expected_status=session["status"])
            self._append_event_conn(
                conn,
                session_id,
                "turn.started",
                {"turn_id": turn_id, "request_id": request_id, "phase": phase},
                created_at=now,
            )
            row = self._require_turn_conn(conn, turn_id)
        result = self._turn_from_row(row)
        result["replayed"] = False
        return result

    async def update_turn_checkpoint(
        self,
        turn_id: str,
        *,
        phase: str,
        checkpoint: Mapping[str, Any],
        side_effect_state: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._update_turn_checkpoint_sync,
                turn_id,
                phase,
                dict(checkpoint),
                side_effect_state,
            )

    def _update_turn_checkpoint_sync(
        self,
        turn_id: str,
        phase: str,
        checkpoint: dict[str, Any],
        side_effect_state: str | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            row = self._require_turn_conn(conn, turn_id)
            if row["status"] not in {"running", "waiting_permission", "waiting_collaboration"}:
                raise LiliesConflictError(f"cannot checkpoint finished turn {turn_id}")
            if side_effect_state is None:
                conn.execute(
                    "UPDATE turns SET phase=?,checkpoint_json=?,updated_at=? WHERE id=?",
                    (phase, _json_dump(checkpoint), now, turn_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE turns SET phase=?,checkpoint_json=?,side_effect_state=?,updated_at=?
                    WHERE id=?
                    """,
                    (phase, _json_dump(checkpoint), side_effect_state, now, turn_id),
                )
            self._append_event_conn(
                conn,
                row["session_id"],
                "turn.checkpointed",
                {"turn_id": turn_id, "phase": phase, "side_effect_state": side_effect_state},
                created_at=now,
            )
            return self._turn_from_row(self._require_turn_conn(conn, turn_id))

    async def finish_turn(
        self,
        turn_id: str,
        status: str,
        *,
        session_status: str | None = None,
        token_count: int = 0,
        cost_usd: float = 0.0,
        tool_count: int = 0,
        model_call_count: int = 0,
        error: str | None = None,
        interruption_reason: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._finish_turn_sync,
                turn_id,
                status,
                session_status,
                token_count,
                cost_usd,
                tool_count,
                model_call_count,
                error,
                interruption_reason,
            )

    def _finish_turn_sync(
        self,
        turn_id: str,
        status: str,
        session_status: str | None,
        token_count: int,
        cost_usd: float,
        tool_count: int,
        model_call_count: int,
        error: str | None,
        interruption_reason: str | None,
    ) -> dict[str, Any]:
        if status not in {"completed", "error", "cancelled", "interrupted"}:
            raise ValueError(f"invalid terminal turn status: {status}")
        default_session_status = {
            "completed": "ready",
            "error": "error",
            "cancelled": "cancelled",
            "interrupted": "interrupted",
        }[status]
        target_status = session_status or default_session_status
        now = utc_now()
        with self._connect() as conn:
            turn = self._require_turn_conn(conn, turn_id)
            if turn["status"] not in {"running", "waiting_permission", "waiting_collaboration"}:
                raise LiliesConflictError(f"turn already finished: {turn_id}")
            should_settle = turn["metrics_settled_at"] is None
            published = {
                "token_count": token_count if should_settle else turn["token_count"],
                "cost_usd": cost_usd if should_settle else turn["cost_usd"],
                "tool_count": tool_count if should_settle else turn["tool_count"],
                "model_call_count": (
                    model_call_count if should_settle else turn["model_call_count"]
                ),
            }
            conn.execute(
                """
                UPDATE turns SET status=?,phase=?,token_count=?,cost_usd=?,tool_count=?,
                  model_call_count=?,error=?,interruption_reason=?,
                  metrics_settled_at=COALESCE(metrics_settled_at,?),updated_at=?,finished_at=?
                WHERE id=?
                """,
                (
                    status,
                    status,
                    published["token_count"],
                    published["cost_usd"],
                    published["tool_count"],
                    published["model_call_count"],
                    error,
                    interruption_reason,
                    now,
                    now,
                    now,
                    turn_id,
                ),
            )
            if should_settle:
                conn.execute(
                    """
                    UPDATE sessions SET token_count=token_count+?,cost_usd=cost_usd+?,
                      tool_count=tool_count+?,model_call_count=model_call_count+?,updated_at=?
                    WHERE id=?
                    """,
                    (
                        published["token_count"],
                        published["cost_usd"],
                        published["tool_count"],
                        published["model_call_count"],
                        now,
                        turn["session_id"],
                    ),
                )
            self._transition_session_conn(conn, turn["session_id"], target_status)
            self._append_event_conn(
                conn,
                turn["session_id"],
                "turn.finished",
                {
                    "turn_id": turn_id,
                    "status": status,
                    "error": error,
                    "interruption_reason": interruption_reason,
                },
                created_at=now,
            )
            return self._turn_from_row(self._require_turn_conn(conn, turn_id))

    async def cancel_active_turn(
        self,
        turn_id: str,
        *,
        reason: str,
        session_status: str = "cancelled",
        token_count: int | None = None,
        cost_usd: float | None = None,
        tool_count: int | None = None,
        model_call_count: int | None = None,
    ) -> dict[str, Any]:
        """Cancel any active turn and publish its durable checkpoint metrics exactly once."""

        if session_status not in {"cancelled", "interrupted"}:
            raise ValueError("cancelled turns require a cancelled or interrupted session")
        async with self._lock:
            return await asyncio.to_thread(
                self._cancel_active_turn_sync,
                turn_id,
                reason,
                session_status,
                token_count,
                cost_usd,
                tool_count,
                model_call_count,
            )

    async def cancel_turn_for_stop(
        self,
        turn_id: str,
        *,
        reason: str,
        token_count: int | None = None,
        cost_usd: float | None = None,
        tool_count: int | None = None,
        model_call_count: int | None = None,
    ) -> dict[str, Any]:
        """Compatibility wrapper that leaves the cancelled session resumable."""

        async with self._lock:
            return await asyncio.to_thread(
                self._cancel_active_turn_sync,
                turn_id,
                reason,
                "interrupted",
                token_count,
                cost_usd,
                tool_count,
                model_call_count,
            )

    def _cancel_active_turn_sync(
        self,
        turn_id: str,
        reason: str,
        target_session_status: str,
        token_count: int | None,
        cost_usd: float | None,
        tool_count: int | None,
        model_call_count: int | None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            turn = self._require_turn_conn(conn, turn_id)
            session = self._require_session_conn(conn, turn["session_id"])
            if turn["status"] == "cancelled" and session["status"] == target_session_status:
                cancelled_permission = conn.execute(
                    """
                    SELECT id FROM permission_requests
                    WHERE turn_id=? AND status='cancelled' ORDER BY resolved_at DESC LIMIT 1
                    """,
                    (turn_id,),
                ).fetchone()
                return {
                    "turn": self._turn_from_row(turn),
                    "session": self._session_from_row(session),
                    "cancelled_permission_id": (
                        str(cancelled_permission["id"])
                        if cancelled_permission is not None
                        else None
                    ),
                    "replayed": True,
                }
            if turn["status"] not in {
                "running",
                "waiting_permission",
                "waiting_collaboration",
            }:
                raise LiliesConflictError(f"turn is not active: {turn_id}")
            checkpoint_metrics = _checkpoint_metrics(turn["checkpoint_json"])
            should_settle = turn["metrics_settled_at"] is None
            requested_metrics = {
                "token_count": (
                    checkpoint_metrics["token_count"]
                    if token_count is None
                    else _non_negative_int(token_count)
                ),
                "cost_usd": (
                    checkpoint_metrics["cost_usd"]
                    if cost_usd is None
                    else _non_negative_float(cost_usd)
                ),
                "tool_count": (
                    checkpoint_metrics["tool_count"]
                    if tool_count is None
                    else _non_negative_int(tool_count)
                ),
                "model_call_count": (
                    checkpoint_metrics["model_call_count"]
                    if model_call_count is None
                    else _non_negative_int(model_call_count)
                ),
            }
            published_metrics = requested_metrics if should_settle else {
                "token_count": turn["token_count"],
                "cost_usd": turn["cost_usd"],
                "tool_count": turn["tool_count"],
                "model_call_count": turn["model_call_count"],
            }
            pending = conn.execute(
                """
                SELECT * FROM permission_requests
                WHERE turn_id=? AND status='pending' ORDER BY created_at LIMIT 1
                """,
                (turn_id,),
            ).fetchone()
            cancelled_permission_id: str | None = None
            if pending is not None:
                cancelled_permission_id = str(pending["id"])
                conn.execute(
                    """
                    UPDATE permission_requests
                    SET status='cancelled',decision_message=?,resolved_at=?
                    WHERE id=? AND status='pending'
                    """,
                    (reason, now, cancelled_permission_id),
                )
                self._append_event_conn(
                    conn,
                    turn["session_id"],
                    "permission.resolved",
                    {
                        "request_id": cancelled_permission_id,
                        "turn_id": turn_id,
                        "decision": "cancelled",
                        "reason": reason,
                    },
                    created_at=now,
                )
            conn.execute(
                """
                UPDATE turns SET status='cancelled',phase='cancelled',token_count=?,cost_usd=?,
                  tool_count=?,model_call_count=?,interruption_reason=?,
                  metrics_settled_at=COALESCE(metrics_settled_at,?),updated_at=?,finished_at=?
                WHERE id=?
                """,
                (
                    published_metrics["token_count"],
                    published_metrics["cost_usd"],
                    published_metrics["tool_count"],
                    published_metrics["model_call_count"],
                    reason,
                    now,
                    now,
                    now,
                    turn_id,
                ),
            )
            if should_settle:
                conn.execute(
                    """
                    UPDATE sessions SET token_count=token_count+?,cost_usd=cost_usd+?,
                      tool_count=tool_count+?,model_call_count=model_call_count+?,
                      waiting_permission_id=NULL,waiting_collaboration_id=NULL,updated_at=?
                    WHERE id=?
                    """,
                    (
                        published_metrics["token_count"],
                        published_metrics["cost_usd"],
                        published_metrics["tool_count"],
                        published_metrics["model_call_count"],
                        now,
                        turn["session_id"],
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE sessions SET waiting_permission_id=NULL,
                      waiting_collaboration_id=NULL,updated_at=? WHERE id=?
                    """,
                    (now, turn["session_id"]),
                )
            self._transition_session_conn(
                conn,
                turn["session_id"],
                target_session_status,
                reason=reason,
                expected_status=session["status"],
            )
            self._append_event_conn(
                conn,
                turn["session_id"],
                "turn.finished",
                {
                    "turn_id": turn_id,
                    "status": "cancelled",
                    "interruption_reason": reason,
                    "metrics_settled": should_settle,
                },
                created_at=now,
            )
            updated_turn = self._require_turn_conn(conn, turn_id)
            updated_session = self._require_session_conn(conn, turn["session_id"])
        return {
            "turn": self._turn_from_row(updated_turn),
            "session": self._session_from_row(updated_session),
            "cancelled_permission_id": cancelled_permission_id,
            "replayed": False,
        }

    async def get_turn(self, turn_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_turn_sync, turn_id)

    def _get_turn_sync(self, turn_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._turn_from_row(self._require_turn_conn(conn, turn_id))

    async def list_turns(self, session_id: str) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_turns_sync, session_id)

    def _list_turns_sync(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._require_session_conn(conn, session_id)
            rows = conn.execute(
                "SELECT * FROM turns WHERE session_id=? ORDER BY created_at,id", (session_id,)
            ).fetchall()
        return [self._turn_from_row(row) for row in rows]

    # Permissions --------------------------------------------------------------

    async def create_permission_request(
        self,
        session_id: str,
        turn_id: str,
        tool_name: str,
        input_digest: str,
        *,
        tool_call_id: str | None = None,
        tool_input: Mapping[str, Any] | None = None,
        checkpoint: Mapping[str, Any] | None = None,
        input_summary: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._create_permission_request_sync,
                session_id,
                turn_id,
                tool_name,
                input_digest,
                tool_call_id,
                dict(tool_input or {}),
                dict(checkpoint or {}),
                dict(input_summary or {}),
                request_id,
            )

    def _create_permission_request_sync(
        self,
        session_id: str,
        turn_id: str,
        tool_name: str,
        input_digest: str,
        tool_call_id: str | None,
        tool_input: dict[str, Any],
        checkpoint: dict[str, Any],
        input_summary: dict[str, Any],
        request_id: str | None,
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        tool_call_id = tool_call_id or request_id
        canonical_input_digest = _payload_hash(tool_input)
        if not hmac.compare_digest(canonical_input_digest, input_digest):
            raise LiliesConflictError(
                "permission input digest does not bind the canonical tool input"
            )
        now = utc_now()
        with self._connect() as conn:
            session = self._require_session_conn(conn, session_id)
            turn = self._require_turn_conn(conn, turn_id, session_id=session_id)
            existing = conn.execute(
                "SELECT * FROM permission_requests WHERE session_id=? AND status='pending'",
                (session_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["turn_id"] == turn_id
                    and existing["tool_call_id"] == tool_call_id
                    and existing["tool_name"] == tool_name
                    and existing["input_digest"] == input_digest
                    and _json_load(existing["tool_input_json"], {}) == tool_input
                ):
                    return self._permission_from_row(existing)
                raise LiliesConflictError(
                    f"session already waits for permission {existing['id']}"
                )
            if session["status"] != "running" or turn["status"] != "running":
                raise LiliesConflictError("permission can only pause an active running turn")
            stored_checkpoint = checkpoint or _json_load(turn["checkpoint_json"], {})
            conn.execute(
                """
                INSERT INTO permission_requests(
                  id,session_id,turn_id,tool_call_id,tool_name,input_digest,
                  tool_input_json,checkpoint_json,
                  input_summary_json,status,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,'pending',?)
                """,
                (
                    request_id,
                    session_id,
                    turn_id,
                    tool_call_id,
                    tool_name,
                    input_digest,
                    _json_dump(tool_input),
                    _json_dump(stored_checkpoint),
                    _json_dump(input_summary),
                    now,
                ),
            )
            conn.execute(
                "UPDATE turns SET status='waiting_permission',phase='waiting_permission',updated_at=? WHERE id=?",
                (now, turn_id),
            )
            conn.execute(
                "UPDATE sessions SET waiting_permission_id=?,updated_at=? WHERE id=?",
                (request_id, now, session_id),
            )
            self._transition_session_conn(
                conn, session_id, "waiting_permission", expected_status="running"
            )
            self._append_event_conn(
                conn,
                session_id,
                "permission.requested",
                {
                    "request_id": request_id,
                    "turn_id": turn_id,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "input_digest": input_digest,
                    "input_summary": input_summary,
                },
                created_at=now,
            )
            row = conn.execute(
                "SELECT * FROM permission_requests WHERE id=?", (request_id,)
            ).fetchone()
        return self._permission_from_row(row)

    async def resolve_permission_request(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        *,
        client_id: str | None = None,
        expected_input_digest: str | None = None,
        updated_input: Mapping[str, Any] | None = None,
        message: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._resolve_permission_request_sync,
                session_id,
                request_id,
                decision,
                client_id,
                expected_input_digest,
                dict(updated_input) if updated_input is not None else None,
                message,
                idempotency_key,
            )

    def _resolve_permission_request_sync(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        client_id: str | None,
        expected_input_digest: str | None,
        updated_input: dict[str, Any] | None,
        message: str | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        aliases = {"allow": "allowed", "deny": "denied", "cancel": "cancelled"}
        decision = aliases.get(decision, decision)
        if decision not in {"allowed", "denied", "cancelled"}:
            raise ValueError(f"invalid permission decision: {decision}")
        if decision != "allowed" and updated_input is not None:
            raise ValueError("updated_input is only valid for an allow decision")
        if decision in {"allowed", "denied"} and expected_input_digest is None:
            raise ValueError("expected_input_digest is required for a permission decision")
        decision_payload = {
            "decision": decision,
            "expected_input_digest": expected_input_digest,
            "updated_input": updated_input,
            "message": message,
        }
        decision_payload_hash = hashlib.sha256(
            _json_dump(decision_payload).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self._connect() as conn:
            if client_id is not None:
                client = self._require_client_conn(conn, client_id)
                scopes = set(_json_load(client["scopes_json"], []))
                if "lilies.permission:resolve" not in scopes:
                    raise LiliesAccessDeniedError("client lacks lilies.permission:resolve")
                self._require_session_access_conn(conn, client_id, session_id)
            row = conn.execute(
                "SELECT * FROM permission_requests WHERE id=? AND session_id=?",
                (request_id, session_id),
            ).fetchone()
            if row is None:
                raise LiliesNotFoundError(f"permission request not found: {request_id}")
            original_input = _json_load(row["tool_input_json"], {})
            canonical_original_digest = _payload_hash(original_input)
            if not hmac.compare_digest(canonical_original_digest, str(row["input_digest"])):
                raise LiliesConflictError("stored permission request input digest mismatch")
            if row["status"] == "allowed":
                approved_input = _json_load(row["decision_input_json"], None)
                approved_digest = row["decision_input_digest"]
                if (
                    not isinstance(approved_input, dict)
                    or approved_digest is None
                    or not hmac.compare_digest(
                        _payload_hash(approved_input), str(approved_digest)
                    )
                ):
                    raise LiliesConflictError("stored approved permission input digest mismatch")
            if row["status"] != "pending":
                if (
                    idempotency_key is not None
                    and row["decision_idempotency_key"] == idempotency_key
                    and hmac.compare_digest(
                        str(row["decision_payload_hash"]), decision_payload_hash
                    )
                ):
                    result = self._permission_from_row(row)
                    result["replayed"] = True
                    return result
                raise LiliesConflictError(f"permission request already resolved: {request_id}")
            if expected_input_digest is not None and expected_input_digest != row["input_digest"]:
                raise LiliesConflictError("permission input digest changed before decision")
            approved_input = updated_input if updated_input is not None else original_input
            approved_digest = _payload_hash(approved_input) if decision == "allowed" else None
            conn.execute(
                """
                UPDATE permission_requests
                SET status=?,decision_client_id=?,decision_input_json=?,
                    decision_input_digest=?,decision_message=?,
                    decision_idempotency_key=?,
                    decision_payload_hash=?,resolved_at=?
                WHERE id=? AND status='pending'
                """,
                (
                    decision,
                    client_id,
                    _json_dump(approved_input) if decision == "allowed" else None,
                    approved_digest,
                    message,
                    idempotency_key,
                    decision_payload_hash,
                    now,
                    request_id,
                ),
            )
            if decision == "cancelled":
                conn.execute(
                    "UPDATE turns SET status='cancelled',phase='cancelled',updated_at=?,finished_at=? WHERE id=?",
                    (now, now, row["turn_id"]),
                )
                target_status = "cancelled"
            else:
                conn.execute(
                    "UPDATE turns SET status='running',phase='tool_result',updated_at=? WHERE id=?",
                    (now, row["turn_id"]),
                )
                target_status = "running"
            conn.execute(
                "UPDATE sessions SET waiting_permission_id=NULL,updated_at=? WHERE id=?",
                (now, session_id),
            )
            self._transition_session_conn(
                conn,
                session_id,
                target_status,
                expected_status="waiting_permission",
            )
            self._append_event_conn(
                conn,
                session_id,
                "permission.resolved",
                {
                    "request_id": request_id,
                    "turn_id": row["turn_id"],
                    "decision": decision,
                    "decision_client_id": client_id,
                    "original_input_digest": row["input_digest"],
                    "approved_input_digest": approved_digest,
                },
                created_at=now,
            )
            result = conn.execute(
                "SELECT * FROM permission_requests WHERE id=?", (request_id,)
            ).fetchone()
        response = self._permission_from_row(result)
        response["replayed"] = False
        return response

    async def get_permission_request(self, request_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_permission_request_sync, request_id)

    def _get_permission_request_sync(self, request_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM permission_requests WHERE id=?", (request_id,)
            ).fetchone()
        if row is None:
            raise LiliesNotFoundError(f"permission request not found: {request_id}")
        return self._permission_from_row(row)

    async def list_pending_permission_requests(
        self, *, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_pending_permissions_sync, session_id)

    def _list_pending_permissions_sync(
        self, session_id: str | None
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if session_id is None:
                rows = conn.execute(
                    "SELECT * FROM permission_requests WHERE status='pending' ORDER BY created_at,id"
                ).fetchall()
            else:
                self._require_session_conn(conn, session_id)
                rows = conn.execute(
                    """
                    SELECT * FROM permission_requests
                    WHERE session_id=? AND status='pending' ORDER BY created_at,id
                    """,
                    (session_id,),
                ).fetchall()
        return [self._permission_from_row(row) for row in rows]

    # Pairing, clients, ACL -----------------------------------------------------

    async def create_pairing_code(
        self,
        *,
        ttl_seconds: int = 600,
        allowed_scopes: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("pairing code ttl must be between 1 and 3600 seconds")
        scopes = sorted(
            self.allowed_scopes if allowed_scopes is None else set(allowed_scopes)
        )
        unknown_scopes = sorted(set(scopes) - self.allowed_scopes)
        if unknown_scopes:
            raise ValueError(f"unknown pairing-code scopes: {unknown_scopes}")
        if not scopes:
            raise ValueError("a pairing code must allow at least one scope")
        async with self._lock:
            return await asyncio.to_thread(self._create_pairing_code_sync, ttl_seconds, scopes)

    def _create_pairing_code_sync(
        self, ttl_seconds: int, allowed_scopes: list[str]
    ) -> dict[str, Any]:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code_id = "".join(secrets.choice(alphabet) for _ in range(12))
        secret_value = "".join(secrets.choice(alphabet) for _ in range(24))
        pairing_code = f"{code_id}-{secret_value}"
        salt = secrets.token_bytes(16)
        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(seconds=ttl_seconds)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pairing_codes(
                  id,salt_hex,digest,allowed_scopes_json,created_at,expires_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    code_id,
                    salt.hex(),
                    _digest(pairing_code, salt),
                    _json_dump(allowed_scopes),
                    created_at.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            self._security_event_conn(
                conn,
                "pairing.code_created",
                {
                    "code_id": code_id,
                    "allowed_scopes": allowed_scopes,
                    "expires_at": expires_at.isoformat(),
                },
            )
        return {
            "code_id": code_id,
            "pairing_code": pairing_code,
            "allowed_scopes": allowed_scopes,
            "expires_at": expires_at.isoformat(),
        }

    async def exchange_pairing_code(
        self,
        pairing_code: str,
        client_name: str,
        requested_scopes: Sequence[str],
        client_nonce: str,
        daemon_fingerprint: str,
        *,
        token_ttl_seconds: int | None = None,
        previous_client_id: str | None = None,
        previous_access_token: str | None = None,
    ) -> dict[str, Any]:
        if (previous_client_id is None) != (previous_access_token is None):
            raise ValueError(
                "previous_client_id and previous_access_token must be provided together"
            )
        async with self._lock:
            return await asyncio.to_thread(
                self._exchange_pairing_code_sync,
                pairing_code,
                client_name,
                list(requested_scopes),
                client_nonce,
                daemon_fingerprint,
                token_ttl_seconds,
                previous_client_id,
                previous_access_token,
            )

    def _exchange_pairing_code_sync(
        self,
        pairing_code: str,
        client_name: str,
        requested_scopes: list[str],
        client_nonce: str,
        daemon_fingerprint: str,
        token_ttl_seconds: int | None,
        previous_client_id: str | None,
        previous_access_token: str | None,
    ) -> dict[str, Any]:
        if len(client_nonce.encode("utf-8")) < 16:
            self._record_pairing_rejection("nonce_too_short")
            raise LiliesAuthenticationError("client nonce must contain at least 16 bytes")
        unknown_scopes = sorted(set(requested_scopes) - self.allowed_scopes)
        if unknown_scopes:
            self._record_pairing_rejection("unknown_scope", {"unknown_scopes": unknown_scopes})
            raise LiliesAccessDeniedError(f"unknown requested scopes: {unknown_scopes}")
        try:
            code_id, _ = pairing_code.split("-", 1)
        except ValueError as exc:
            self._record_pairing_rejection("malformed_code")
            raise LiliesAuthenticationError("invalid pairing code") from exc
        nonce_digest = hashlib.sha256(client_nonce.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        with self._connect() as conn:
            replay = conn.execute(
                "SELECT id FROM pairing_codes WHERE client_nonce_digest=?", (nonce_digest,)
            ).fetchone()
            if replay is not None:
                self._security_event_conn(
                    conn,
                    "pairing.exchange_rejected",
                    {"reason": "nonce_replay", "code_id": code_id},
                )
                conn.commit()
                raise LiliesAuthenticationError("client nonce was already used")
            row = conn.execute("SELECT * FROM pairing_codes WHERE id=?", (code_id,)).fetchone()
            if row is None:
                self._security_event_conn(
                    conn,
                    "pairing.exchange_rejected",
                    {"reason": "unknown_code", "code_id": code_id},
                )
                conn.commit()
                raise LiliesAuthenticationError("invalid pairing code")
            supplied_digest = _digest(pairing_code, bytes.fromhex(row["salt_hex"]))
            if not hmac.compare_digest(supplied_digest, row["digest"]):
                self._security_event_conn(
                    conn,
                    "pairing.exchange_rejected",
                    {"reason": "invalid_code", "code_id": code_id},
                )
                conn.commit()
                raise LiliesAuthenticationError("invalid pairing code")
            if row["redeemed_at"] is not None:
                self._security_event_conn(
                    conn,
                    "pairing.exchange_rejected",
                    {"reason": "code_redeemed", "code_id": code_id},
                )
                conn.commit()
                raise LiliesAuthenticationError("pairing code was already redeemed")
            if datetime.fromisoformat(row["expires_at"]) <= now:
                self._security_event_conn(
                    conn,
                    "pairing.exchange_rejected",
                    {"reason": "code_expired", "code_id": code_id},
                )
                conn.commit()
                raise LiliesAuthenticationError("pairing code expired")
            scopes = sorted(set(requested_scopes) & set(_json_load(row["allowed_scopes_json"], [])))
            if not scopes:
                self._security_event_conn(
                    conn,
                    "pairing.exchange_rejected",
                    {"reason": "scope_not_granted", "code_id": code_id},
                )
                conn.commit()
                raise LiliesAccessDeniedError("pairing code grants none of the requested scopes")
            rotating_client: sqlite3.Row | None = None
            if previous_client_id is not None and previous_access_token is not None:
                rotating_client = conn.execute(
                    "SELECT * FROM clients WHERE id=?", (previous_client_id,)
                ).fetchone()
                if rotating_client is None:
                    supplied_previous_digest = _digest(
                        previous_access_token, bytes(16)
                    )
                    previous_token_matches = hmac.compare_digest(
                        supplied_previous_digest, "0" * len(supplied_previous_digest)
                    )
                else:
                    supplied_previous_digest = _digest(
                        previous_access_token,
                        bytes.fromhex(rotating_client["token_salt_hex"]),
                    )
                    previous_token_matches = hmac.compare_digest(
                        supplied_previous_digest,
                        str(rotating_client["token_digest"]),
                    )
                previous_proof_valid = bool(
                    rotating_client is not None
                    and previous_token_matches
                    and rotating_client["name"] == client_name
                    and rotating_client["revoked_at"] is None
                    and rotating_client["daemon_fingerprint"] == daemon_fingerprint
                )
                if not previous_proof_valid:
                    self._security_event_conn(
                        conn,
                        "pairing.exchange_rejected",
                        {
                            "reason": "invalid_previous_client_proof",
                            "code_id": code_id,
                            "previous_client_id": previous_client_id,
                        },
                    )
                    conn.commit()
                    raise LiliesAuthenticationError("previous client proof is invalid")
                client_id = previous_client_id
            else:
                client_id = str(uuid.uuid4())
            token_secret = secrets.token_urlsafe(32)
            access_token = f"{client_id}.{token_secret}"
            token_salt = secrets.token_bytes(16)
            if token_ttl_seconds is None:
                token_ttl_seconds = 30 * 24 * 3600 if client_name == "platform" else 24 * 3600
            if token_ttl_seconds <= 0:
                raise ValueError("client token ttl must be positive")
            expires_at = now + timedelta(seconds=token_ttl_seconds)
            if rotating_client is None:
                conn.execute(
                    """
                    INSERT INTO clients(
                      id,name,token_salt_hex,token_digest,scopes_json,daemon_fingerprint,
                      expires_at,created_at,last_seen_at
                    ) VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        client_id,
                        client_name,
                        token_salt.hex(),
                        _digest(access_token, token_salt),
                        _json_dump(scopes),
                        daemon_fingerprint,
                        expires_at.isoformat(),
                        now_text,
                        now_text,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE clients
                    SET token_salt_hex=?,token_digest=?,scopes_json=?,daemon_fingerprint=?,
                        expires_at=?,last_seen_at=?
                    WHERE id=? AND revoked_at IS NULL
                    """,
                    (
                        token_salt.hex(),
                        _digest(access_token, token_salt),
                        _json_dump(scopes),
                        daemon_fingerprint,
                        expires_at.isoformat(),
                        now_text,
                        client_id,
                    ),
                )
            conn.execute(
                """
                UPDATE pairing_codes
                SET redeemed_at=?,redeemed_by_client_id=?,client_nonce_digest=? WHERE id=?
                """,
                (now_text, client_id, nonce_digest, code_id),
            )
            self._security_event_conn(
                conn,
                "pairing.rotated" if rotating_client is not None else "pairing.exchanged",
                {"code_id": code_id, "client_id": client_id, "scopes": scopes},
                client_id=client_id,
            )
        return {
            "client_id": client_id,
            "access_token": access_token,
            "granted_scopes": scopes,
            "expires_at": expires_at.isoformat(),
            "daemon_fingerprint": daemon_fingerprint,
        }

    def _record_pairing_rejection(
        self, reason: str, details: Mapping[str, Any] | None = None
    ) -> None:
        with self._connect() as conn:
            self._security_event_conn(
                conn,
                "pairing.exchange_rejected",
                {"reason": reason, **dict(details or {})},
            )

    async def authenticate_client(
        self, access_token: str, *, required_scope: str | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._authenticate_client_sync, access_token, required_scope
            )

    def _authenticate_client_sync(
        self, access_token: str, required_scope: str | None
    ) -> dict[str, Any]:
        try:
            client_id, _ = access_token.split(".", 1)
        except ValueError as exc:
            raise LiliesAuthenticationError("invalid client token") from exc
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
            if row is None:
                raise LiliesAuthenticationError("invalid client token")
            supplied = _digest(access_token, bytes.fromhex(row["token_salt_hex"]))
            if not hmac.compare_digest(supplied, row["token_digest"]):
                self._security_event_conn(
                    conn,
                    "client.authentication_rejected",
                    {"reason": "invalid_token"},
                    client_id=client_id,
                )
                raise LiliesAuthenticationError("invalid client token")
            now = datetime.now(timezone.utc)
            if row["revoked_at"] is not None:
                raise LiliesAuthenticationError("client token revoked")
            if row["expires_at"] is not None and datetime.fromisoformat(row["expires_at"]) <= now:
                raise LiliesAuthenticationError("client token expired")
            scopes = set(_json_load(row["scopes_json"], []))
            if required_scope is not None and required_scope not in scopes:
                raise LiliesAccessDeniedError(f"client lacks {required_scope}")
            conn.execute(
                "UPDATE clients SET last_seen_at=? WHERE id=?", (now.isoformat(), client_id)
            )
            row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        return self._client_from_row(row)

    async def revoke_client(self, client_id: str) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._revoke_client_sync, client_id)

    def _revoke_client_sync(self, client_id: str) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            row = self._require_client_conn(conn, client_id)
            if row["revoked_at"] is None:
                conn.execute("UPDATE clients SET revoked_at=? WHERE id=?", (now, client_id))
                self._security_event_conn(
                    conn, "client.revoked", {"client_id": client_id}, client_id=client_id
                )
            return self._client_from_row(self._require_client_conn(conn, client_id))

    async def get_client(self, client_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_client_sync, client_id)

    def _get_client_sync(self, client_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            return self._client_from_row(self._require_client_conn(conn, client_id))

    async def list_clients(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_clients_sync)

    def _list_clients_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM clients ORDER BY created_at,id").fetchall()
        return [self._client_from_row(row) for row in rows]

    async def grant_session_access(self, client_id: str, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._grant_session_access_sync, client_id, session_id)

    def _grant_session_access_sync(self, client_id: str, session_id: str) -> None:
        with self._connect() as conn:
            self._require_client_conn(conn, client_id)
            self._require_session_conn(conn, session_id)
            conn.execute(
                """
                INSERT INTO client_session_acl(client_id,session_id,created_at)
                VALUES (?,?,?) ON CONFLICT(client_id,session_id) DO NOTHING
                """,
                (client_id, session_id, utc_now()),
            )

    async def revoke_session_access(self, client_id: str, session_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._revoke_session_access_sync, client_id, session_id)

    def _revoke_session_access_sync(self, client_id: str, session_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM client_session_acl WHERE client_id=? AND session_id=?",
                (client_id, session_id),
            )

    async def client_can_access(self, client_id: str, session_id: str) -> bool:
        return await asyncio.to_thread(self._client_can_access_sync, client_id, session_id)

    def _client_can_access_sync(self, client_id: str, session_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM client_session_acl WHERE client_id=? AND session_id=?",
                (client_id, session_id),
            ).fetchone()
        return row is not None

    # Events and acknowledgements ---------------------------------------------

    async def append_event(
        self, session_id: str, event_type: str, data: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Append a durable event without changing session state."""

        async with self._lock:
            return await asyncio.to_thread(
                self._append_event_sync, session_id, event_type, dict(data)
            )

    def _append_event_sync(
        self, session_id: str, event_type: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        with self._connect() as conn:
            self._require_session_conn(conn, session_id)
            seq = self._append_event_conn(conn, session_id, event_type, data)
            row = conn.execute(
                "SELECT * FROM events WHERE session_id=? AND seq=?", (session_id, seq)
            ).fetchone()
        return self._event_from_row(row)

    async def list_events(
        self,
        session_id: str,
        *,
        after: int = 0,
        limit: int = 1000,
        client_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            self._list_events_sync, session_id, after, limit, client_id
        )

    def _list_events_sync(
        self,
        session_id: str,
        after: int,
        limit: int,
        client_id: str | None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            self._require_session_conn(conn, session_id)
            if client_id is not None:
                self._require_session_access_conn(conn, client_id, session_id)
            rows = conn.execute(
                """
                SELECT * FROM events WHERE session_id=? AND seq>?
                ORDER BY seq LIMIT ?
                """,
                (session_id, max(0, after), max(1, min(limit, 5000))),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    async def subscribe_events(
        self,
        session_id: str,
        *,
        after: int = 0,
        client_id: str | None = None,
        poll_interval: float = 0.1,
    ) -> AsyncIterator[dict[str, Any]]:
        """Replay then follow the durable stream; cancellation stops polling."""

        cursor = max(0, after)
        while True:
            events = await self.list_events(
                session_id, after=cursor, limit=1000, client_id=client_id
            )
            if events:
                for event in events:
                    cursor = int(event["seq"])
                    yield event
                continue
            await asyncio.sleep(max(0.01, poll_interval))

    async def ack_events(self, client_id: str, session_id: str, cursor: int) -> dict[str, Any]:
        if cursor < 0:
            raise ValueError("event cursor cannot be negative")
        async with self._lock:
            return await asyncio.to_thread(
                self._ack_events_sync, client_id, session_id, cursor
            )

    def _ack_events_sync(
        self, client_id: str, session_id: str, cursor: int
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            self._require_session_access_conn(conn, client_id, session_id)
            highest = int(
                conn.execute(
                    "SELECT COALESCE(MAX(seq),0) AS seq FROM events WHERE session_id=?",
                    (session_id,),
                ).fetchone()["seq"]
            )
            if cursor > highest:
                raise LiliesConflictError(
                    f"cannot acknowledge cursor {cursor}; stream ends at {highest}"
                )
            conn.execute(
                """
                INSERT INTO reader_acks(client_id,session_id,cursor,updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(client_id,session_id) DO UPDATE SET
                  cursor=MAX(reader_acks.cursor,excluded.cursor),updated_at=excluded.updated_at
                """,
                (client_id, session_id, cursor, now),
            )
            row = conn.execute(
                "SELECT * FROM reader_acks WHERE client_id=? AND session_id=?",
                (client_id, session_id),
            ).fetchone()
        return dict(row)

    async def get_ack(self, client_id: str, session_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_ack_sync, client_id, session_id)

    def _get_ack_sync(self, client_id: str, session_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            self._require_session_access_conn(conn, client_id, session_id)
            row = conn.execute(
                "SELECT * FROM reader_acks WHERE client_id=? AND session_id=?",
                (client_id, session_id),
            ).fetchone()
        if row is None:
            return {"client_id": client_id, "session_id": session_id, "cursor": 0, "updated_at": None}
        return dict(row)

    # Private credential references -------------------------------------------

    async def provision_credential(
        self,
        kind: str,
        value: str,
        *,
        scopes: Sequence[str] | None = None,
        idempotency_key: str | None = None,
        credential_ref: str | None = None,
        client_id: str | None = None,
        assignment_id: str | None = None,
        expires_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        normalised_scopes = sorted(set(scopes or []))
        if idempotency_key is not None and credential_ref is None:
            raise ValueError("credential_ref is required for idempotent provisioning")
        async with self._lock:
            return await asyncio.to_thread(
                self._provision_credential_sync,
                kind,
                value,
                normalised_scopes,
                idempotency_key,
                credential_ref,
                client_id,
                assignment_id,
                _normalise_datetime(expires_at),
            )

    def _provision_credential_sync(
        self,
        kind: str,
        value: str,
        scopes: list[str],
        idempotency_key: str | None,
        credential_ref: str | None,
        client_id: str | None,
        assignment_id: str | None,
        expires_at: str | None,
    ) -> dict[str, Any]:
        credential_ref = credential_ref or f"cred_{uuid.uuid4()}"
        provision_hash = _payload_hash(
            {
                "assignment_id": assignment_id,
                "client_id": client_id,
                "credential_ref": credential_ref,
                "expires_at": expires_at,
                "kind": kind,
                "scopes": scopes,
                "secret_digest": "sha256:"
                + hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
        )
        now = utc_now()
        with self._connect() as conn:
            if client_id is not None:
                self._require_client_conn(conn, client_id)
            if idempotency_key is not None:
                replay = conn.execute(
                    """
                    SELECT * FROM credentials
                    WHERE provision_idempotency_key=?
                      AND ((client_id IS NULL AND ? IS NULL) OR client_id=?)
                    """,
                    (idempotency_key, client_id, client_id),
                ).fetchone()
                if replay is not None:
                    if hmac.compare_digest(
                        str(replay["provision_payload_hash"]), provision_hash
                    ):
                        return self._credential_metadata(replay, replayed=True)
                    raise LiliesConflictError(
                        "credential provision idempotency key reused with different payload"
                    )
            try:
                conn.execute(
                    """
                    INSERT INTO credentials(
                      ref,name,kind,scopes_json,secret_value,client_id,assignment_id,expires_at,
                      provision_idempotency_key,provision_payload_hash,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        credential_ref,
                        kind,
                        kind,
                        _json_dump(scopes),
                        value,
                        client_id,
                        assignment_id,
                        expires_at,
                        idempotency_key,
                        provision_hash,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LiliesConflictError(f"credential already exists: {credential_ref}") from exc
            self._security_event_conn(
                conn,
                "credential.provisioned",
                {
                    "credential_ref": credential_ref,
                    "kind": kind,
                    "scopes": scopes,
                    "assignment_id": assignment_id,
                },
                client_id=client_id,
            )
            row = conn.execute(
                "SELECT * FROM credentials WHERE ref=?", (credential_ref,)
            ).fetchone()
        return self._credential_metadata(row, replayed=False)

    async def get_credential(
        self, credential_ref: str, *, assignment_id: str | None = None
    ) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_credential_sync, credential_ref, assignment_id
        )

    def _get_credential_sync(
        self, credential_ref: str, assignment_id: str | None
    ) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM credentials WHERE ref=?", (credential_ref,)
            ).fetchone()
        if row is None:
            raise LiliesNotFoundError(f"credential not found: {credential_ref}")
        if row["revoked_at"] is not None:
            raise LiliesAuthenticationError("credential revoked")
        if row["expires_at"] is not None and datetime.fromisoformat(row["expires_at"]) <= datetime.now(
            timezone.utc
        ):
            raise LiliesAuthenticationError("credential expired")
        if row["assignment_id"] is not None and row["assignment_id"] != assignment_id:
            raise LiliesAccessDeniedError("credential is bound to another assignment")
        result = self._credential_metadata(row)
        result["value"] = row["secret_value"]
        return result

    async def list_credentials(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_credentials_sync)

    def _list_credentials_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM credentials ORDER BY created_at,ref").fetchall()
        return [self._credential_metadata(row) for row in rows]

    async def revoke_credential(
        self,
        credential_ref: str,
        *,
        idempotency_key: str | None = None,
        reason: str = "revoked",
    ) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(
                self._revoke_credential_sync,
                credential_ref,
                idempotency_key,
                reason,
            )

    def _revoke_credential_sync(
        self,
        credential_ref: str,
        idempotency_key: str | None,
        reason: str,
    ) -> dict[str, Any]:
        now = utc_now()
        revoke_hash = _payload_hash(
            {
                "credential_ref": credential_ref,
                "reason": reason,
            }
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM credentials WHERE ref=?", (credential_ref,)
            ).fetchone()
            if row is None:
                raise LiliesNotFoundError(f"credential not found: {credential_ref}")
            if idempotency_key is not None:
                replay = conn.execute(
                    """
                    SELECT * FROM credentials
                    WHERE revoke_idempotency_key=?
                      AND ((client_id IS NULL AND ? IS NULL) OR client_id=?)
                    """,
                    (idempotency_key, row["client_id"], row["client_id"]),
                ).fetchone()
                if replay is not None:
                    if replay["ref"] == credential_ref and hmac.compare_digest(
                        str(replay["revoke_payload_hash"]), revoke_hash
                    ):
                        return self._credential_metadata(replay, replayed=True)
                    raise LiliesConflictError(
                        "credential revoke idempotency key reused with different payload"
                    )
            if row["revoked_at"] is not None:
                if idempotency_key is None:
                    return self._credential_metadata(row, replayed=True)
                raise LiliesConflictError("credential was revoked by a different request")
            conn.execute(
                """
                UPDATE credentials
                SET revoked_at=?,updated_at=?,revoke_idempotency_key=?,
                    revoke_payload_hash=?,revoke_reason=?
                WHERE ref=? AND revoked_at IS NULL
                """,
                (now, now, idempotency_key, revoke_hash, reason, credential_ref),
            )
            self._security_event_conn(
                conn,
                "credential.revoked",
                {
                    "credential_ref": credential_ref,
                    "assignment_id": row["assignment_id"],
                    "kind": row["kind"],
                    "reason": reason,
                },
                client_id=row["client_id"],
            )
            updated = conn.execute(
                "SELECT * FROM credentials WHERE ref=?", (credential_ref,)
            ).fetchone()
        return self._credential_metadata(updated, replayed=False)

    async def list_security_events(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._list_security_events_sync)

    async def append_security_event(
        self,
        event_type: str,
        details: Mapping[str, Any],
        *,
        client_id: str | None = None,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._append_security_event_sync,
                event_type,
                dict(details),
                client_id,
            )

    def _append_security_event_sync(
        self,
        event_type: str,
        details: dict[str, Any],
        client_id: str | None,
    ) -> None:
        with self._connect() as conn:
            if client_id is not None:
                self._require_client_conn(conn, client_id)
            self._security_event_conn(
                conn,
                event_type,
                details,
                client_id=client_id,
            )

    def _list_security_events_sync(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM security_events ORDER BY seq").fetchall()
        return [
            {
                "seq": row["seq"],
                "event_type": row["event_type"],
                "client_id": row["client_id"],
                "details": _json_load(row["details_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # Internal row/event helpers ----------------------------------------------

    def _append_event_conn(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        event_type: str,
        data: Mapping[str, Any],
        *,
        created_at: str | None = None,
    ) -> int:
        seq = int(
            conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS seq FROM events WHERE session_id=?",
                (session_id,),
            ).fetchone()["seq"]
        )
        conn.execute(
            "INSERT INTO events(session_id,seq,event_type,data_json,created_at) VALUES (?,?,?,?,?)",
            (
                session_id,
                seq,
                event_type,
                _json_dump(_redact_event_data(dict(data))),
                created_at or utc_now(),
            ),
        )
        return seq

    def _security_event_conn(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        details: Mapping[str, Any],
        *,
        client_id: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO security_events(event_type,client_id,details_json,created_at)
            VALUES (?,?,?,?)
            """,
            (event_type, client_id, _json_dump(_redact_event_data(dict(details))), utc_now()),
        )

    def _require_session_conn(
        self, conn: sqlite3.Connection, session_id: str
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise LiliesNotFoundError(f"session not found: {session_id}")
        return row

    def _require_turn_conn(
        self,
        conn: sqlite3.Connection,
        turn_id: str,
        *,
        session_id: str | None = None,
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM turns WHERE id=?", (turn_id,)).fetchone()
        if row is None or (session_id is not None and row["session_id"] != session_id):
            raise LiliesNotFoundError(f"turn not found: {turn_id}")
        return row

    def _require_client_conn(
        self, conn: sqlite3.Connection, client_id: str
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if row is None:
            raise LiliesNotFoundError(f"client not found: {client_id}")
        return row

    def _require_session_access_conn(
        self, conn: sqlite3.Connection, client_id: str, session_id: str
    ) -> None:
        self._require_client_conn(conn, client_id)
        self._require_session_conn(conn, session_id)
        row = conn.execute(
            "SELECT 1 FROM client_session_acl WHERE client_id=? AND session_id=?",
            (client_id, session_id),
        ).fetchone()
        if row is None:
            raise LiliesAccessDeniedError(
                f"client {client_id} cannot access session {session_id}"
            )

    def _session_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["config"] = _json_load(result.pop("config_json"), {})
        result["profile"] = _json_load(result.pop("profile_json"), {})
        result["assignment"] = _json_load(result.pop("assignment_json"), None)
        return result

    def _message_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["content"] = _json_load(result.pop("content_json"), None)
        return result

    def _turn_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["checkpoint"] = _json_load(result.pop("checkpoint_json"), {})
        return result

    def _permission_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["tool_input"] = _json_load(result.pop("tool_input_json"), {})
        result["checkpoint"] = _json_load(result.pop("checkpoint_json"), {})
        result["decision_input"] = _json_load(result.pop("decision_input_json"), None)
        result["input_summary"] = _json_load(result.pop("input_summary_json"), {})
        result["original_input_digest"] = result["input_digest"]
        result["approved_input_digest"] = result.get("decision_input_digest")
        result["message"] = result.get("decision_message")
        return result

    def _event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "session_id": row["session_id"],
            "seq": row["seq"],
            "event_type": row["event_type"],
            "data": _json_load(row["data_json"], {}),
            "created_at": row["created_at"],
        }

    def _client_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "client_id": row["id"],
            "name": row["name"],
            "scopes": _json_load(row["scopes_json"], []),
            "daemon_fingerprint": row["daemon_fingerprint"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "created_at": row["created_at"],
            "last_seen_at": row["last_seen_at"],
        }

    def _credential_metadata(
        self, row: sqlite3.Row, *, replayed: bool = False
    ) -> dict[str, Any]:
        return {
            "credential_ref": row["ref"],
            "name": row["name"],
            "kind": row["kind"],
            "scopes": _json_load(row["scopes_json"], []),
            "client_id": row["client_id"],
            "assignment_id": row["assignment_id"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "revoke_reason": row["revoke_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "replayed": replayed,
        }
