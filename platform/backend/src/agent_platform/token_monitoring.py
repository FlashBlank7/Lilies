from __future__ import annotations

import json
import math
import os
import re
import shlex
import sqlite3
import stat
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import UUID


TOKEN_MONITOR_SCHEMA_VERSION = "1.0"
_ACTIVE_SESSION_STATUSES = {
    "running",
    "waiting_permission",
    "waiting_collaboration",
}
_TERMINAL_ASSIGNMENT_STATUSES = {
    "cancelled",
    "completed",
    "failed",
}
_PYTHON_EXECUTABLE = re.compile(r"(?:python(?:\d+(?:\.\d+)*)?|pypy(?:\d+)?)")
_PYTHON_FLAG_WITH_VALUE = {"-W", "-X"}
_PYTHON_FLAG_WITHOUT_VALUE = {
    "-B",
    "-E",
    "-I",
    "-O",
    "-OO",
    "-P",
    "-R",
    "-S",
    "-s",
    "-u",
    "-v",
    "-x",
}
_LIVE_MODEL_SCRIPTS = {
    "e02_readable_testframe_review_experiment.py",
    "v02_78_complexity_router_bounded_live_validation.py",
    "run_v04_13_live_development_handoff.py",
}
_SOURCE_ACTIVE_COLLECTIONS = {
    "platform": (
        "active_tasks",
        "published_schedule_nodes",
        "due_durable_jobs",
    ),
    "platform_owned_legacy_lilies": (
        "active_sessions",
        "startup_resumable_turns",
    ),
    "standalone_lilies": (
        "active_sessions",
        "startup_resumable_turns",
    ),
    "bridge": ("recoverable_assignments",),
    "collaborative_development": (
        "active_assignments",
        "reserved_provider_costs",
    ),
}
_PROVIDER_BREAKER_GUARDED_PROCESS_KINDS = {
    "collaborative_development_worker",
    "local_lilies_daemon",
    "platform_api",
}
_SOURCE_SCHEMAS: dict[str, dict[str, frozenset[str]]] = {
    "platform": {
        "platform_harness_tasks": frozenset(
            {
                "id",
                "kind",
                "status",
                "owner_id",
                "resource_id",
                "record_json",
                "updated_at",
            }
        ),
        "applications": frozenset({"id", "active_version"}),
        "application_versions": frozenset({"application_id", "version", "snapshot_json"}),
        "durable_jobs": frozenset(
            {
                "id",
                "application_id",
                "status",
                "next_attempt_at",
                "attempt_count",
                "max_attempts",
                "updated_at",
            }
        ),
    },
    "platform_owned_legacy_lilies": {
        "sessions": frozenset(
            {
                "id",
                "status",
                "assignment_id",
                "config_json",
                "assignment_json",
                "token_count",
                "cost_usd",
                "tool_count",
                "model_call_count",
                "created_at",
                "updated_at",
            }
        ),
        "turns": frozenset(
            {
                "id",
                "session_id",
                "status",
                "phase",
                "token_count",
                "cost_usd",
                "tool_count",
                "model_call_count",
                "checkpoint_json",
                "created_at",
                "updated_at",
            }
        ),
    },
    "bridge": {
        "local_lilies_assignments": frozenset(
            {
                "assignment_id",
                "session_id",
                "status",
                "phase",
                "desired_state",
                "updated_at",
            }
        )
    },
    "collaborative_development": {
        "collaborative_development_assignments": frozenset(
            {"assignment_id", "status", "execution_mode", "updated_at"}
        ),
        "collaborative_development_provider_cost_reservations": frozenset(
            {
                "reservation_id",
                "assignment_id",
                "provider",
                "status",
                "record_json",
                "reserved_at",
                "settled_at",
            }
        ),
    },
}
_SQLITE_SNAPSHOT_SUFFIXES = ("", "-wal", "-journal")
_STANDALONE_INTEGER_MAX = 9_223_372_036_854_775_807
_STANDALONE_COST_MAX = 1_000_000_000_000
_STANDALONE_OBSERVABILITY_ENVELOPE_FIELDS = {
    "schema_version",
    "snapshot_kind",
    "before",
    "client_acl_usage",
    "after",
}
_STANDALONE_OBSERVABILITY_FIELDS = {
    "schema_version",
    "scope",
    "coverage_complete",
    "daemon_fingerprint",
    "daemon_instance_id",
    "captured_at",
    "activity_revision",
    "model_egress_enabled",
    "usage",
    "runtime",
    "startup",
}
_STANDALONE_OBSERVABILITY_USAGE_FIELDS = {
    "attempted_calls",
    "recorded_calls",
    "unknown_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
    "ledger_cursor",
}
_STANDALONE_OBSERVABILITY_RUNTIME_FIELDS = {
    "active_sessions",
    "active_model_turns",
    "active_provider_calls",
    "active_development_model_calls",
}
_STANDALONE_OBSERVABILITY_STARTUP_FIELDS = {
    "recovery_completed",
    "automatic_resume_policy",
    "automatic_model_resume_count",
    "explicit_resume_candidate_count",
    "interrupted_sessions",
    "interrupted_turns",
    "interrupted_development_assignments",
    "reconciliation_required_development_invocations",
}
_STANDALONE_OBSERVABILITY_USAGE_COUNTER_FIELDS = (
    "attempted_calls",
    "recorded_calls",
    "unknown_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
)


class TokenMonitorReadError(RuntimeError):
    """A configured token-monitor source could not be read safely."""


class TokenMonitorDataError(RuntimeError):
    """A token-monitor source contains malformed persisted accounting data."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _persisted_json_object(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise TokenMonitorDataError(f"invalid_json:{field}")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as error:
        raise TokenMonitorDataError(f"invalid_json:{field}") from error
    if not isinstance(parsed, Mapping):
        raise TokenMonitorDataError(f"invalid_json_object:{field}")
    return dict(parsed)


def _optional_object(
    value: Mapping[str, Any],
    key: str,
    *,
    field: str,
) -> dict[str, Any]:
    nested = value.get(key)
    if nested is None:
        return {}
    if not isinstance(nested, Mapping):
        raise TokenMonitorDataError(f"invalid_json_object:{field}")
    return dict(nested)


def _ledger_number(
    value: object,
    *,
    field: str,
    integer: bool,
) -> int | float:
    if integer:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 9_223_372_036_854_775_807
        ):
            raise TokenMonitorDataError(f"invalid_nonnegative_integer:{field}")
        return value
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1_000_000_000_000
    ):
        raise TokenMonitorDataError(f"invalid_nonnegative_number:{field}")
    return float(value)


def _optional_usage_number(
    metadata: Mapping[str, Any],
    key: str,
    *,
    integer: bool,
) -> int | float:
    value = metadata.get(key)
    if value is None:
        return 0
    return _ledger_number(value, field=key, integer=integer)


def _number(value: object, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if integer else float(value)


def _is_bounded_public_text(value: object, *, max_bytes: int) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeEncodeError:
        return False


def _file_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        details = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise TokenMonitorReadError(f"cannot inspect token source: {path}") from error
    if not stat.S_ISREG(details.st_mode):
        raise TokenMonitorReadError(f"token source component is not a regular file: {path}")
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _copy_snapshot_component(
    source: Path,
    destination: Path,
    *,
    expected_identity: tuple[int, int, int, int, int],
) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        source_descriptor = os.open(source, source_flags)
    except OSError as error:
        raise TokenMonitorReadError(f"cannot open token source component: {source}") from error
    try:
        opened = os.fstat(source_descriptor)
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        )
        if not stat.S_ISREG(opened.st_mode) or opened_identity != expected_identity:
            raise TokenMonitorReadError(f"token source changed before snapshot: {source}")
        destination_descriptor = os.open(destination, destination_flags, 0o600)
        try:
            while True:
                chunk = os.read(source_descriptor, 1_048_576)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_descriptor, view)
                    if written <= 0:
                        raise TokenMonitorReadError(
                            f"cannot write private token snapshot: {destination}"
                        )
                    view = view[written:]
        finally:
            os.close(destination_descriptor)
        finished = os.fstat(source_descriptor)
        finished_identity = (
            finished.st_dev,
            finished.st_ino,
            finished.st_size,
            finished.st_mtime_ns,
            finished.st_ctime_ns,
        )
        if finished_identity != expected_identity:
            raise TokenMonitorReadError(f"token source changed during snapshot: {source}")
    finally:
        os.close(source_descriptor)
    if _file_identity(source) != expected_identity:
        raise TokenMonitorReadError(f"token source path changed during snapshot: {source}")


def _memory_snapshot(path: Path) -> sqlite3.Connection:
    initial = {
        suffix: _file_identity(Path(f"{path}{suffix}")) for suffix in _SQLITE_SNAPSHOT_SUFFIXES
    }
    if initial[""] is None:
        raise TokenMonitorReadError(f"token source disappeared: {path}")
    with tempfile.TemporaryDirectory(prefix="lilies-token-ledger-") as temporary:
        snapshot_path = Path(temporary) / "ledger.db"
        for suffix, identity in initial.items():
            if identity is None:
                continue
            _copy_snapshot_component(
                Path(f"{path}{suffix}"),
                Path(f"{snapshot_path}{suffix}"),
                expected_identity=identity,
            )
        final = {
            suffix: _file_identity(Path(f"{path}{suffix}")) for suffix in _SQLITE_SNAPSHOT_SUFFIXES
        }
        if final != initial:
            raise TokenMonitorReadError(f"token source changed while being snapshotted: {path}")
        copied: sqlite3.Connection | None = None
        memory: sqlite3.Connection | None = None
        try:
            # The copied directory is private and disposable, so SQLite may safely
            # create/rebuild WAL metadata or recover a copied rollback journal there.
            copied = sqlite3.connect(snapshot_path)
            copied.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            memory = sqlite3.connect(":memory:")
            copied.backup(memory)
        except sqlite3.Error as error:
            if memory is not None:
                memory.close()
            raise TokenMonitorReadError(f"cannot read token source snapshot: {path}") from error
        finally:
            if copied is not None:
                copied.close()
    if memory is None:
        raise TokenMonitorReadError(f"cannot create token source snapshot: {path}")
    memory.row_factory = sqlite3.Row
    memory.execute("PRAGMA query_only=ON")
    return memory


def _open_read_only(path: Path) -> sqlite3.Connection | None:
    path = Path(os.path.abspath(path.expanduser()))
    identity = _file_identity(path)
    if identity is None:
        return None
    return _memory_snapshot(path)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _schema_errors(connection: sqlite3.Connection, source: str) -> list[str]:
    errors: list[str] = []
    for table, required_columns in _SOURCE_SCHEMAS[source].items():
        if not _table_exists(connection, table):
            errors.append(f"missing_table:{table}")
            continue
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        columns = {str(row["name"]) for row in rows}
        missing = sorted(required_columns - columns)
        if missing:
            errors.append(f"missing_columns:{table}:{','.join(missing)}")
    return errors


def _source_unavailable(
    path: Path,
    *,
    reason: str,
    schema_errors: Sequence[str] = (),
    **payload: Any,
) -> dict[str, Any]:
    return {
        "available": False,
        "schema_valid": False if reason in {"schema_mismatch", "data_mismatch"} else None,
        "unavailable_reason": reason,
        "schema_errors": list(schema_errors),
        "path": str(path.expanduser()),
        **payload,
    }


def _usage_sample(
    *,
    source: str,
    stage: str,
    created_at: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    input_tokens = _optional_usage_number(metadata, "input_tokens", integer=True)
    output_tokens = _optional_usage_number(metadata, "output_tokens", integer=True)
    cache_read_tokens = _optional_usage_number(
        metadata,
        "cache_read_input_tokens",
        integer=True,
    )
    cache_creation_tokens = _optional_usage_number(
        metadata,
        "cache_creation_input_tokens",
        integer=True,
    )
    return {
        "source": source,
        "stage": stage,
        "created_at": created_at,
        "provider": metadata.get("provider"),
        "model": metadata.get("model"),
        "task_id": metadata.get("task_id"),
        "task_kind": metadata.get("task_kind"),
        "assignment_id": metadata.get("assignment_id"),
        "session_id": metadata.get("session_id"),
        "turn_id": metadata.get("turn_id"),
        "application_id": metadata.get("application_id"),
        "workflow_id": metadata.get("workflow_id"),
        "run_id": metadata.get("run_id") or metadata.get("resource_id"),
        "node_id": metadata.get("node_id"),
        "seed": metadata.get("seed"),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "cached_input_tokens": int(cache_read_tokens + cache_creation_tokens),
        "reasoning_tokens": int(_optional_usage_number(metadata, "reasoning_tokens", integer=True)),
        "unattributed_tokens": 0,
        "tokens": int(input_tokens + output_tokens),
        "cost_usd": float(_optional_usage_number(metadata, "cost_usd", integer=False)),
        "cost_source": metadata.get("cost_source") or "not_recorded",
        "model_calls": 1,
        "usage_records": 1,
        "unknown_usage_model_calls": 0,
    }


def _platform_stage(task: Mapping[str, Any], metadata: Mapping[str, Any]) -> str:
    phase = str(metadata.get("phase") or "").strip()
    if phase:
        return phase
    kind = str(metadata.get("task_kind") or task.get("kind") or "").strip()
    return kind or "platform_unattributed"


def _platform_snapshot(path: Path, *, now: str) -> dict[str, Any]:
    empty = {
        "samples": [],
        "active_tasks": [],
        "published_schedule_nodes": [],
        "due_durable_jobs": [],
    }
    try:
        connection = _open_read_only(path)
    except TokenMonitorReadError:
        return _source_unavailable(path, reason="read_error", **empty)
    if connection is None:
        return _source_unavailable(path, reason="missing", **empty)
    try:
        errors = _schema_errors(connection, "platform")
        if errors:
            return _source_unavailable(
                path,
                reason="schema_mismatch",
                schema_errors=errors,
                **empty,
            )
        samples: list[dict[str, Any]] = []
        active_tasks: list[dict[str, Any]] = []
        published_schedule_nodes: list[dict[str, Any]] = []
        due_durable_jobs: list[dict[str, Any]] = []
        if _table_exists(connection, "platform_harness_tasks"):
            rows = connection.execute(
                "SELECT id,kind,status,owner_id,resource_id,record_json,updated_at "
                "FROM platform_harness_tasks ORDER BY updated_at,id"
            ).fetchall()
            for row in rows:
                task = _persisted_json_object(
                    row["record_json"],
                    field="platform_harness_tasks.record_json",
                )
                if str(row["status"]) in {"queued", "running", "retry_wait"}:
                    active_tasks.append(
                        {
                            "task_id": str(row["id"]),
                            "kind": str(row["kind"]),
                            "status": str(row["status"]),
                            "updated_at": str(row["updated_at"]),
                        }
                    )
                usage_records = task.get("usage")
                if usage_records is None:
                    continue
                if not isinstance(usage_records, list):
                    raise TokenMonitorDataError(
                        "invalid_json_array:platform_harness_tasks.record_json.usage"
                    )
                model_call_count = 0
                model_usage_count = 0
                model_call_metadata: list[dict[str, Any]] = []
                for usage in usage_records:
                    if not isinstance(usage, Mapping):
                        raise TokenMonitorDataError(
                            "invalid_json_object:platform_harness_tasks.record_json.usage"
                        )
                    if usage.get("usage_type") == "model_call":
                        model_call_count += 1
                        model_call_metadata.append(
                            _optional_object(
                                usage,
                                "metadata",
                                field="platform_harness_tasks.record_json.usage.metadata",
                            )
                        )
                        continue
                    if usage.get("usage_type") != "model_usage":
                        continue
                    model_usage_count += 1
                    metadata = _optional_object(
                        usage,
                        "metadata",
                        field="platform_harness_tasks.record_json.usage.metadata",
                    )
                    samples.append(
                        _usage_sample(
                            source="platform_harness",
                            stage=_platform_stage(task, metadata),
                            created_at=str(usage.get("created_at") or row["updated_at"]),
                            metadata=metadata,
                        )
                    )
                unknown_calls = max(0, model_call_count - model_usage_count)
                if unknown_calls:
                    last_call = model_call_metadata[-1] if model_call_metadata else {}
                    unknown = _usage_sample(
                        source="platform_harness",
                        stage=_platform_stage(task, last_call),
                        created_at=str(row["updated_at"]),
                        metadata={
                            **last_call,
                            "task_id": str(row["id"]),
                            "task_kind": str(row["kind"]),
                            "run_id": row["resource_id"],
                            "model": last_call.get("model"),
                        },
                    )
                    unknown["model_calls"] = unknown_calls
                    unknown["usage_records"] = 0
                    unknown["unknown_usage_model_calls"] = unknown_calls
                    samples.append(unknown)
        if _table_exists(connection, "applications") and _table_exists(
            connection, "application_versions"
        ):
            active_application_rows = connection.execute(
                "SELECT id,active_version FROM applications "
                "WHERE active_version IS NOT NULL ORDER BY id"
            ).fetchall()
            active_versions = {
                str(row["id"]): int(
                    _ledger_number(
                        row["active_version"],
                        field="applications.active_version",
                        integer=True,
                    )
                )
                for row in active_application_rows
            }
            rows = connection.execute(
                "SELECT a.id,a.active_version,v.snapshot_json "
                "FROM applications a JOIN application_versions v "
                "ON v.application_id=a.id AND v.version=a.active_version "
                "WHERE a.active_version IS NOT NULL"
            ).fetchall()
            if len(rows) != len(active_versions) or {str(row["id"]) for row in rows} != set(
                active_versions
            ):
                raise TokenMonitorDataError("missing_or_duplicate_active_application_version")
            for row in rows:
                active_version = active_versions[str(row["id"])]
                snapshot = _persisted_json_object(
                    row["snapshot_json"],
                    field="application_versions.snapshot_json",
                )
                workflow = _optional_object(
                    snapshot,
                    "workflow",
                    field="application_versions.snapshot_json.workflow",
                )
                nodes = workflow.get("nodes")
                if nodes is None:
                    continue
                if not isinstance(nodes, list):
                    raise TokenMonitorDataError(
                        "invalid_json_array:application_versions.snapshot_json.workflow.nodes"
                    )
                for node in nodes:
                    if not isinstance(node, Mapping):
                        raise TokenMonitorDataError(
                            "invalid_json_object:application_versions.snapshot_json.workflow.nodes"
                        )
                    if node.get("type") != "schedule_trigger":
                        continue
                    published_schedule_nodes.append(
                        {
                            "application_id": str(row["id"]),
                            "version": active_version,
                            "node_id": str(node.get("id") or ""),
                            "title": str(node.get("title") or ""),
                        }
                    )
        if _table_exists(connection, "durable_jobs"):
            rows = connection.execute(
                "SELECT id,application_id,status,next_attempt_at,attempt_count,"
                "max_attempts,updated_at FROM durable_jobs "
                "WHERE status IN ('queued','running','retry_wait') "
                "ORDER BY next_attempt_at,id"
            ).fetchall()
            for row in rows:
                attempt_count = int(
                    _ledger_number(
                        row["attempt_count"],
                        field="durable_jobs.attempt_count",
                        integer=True,
                    )
                )
                max_attempts = int(
                    _ledger_number(
                        row["max_attempts"],
                        field="durable_jobs.max_attempts",
                        integer=True,
                    )
                )
                next_attempt_at = str(row["next_attempt_at"])
                if str(row["status"]) == "running" or next_attempt_at <= now:
                    due_durable_jobs.append(
                        {
                            "job_id": str(row["id"]),
                            "application_id": str(row["application_id"]),
                            "status": str(row["status"]),
                            "next_attempt_at": next_attempt_at,
                            "attempt_count": attempt_count,
                            "max_attempts": max_attempts,
                            "updated_at": str(row["updated_at"]),
                        }
                    )
        return {
            "available": True,
            "schema_valid": True,
            "unavailable_reason": None,
            "schema_errors": [],
            "path": str(Path(os.path.abspath(path.expanduser()))),
            "samples": samples,
            "active_tasks": active_tasks,
            "published_schedule_nodes": published_schedule_nodes,
            "due_durable_jobs": due_durable_jobs,
        }
    finally:
        connection.close()


def _local_stage(config: Mapping[str, Any], assignment: Mapping[str, Any]) -> str:
    if assignment:
        mode = str(assignment.get("mode") or assignment.get("assignment_mode") or "")
        if assignment.get("collaboration") is not None:
            return "local_lilies_collaboration"
        if mode == "formal" or assignment.get("task_package") is not None:
            return "local_lilies_formal_builder"
        return "local_lilies_platform_builder"
    kind = str(config.get("kind") or "interactive")
    return f"local_lilies_{kind}"


def _platform_owned_legacy_lilies_snapshot(path: Path | None) -> dict[str, Any]:
    empty = {
        "samples": [],
        "sessions": [],
        "active_sessions": [],
        "startup_resumable_turns": [],
        "boundary": "platform_owned_legacy_state_only",
    }
    if path is None:
        return {
            "available": False,
            "schema_valid": None,
            "unavailable_reason": "not_configured",
            "schema_errors": [],
            "path": None,
            **empty,
        }
    try:
        connection = _open_read_only(path)
    except TokenMonitorReadError:
        return _source_unavailable(path, reason="read_error", **empty)
    if connection is None:
        return _source_unavailable(path, reason="missing", **empty)
    try:
        errors = _schema_errors(connection, "platform_owned_legacy_lilies")
        if errors:
            return _source_unavailable(
                path,
                reason="schema_mismatch",
                schema_errors=errors,
                **empty,
            )
        session_rows = connection.execute(
            "SELECT id,status,assignment_id,config_json,assignment_json,"
            "token_count,cost_usd,tool_count,model_call_count,created_at,updated_at "
            "FROM sessions ORDER BY updated_at,id"
        ).fetchall()
        turn_rows = connection.execute(
            "SELECT id,session_id,status,phase,token_count,cost_usd,"
            "tool_count,model_call_count,checkpoint_json,created_at,updated_at "
            "FROM turns ORDER BY created_at,id"
        ).fetchall()
        session_ids = {str(row["id"]) for row in session_rows}
        turns_by_session: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in turn_rows:
            turn_session_id = str(row["session_id"])
            if turn_session_id not in session_ids:
                raise TokenMonitorDataError("orphan_turn_session")
            _ledger_number(
                row["token_count"],
                field="turns.token_count",
                integer=True,
            )
            _ledger_number(
                row["cost_usd"],
                field="turns.cost_usd",
                integer=False,
            )
            _ledger_number(
                row["tool_count"],
                field="turns.tool_count",
                integer=True,
            )
            _ledger_number(
                row["model_call_count"],
                field="turns.model_call_count",
                integer=True,
            )
            turns_by_session[turn_session_id].append(row)

        samples: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        active_sessions: list[dict[str, Any]] = []
        startup_resumable_turns: list[dict[str, Any]] = []
        for row in session_rows:
            session_id = str(row["id"])
            authoritative_tokens = int(
                _ledger_number(
                    row["token_count"],
                    field="sessions.token_count",
                    integer=True,
                )
            )
            authoritative_cost = float(
                _ledger_number(
                    row["cost_usd"],
                    field="sessions.cost_usd",
                    integer=False,
                )
            )
            authoritative_tools = int(
                _ledger_number(
                    row["tool_count"],
                    field="sessions.tool_count",
                    integer=True,
                )
            )
            authoritative_calls = int(
                _ledger_number(
                    row["model_call_count"],
                    field="sessions.model_call_count",
                    integer=True,
                )
            )
            config = _persisted_json_object(
                row["config_json"],
                field="sessions.config_json",
            )
            assignment = _persisted_json_object(
                row["assignment_json"],
                field="sessions.assignment_json",
            )
            stage = _local_stage(config, assignment)
            assignment_id = row["assignment_id"]
            detailed_tokens = 0
            detailed_cost = 0.0
            detailed_calls = 0
            detailed_tools = 0
            session_turns = turns_by_session.get(session_id, [])
            for turn in session_turns:
                _ledger_number(
                    turn["token_count"],
                    field="turns.token_count",
                    integer=True,
                )
                _ledger_number(
                    turn["cost_usd"],
                    field="turns.cost_usd",
                    integer=False,
                )
                turn_tool_count = int(
                    _ledger_number(
                        turn["tool_count"],
                        field="turns.tool_count",
                        integer=True,
                    )
                )
                turn_model_call_count = int(
                    _ledger_number(
                        turn["model_call_count"],
                        field="turns.model_call_count",
                        integer=True,
                    )
                )
                checkpoint = _persisted_json_object(
                    turn["checkpoint_json"],
                    field="turns.checkpoint_json",
                )
                metrics = _optional_object(
                    checkpoint,
                    "metrics",
                    field="turns.checkpoint_json.metrics",
                )
                usage = _optional_object(
                    metrics,
                    "usage",
                    field="turns.checkpoint_json.metrics.usage",
                )
                sample = _usage_sample(
                    source="platform_owned_legacy_lilies",
                    stage=stage,
                    created_at=str(turn["updated_at"]),
                    metadata={
                        **usage,
                        "provider": "deepseek",
                        "model": config.get("model"),
                        "assignment_id": assignment_id,
                        "session_id": session_id,
                        "turn_id": str(turn["id"]),
                        "task_kind": config.get("kind"),
                    },
                )
                checkpoint_model_calls = int(
                    _optional_usage_number(metrics, "model_calls", integer=True)
                )
                effective_model_calls = max(
                    turn_model_call_count,
                    checkpoint_model_calls,
                )
                checkpoint_tool_calls = int(
                    _optional_usage_number(metrics, "tool_calls", integer=True)
                )
                effective_tool_calls = max(
                    turn_tool_count,
                    checkpoint_tool_calls,
                )
                sample["model_calls"] = effective_model_calls
                sample["usage_records"] = 1 if usage else 0
                usage_backed_calls = int(
                    _optional_usage_number(
                        metrics,
                        "usage_backed_model_calls",
                        integer=True,
                    )
                )
                sample["unknown_usage_model_calls"] = max(
                    0,
                    effective_model_calls - usage_backed_calls,
                )
                sample["tool_calls"] = effective_tool_calls
                detailed_tokens += int(sample["tokens"])
                detailed_cost += float(sample["cost_usd"])
                detailed_calls += int(sample["model_calls"])
                detailed_tools += int(sample["tool_calls"])
                samples.append(sample)
                pending = _optional_object(
                    checkpoint,
                    "pending",
                    field="turns.checkpoint_json.pending",
                )
                if str(turn["status"]) in {"running", "waiting_collaboration"} and (
                    str(turn["status"]) == "waiting_collaboration"
                    or pending.get("kind")
                    in {
                        "collaboration_side_effect_pending",
                        "collaboration_result_recovered",
                    }
                ):
                    startup_resumable_turns.append(
                        {
                            "session_id": session_id,
                            "turn_id": str(turn["id"]),
                            "status": str(turn["status"]),
                            "pending_kind": pending.get("kind"),
                        }
                    )
            missing_tokens = max(0, authoritative_tokens - detailed_tokens)
            missing_cost = max(0.0, authoritative_cost - detailed_cost)
            missing_calls = max(0, authoritative_calls - detailed_calls)
            if missing_tokens or missing_cost or missing_calls:
                unattributed = _usage_sample(
                    source="platform_owned_legacy_lilies",
                    stage=f"{stage}_unattributed",
                    created_at=str(row["updated_at"]),
                    metadata={
                        "assignment_id": assignment_id,
                        "session_id": session_id,
                        "task_kind": config.get("kind"),
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": missing_cost,
                        "cost_source": "session_settlement",
                    },
                )
                unattributed["model_calls"] = missing_calls
                unattributed["tokens"] = missing_tokens
                unattributed["unattributed_tokens"] = missing_tokens
                unattributed["usage_records"] = int(bool(missing_tokens or missing_cost))
                unattributed["unknown_usage_model_calls"] = missing_calls
                samples.append(unattributed)
            session_view = {
                "session_id": session_id,
                "assignment_id": assignment_id,
                "status": str(row["status"]),
                "stage": stage,
                "tokens": max(authoritative_tokens, detailed_tokens),
                "cost_usd": max(authoritative_cost, detailed_cost),
                "model_calls": max(authoritative_calls, detailed_calls),
                "tool_calls": max(authoritative_tools, detailed_tools),
                "updated_at": str(row["updated_at"]),
            }
            sessions.append(session_view)
            if str(row["status"]) in _ACTIVE_SESSION_STATUSES:
                active_sessions.append(session_view)
        return {
            "available": True,
            "schema_valid": True,
            "unavailable_reason": None,
            "schema_errors": [],
            "path": str(Path(os.path.abspath(path.expanduser()))),
            "boundary": "platform_owned_legacy_state_only",
            "samples": samples,
            "sessions": sessions,
            "active_sessions": active_sessions,
            "startup_resumable_turns": startup_resumable_turns,
        }
    finally:
        connection.close()


def _standalone_lilies_snapshot(
    payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    empty = {
        "samples": [],
        "active_sessions": None,
        "startup_resumable_turns": None,
        "active_work_evidence_complete": False,
        "global_evidence_complete": False,
        "model_egress_enabled": None,
        "active_provider_calls": None,
        "runtime": None,
        "startup": None,
    }
    if payload is None:
        return {
            "available": False,
            "schema_valid": None,
            "unavailable_reason": "missing_authenticated_public_usage",
            "schema_errors": [],
            "boundary": "authenticated_public_http_only",
            **empty,
        }
    page_fields = {
        "schema_version",
        "group_by",
        "items",
        "page",
        "page_size",
        "returned_count",
        "total_items",
        "total_pages",
        "truncated",
    }
    errors: list[str] = []
    snapshot_kind = payload.get("snapshot_kind")
    complete_paginated_merge = snapshot_kind == "complete_paginated_merge"
    expected_fields = page_fields | ({"snapshot_kind"} if complete_paginated_merge else set())
    if set(payload) != expected_fields:
        errors.append("public_usage_fields")
    if "snapshot_kind" in payload and not complete_paginated_merge:
        errors.append("public_usage_snapshot_kind")
    group_by = payload.get("group_by")
    items = payload.get("items")
    page = payload.get("page")
    page_size = payload.get("page_size")
    returned_count = payload.get("returned_count")
    total_items = payload.get("total_items")
    total_pages = payload.get("total_pages")
    truncated = payload.get("truncated")
    if payload.get("schema_version") != "1.0":
        errors.append("public_usage_schema_version")
    if group_by != ["session", "stage", "model"]:
        errors.append("public_usage_group_by")
    common_page_invalid = (
        isinstance(page, bool)
        or not isinstance(page, int)
        or page != 1
        or isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 100
        or isinstance(returned_count, bool)
        or not isinstance(returned_count, int)
        or isinstance(total_items, bool)
        or not isinstance(total_items, int)
        or isinstance(total_pages, bool)
        or not isinstance(total_pages, int)
        or returned_count < 0
        or total_items < 0
        or truncated is not False
        or not isinstance(items, list)
        or returned_count != len(items or [])
        or total_items != returned_count
    )
    if complete_paginated_merge:
        expected_total_pages = (
            0
            if not isinstance(total_items, int) or isinstance(total_items, bool) or total_items == 0
            else (total_items + 99) // 100
        )
        page_invalid = (
            common_page_invalid
            or page_size != 100
            or total_items > 100_000
            or total_pages < 0
            or total_pages > 1_000
            or total_pages != expected_total_pages
            or len(items or []) > 100_000
        )
    else:
        expected_total_pages = (
            0
            if not isinstance(total_items, int) or isinstance(total_items, bool) or total_items == 0
            else 1
        )
        page_invalid = (
            common_page_invalid
            or total_pages not in {0, 1}
            or total_pages != expected_total_pages
            or len(items or []) > 100
        )
    if page_invalid:
        errors.append("public_usage_incomplete_page")

    samples: list[dict[str, Any]] = []
    dimensions: set[tuple[str, str, str]] = set()
    if isinstance(items, list):
        expected_item_fields = {
            "session_id",
            "stage",
            "model",
            "recorded_calls",
            "unknown_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
        }
        for index, item in enumerate(items):
            if not isinstance(item, Mapping) or set(item) != expected_item_fields:
                errors.append(f"public_usage_item:{index}:fields")
                continue
            session_id = item.get("session_id")
            stage = item.get("stage")
            model = item.get("model")
            try:
                normalized_session_id = str(UUID(str(session_id)))
            except (TypeError, ValueError, AttributeError):
                errors.append(f"public_usage_item:{index}:session")
                continue
            if (
                not isinstance(session_id, str)
                or normalized_session_id != session_id
                or not _is_bounded_public_text(stage, max_bytes=120)
                or not _is_bounded_public_text(model, max_bytes=200)
            ):
                errors.append(f"public_usage_item:{index}:dimension")
                continue
            integer_fields = (
                "recorded_calls",
                "unknown_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
            )
            if any(
                isinstance(item.get(field), bool)
                or not isinstance(item.get(field), int)
                or not 0 <= int(item[field]) <= _STANDALONE_INTEGER_MAX
                for field in integer_fields
            ):
                errors.append(f"public_usage_item:{index}:integer")
                continue
            cost = item.get("cost_usd")
            if (
                not _standalone_nonnegative_cost(cost)
                or item["total_tokens"] != item["input_tokens"] + item["output_tokens"]
                or item["recorded_calls"] + item["unknown_calls"] > _STANDALONE_INTEGER_MAX
            ):
                errors.append(f"public_usage_item:{index}:accounting")
                continue
            dimension = (normalized_session_id, stage, model)
            if dimension in dimensions:
                errors.append(f"public_usage_item:{index}:duplicate")
                continue
            dimensions.add(dimension)
            samples.append(
                {
                    "source": "standalone_lilies",
                    "stage": stage,
                    "created_at": "",
                    "provider": None,
                    "model": model,
                    "task_id": None,
                    "task_kind": None,
                    "assignment_id": None,
                    "session_id": normalized_session_id,
                    "turn_id": None,
                    "application_id": None,
                    "workflow_id": None,
                    "run_id": None,
                    "node_id": None,
                    "seed": None,
                    "input_tokens": item["input_tokens"],
                    "output_tokens": item["output_tokens"],
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "unattributed_tokens": 0,
                    "tokens": item["total_tokens"],
                    "cost_usd": float(cost),
                    "cost_source": "daemon_reported",
                    "model_calls": item["recorded_calls"] + item["unknown_calls"],
                    "usage_records": item["recorded_calls"],
                    "unknown_usage_model_calls": item["unknown_calls"],
                }
            )
    if errors:
        return {
            "available": False,
            "schema_valid": False,
            "unavailable_reason": "invalid_authenticated_public_usage",
            "schema_errors": errors,
            "boundary": "authenticated_public_http_only",
            **empty,
        }
    return {
        "available": True,
        "schema_valid": True,
        "unavailable_reason": None,
        "schema_errors": [],
        "boundary": "authenticated_public_http_only",
        "samples": samples,
        "active_sessions": None,
        "startup_resumable_turns": None,
        "active_work_evidence_complete": False,
        "global_evidence_complete": False,
        "model_egress_enabled": None,
        "active_provider_calls": None,
        "runtime": None,
        "startup": None,
    }


def _standalone_nonnegative_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _STANDALONE_INTEGER_MAX
    )


def _standalone_nonnegative_cost(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= _STANDALONE_COST_MAX
    return (
        isinstance(value, float)
        and math.isfinite(value)
        and 0 <= value <= _STANDALONE_COST_MAX
    )


def _standalone_utc_timestamp(value: object) -> datetime | None:
    if not _is_bounded_public_text(value, max_bytes=64):
        return None
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        return None
    return parsed


def _standalone_observability_receipt(
    payload: object,
    *,
    label: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    if not isinstance(payload, Mapping) or set(payload) != _STANDALONE_OBSERVABILITY_FIELDS:
        return None, [f"{label}:fields"]
    value = dict(payload)
    usage = value.get("usage")
    runtime = value.get("runtime")
    startup = value.get("startup")
    if not isinstance(usage, Mapping) or set(usage) != _STANDALONE_OBSERVABILITY_USAGE_FIELDS:
        errors.append(f"{label}:usage_fields")
    if (
        not isinstance(runtime, Mapping)
        or set(runtime) != _STANDALONE_OBSERVABILITY_RUNTIME_FIELDS
    ):
        errors.append(f"{label}:runtime_fields")
    if (
        not isinstance(startup, Mapping)
        or set(startup) != _STANDALONE_OBSERVABILITY_STARTUP_FIELDS
    ):
        errors.append(f"{label}:startup_fields")
    if errors:
        return None, errors
    assert isinstance(usage, Mapping)
    assert isinstance(runtime, Mapping)
    assert isinstance(startup, Mapping)

    fingerprint = value.get("daemon_fingerprint")
    instance_id = value.get("daemon_instance_id")
    if value.get("schema_version") != "1.0":
        errors.append(f"{label}:schema_version")
    if value.get("scope") != "daemon_global":
        errors.append(f"{label}:scope")
    if value.get("coverage_complete") is not True:
        errors.append(f"{label}:coverage")
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None
    ):
        errors.append(f"{label}:fingerprint")
    try:
        normalized_instance_id = str(UUID(str(instance_id)))
    except (TypeError, ValueError, AttributeError):
        normalized_instance_id = None
    if not isinstance(instance_id, str) or normalized_instance_id != instance_id:
        errors.append(f"{label}:instance")
    if _standalone_utc_timestamp(value.get("captured_at")) is None:
        errors.append(f"{label}:captured_at")
    if not _standalone_nonnegative_integer(value.get("activity_revision")):
        errors.append(f"{label}:activity_revision")
    if not isinstance(value.get("model_egress_enabled"), bool):
        errors.append(f"{label}:model_egress_enabled")

    runtime_counters_valid = not any(
        not _standalone_nonnegative_integer(runtime.get(field))
        for field in _STANDALONE_OBSERVABILITY_RUNTIME_FIELDS
    )
    if not runtime_counters_valid:
        errors.append(f"{label}:runtime_counters")
    elif (
        runtime["active_development_model_calls"] > runtime["active_provider_calls"]
        or runtime["active_provider_calls"] - runtime["active_development_model_calls"]
        > runtime["active_model_turns"]
        or runtime["active_model_turns"] > runtime["active_sessions"]
    ):
        errors.append(f"{label}:runtime_accounting")
    usage_integer_fields = _STANDALONE_OBSERVABILITY_USAGE_FIELDS - {"cost_usd"}
    usage_counters_valid = not any(
        not _standalone_nonnegative_integer(usage.get(field))
        for field in usage_integer_fields
    )
    if not usage_counters_valid:
        errors.append(f"{label}:usage_counters")
    cost = usage.get("cost_usd")
    if not _standalone_nonnegative_cost(cost):
        errors.append(f"{label}:usage_cost")
    if runtime_counters_valid and usage_counters_valid and not errors and (
        usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
        or usage["ledger_cursor"] < usage["attempted_calls"]
        or usage["attempted_calls"]
        != usage["recorded_calls"]
        + usage["unknown_calls"]
        + runtime["active_provider_calls"]
    ):
        errors.append(f"{label}:usage_accounting")

    if startup.get("recovery_completed") is not True:
        errors.append(f"{label}:recovery")
    if startup.get("automatic_resume_policy") != "explicit_request_only":
        errors.append(f"{label}:resume_policy")
    if any(
        not _standalone_nonnegative_integer(startup.get(field))
        for field in _STANDALONE_OBSERVABILITY_STARTUP_FIELDS
        if field not in {"recovery_completed", "automatic_resume_policy"}
    ):
        errors.append(f"{label}:startup_counters")
    return (value if not errors else None), errors


def _standalone_lilies_observability_snapshot(
    payload: object,
) -> dict[str, Any]:
    empty = {
        "samples": [],
        "active_sessions": None,
        "startup_resumable_turns": None,
        "active_work_evidence_complete": False,
        "global_evidence_complete": False,
        "model_egress_enabled": None,
        "active_provider_calls": None,
        "runtime": None,
        "startup": None,
    }
    if payload is None:
        return {
            "available": False,
            "schema_valid": None,
            "unavailable_reason": "missing_authenticated_observability_snapshot",
            "schema_errors": [],
            "boundary": "paired_authenticated_public_http_only",
            **empty,
        }
    if not isinstance(payload, Mapping):
        return {
            "available": False,
            "schema_valid": False,
            "unavailable_reason": "invalid_authenticated_observability_snapshot",
            "schema_errors": ["observability_envelope_type"],
            "boundary": "paired_authenticated_public_http_only",
            **empty,
        }
    errors: list[str] = []
    if set(payload) != _STANDALONE_OBSERVABILITY_ENVELOPE_FIELDS:
        errors.append("observability_envelope_fields")
    if payload.get("schema_version") != "1.0":
        errors.append("observability_envelope_schema_version")
    if payload.get("snapshot_kind") != "paired_observability_bracket":
        errors.append("observability_envelope_kind")
    before, before_errors = _standalone_observability_receipt(
        payload.get("before"),
        label="before",
    )
    after, after_errors = _standalone_observability_receipt(
        payload.get("after"),
        label="after",
    )
    errors.extend(before_errors)
    errors.extend(after_errors)
    client_acl_usage = payload.get("client_acl_usage")
    if (
        not isinstance(client_acl_usage, Mapping)
        or client_acl_usage.get("snapshot_kind") != "complete_paginated_merge"
    ):
        errors.append("client_acl_usage_kind")
        acl = _standalone_lilies_snapshot(None)
    else:
        acl = _standalone_lilies_snapshot(client_acl_usage)
        if not acl["available"]:
            errors.extend(str(item) for item in acl["schema_errors"])
    if before is not None and after is not None:
        before_usage = before["usage"]
        after_usage = after["usage"]
        before_captured_at = _standalone_utc_timestamp(before["captured_at"])
        after_captured_at = _standalone_utc_timestamp(after["captured_at"])
        if before["daemon_fingerprint"] != after["daemon_fingerprint"]:
            errors.append("observability_bracket_fingerprint")
        if before["daemon_instance_id"] != after["daemon_instance_id"]:
            errors.append("observability_bracket_instance")
        if before_usage["ledger_cursor"] != after_usage["ledger_cursor"]:
            errors.append("observability_bracket_ledger_cursor")
        for field in _STANDALONE_OBSERVABILITY_USAGE_COUNTER_FIELDS:
            same = (
                Decimal(str(before_usage[field])) == Decimal(str(after_usage[field]))
                if field == "cost_usd"
                else before_usage[field] == after_usage[field]
            )
            if not same:
                errors.append(f"observability_bracket_usage:{field}")
        if (
            before_captured_at is None
            or after_captured_at is None
            or after_captured_at < before_captured_at
        ):
            errors.append("observability_bracket_captured_at")
        if after["activity_revision"] < before["activity_revision"]:
            errors.append("observability_bracket_activity_revision")

    acl_samples = [dict(sample) for sample in acl["samples"]] if acl["available"] else []
    if after is not None and acl["available"]:
        after_usage = after["usage"]
        acl_totals = {
            "recorded_calls": sum(int(sample["usage_records"]) for sample in acl_samples),
            "unknown_calls": sum(
                int(sample["unknown_usage_model_calls"]) for sample in acl_samples
            ),
            "input_tokens": sum(int(sample["input_tokens"]) for sample in acl_samples),
            "output_tokens": sum(int(sample["output_tokens"]) for sample in acl_samples),
            "total_tokens": sum(int(sample["tokens"]) for sample in acl_samples),
            "cost_usd": sum(
                (Decimal(str(sample["cost_usd"])) for sample in acl_samples),
                Decimal("0"),
            ),
        }
        for field in (
            "recorded_calls",
            "unknown_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            if (
                acl_totals[field] > after_usage[field]
                or acl_totals[field] > _STANDALONE_INTEGER_MAX
            ):
                errors.append(f"client_acl_exceeds_global:{field}")
        if (
            acl_totals["cost_usd"] > Decimal(str(after_usage["cost_usd"]))
            or acl_totals["cost_usd"] > Decimal(str(_STANDALONE_COST_MAX))
        ):
            errors.append("client_acl_exceeds_global:cost_usd")
    else:
        acl_totals = {}

    if errors or before is None or after is None or not acl["available"]:
        return {
            "available": False,
            "schema_valid": False,
            "unavailable_reason": "invalid_authenticated_observability_snapshot",
            "schema_errors": sorted(set(errors)),
            "boundary": "paired_authenticated_public_http_only",
            **empty,
        }

    captured_at = after["captured_at"]
    for sample in acl_samples:
        sample["created_at"] = captured_at
        sample["detail_scope"] = "paired_client_acl"
    global_usage = after["usage"]
    runtime = after["runtime"]
    startup = after["startup"]
    global_unknown_calls = global_usage["unknown_calls"] + runtime["active_provider_calls"]
    remainder = {
        "source": "standalone_lilies",
        "stage": "standalone_global_unattributed_remainder",
        "created_at": captured_at,
        "provider": None,
        "model": None,
        "task_id": None,
        "task_kind": None,
        "assignment_id": None,
        "session_id": None,
        "turn_id": None,
        "application_id": None,
        "workflow_id": None,
        "run_id": None,
        "node_id": None,
        "seed": None,
        "input_tokens": global_usage["input_tokens"] - acl_totals["input_tokens"],
        "output_tokens": global_usage["output_tokens"] - acl_totals["output_tokens"],
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "unattributed_tokens": global_usage["total_tokens"] - acl_totals["total_tokens"],
        "tokens": global_usage["total_tokens"] - acl_totals["total_tokens"],
        "cost_usd": float(
            Decimal(str(global_usage["cost_usd"])) - acl_totals["cost_usd"]
        ),
        "cost_source": "daemon_reported_global_remainder",
        "model_calls": global_usage["attempted_calls"]
        - acl_totals["recorded_calls"]
        - acl_totals["unknown_calls"],
        "usage_records": global_usage["recorded_calls"] - acl_totals["recorded_calls"],
        "unknown_usage_model_calls": global_unknown_calls - acl_totals["unknown_calls"],
        "detail_scope": "daemon_global_unattributed_remainder",
    }
    if any(
        remainder[field]
        for field in (
            "input_tokens",
            "output_tokens",
            "tokens",
            "cost_usd",
            "model_calls",
            "usage_records",
            "unknown_usage_model_calls",
        )
    ):
        acl_samples.append(remainder)
    return {
        "available": True,
        "schema_valid": True,
        "unavailable_reason": None,
        "schema_errors": [],
        "boundary": (
            "paired_authenticated_daemon_global_observability_with_client_acl_detail"
        ),
        "scope": "daemon_global",
        "detail_scope": "paired_client_acl",
        "daemon_fingerprint": after["daemon_fingerprint"],
        "daemon_instance_id": after["daemon_instance_id"],
        "captured_at": captured_at,
        "activity_revision": after["activity_revision"],
        "ledger_cursor": global_usage["ledger_cursor"],
        "samples": acl_samples,
        "active_sessions": runtime["active_sessions"],
        "startup_resumable_turns": startup["automatic_model_resume_count"],
        "explicit_resume_candidate_count": startup["explicit_resume_candidate_count"],
        "active_work_evidence_complete": True,
        "global_evidence_complete": True,
        "model_egress_enabled": after["model_egress_enabled"],
        "active_provider_calls": runtime["active_provider_calls"],
        "runtime": dict(runtime),
        "startup": dict(startup),
        "global_usage": dict(global_usage),
        "bracket": {
            "before_captured_at": before["captured_at"],
            "after_captured_at": after["captured_at"],
            "ledger_cursor": global_usage["ledger_cursor"],
        },
    }


def _bridge_snapshot(path: Path) -> dict[str, Any]:
    empty = {"recoverable_assignments": []}
    try:
        connection = _open_read_only(path)
    except TokenMonitorReadError:
        return _source_unavailable(path, reason="read_error", **empty)
    if connection is None:
        return _source_unavailable(path, reason="missing", **empty)
    try:
        errors = _schema_errors(connection, "bridge")
        if errors:
            return _source_unavailable(
                path,
                reason="schema_mismatch",
                schema_errors=errors,
                **empty,
            )
        recoverable: list[dict[str, Any]] = []
        rows = connection.execute(
            "SELECT assignment_id,session_id,status,phase,desired_state,updated_at "
            "FROM local_lilies_assignments WHERE desired_state='active' "
            "ORDER BY updated_at,assignment_id"
        ).fetchall()
        for row in rows:
            if str(row["status"]) in _TERMINAL_ASSIGNMENT_STATUSES or str(row["phase"]) in {
                "cancelled",
                "completed",
                "error",
            }:
                continue
            recoverable.append(
                {
                    "assignment_id": str(row["assignment_id"]),
                    "session_id": str(row["session_id"]),
                    "status": str(row["status"]),
                    "phase": str(row["phase"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
        return {
            "available": True,
            "schema_valid": True,
            "unavailable_reason": None,
            "schema_errors": [],
            "path": str(Path(os.path.abspath(path.expanduser()))),
            "recoverable_assignments": recoverable,
        }
    finally:
        connection.close()


def _development_snapshot(path: Path) -> dict[str, Any]:
    empty = {
        "samples": [],
        "active_assignments": [],
        "reserved_provider_costs": [],
    }
    try:
        connection = _open_read_only(path)
    except TokenMonitorReadError:
        return _source_unavailable(path, reason="read_error", **empty)
    if connection is None:
        return _source_unavailable(path, reason="missing", **empty)
    try:
        errors = _schema_errors(connection, "collaborative_development")
        if errors:
            return _source_unavailable(
                path,
                reason="schema_mismatch",
                schema_errors=errors,
                **empty,
            )
        samples: list[dict[str, Any]] = []
        active_assignments: list[dict[str, Any]] = []
        reservations: list[dict[str, Any]] = []
        rows = connection.execute(
            "SELECT assignment_id,status,execution_mode,updated_at "
            "FROM collaborative_development_assignments ORDER BY updated_at,assignment_id"
        ).fetchall()
        active_assignments = [
            {
                "assignment_id": str(row["assignment_id"]),
                "status": str(row["status"]),
                "execution_mode": str(row["execution_mode"]),
                "updated_at": str(row["updated_at"]),
            }
            for row in rows
            if str(row["status"]) == "active"
        ]
        rows = connection.execute(
            "SELECT reservation_id,assignment_id,provider,status,record_json,"
            "reserved_at,settled_at FROM collaborative_development_provider_cost_reservations "
            "ORDER BY reserved_at,reservation_id"
        ).fetchall()
        for row in rows:
            record = _persisted_json_object(
                row["record_json"],
                field="collaborative_development_provider_cost_reservations.record_json",
            )
            receipt = _optional_object(
                record,
                "receipt",
                field=("collaborative_development_provider_cost_reservations.record_json.receipt"),
            )
            usage = receipt if receipt else record
            if str(row["status"]) == "settled":
                samples.append(
                    _usage_sample(
                        source="collaborative_development",
                        stage="collaborative_development",
                        created_at=str(row["settled_at"] or row["reserved_at"]),
                        metadata={
                            **usage,
                            "provider": row["provider"],
                            "assignment_id": row["assignment_id"],
                        },
                    )
                )
            else:
                reservations.append(
                    {
                        "reservation_id": str(row["reservation_id"]),
                        "assignment_id": str(row["assignment_id"]),
                        "provider": str(row["provider"]),
                        "status": str(row["status"]),
                        "reserved_at": str(row["reserved_at"]),
                    }
                )
        return {
            "available": True,
            "schema_valid": True,
            "unavailable_reason": None,
            "schema_errors": [],
            "path": str(Path(os.path.abspath(path.expanduser()))),
            "samples": samples,
            "active_assignments": active_assignments,
            "reserved_provider_costs": reservations,
        }
    finally:
        connection.close()


def _command_argv(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _python_entrypoint(
    argv: Sequence[str],
) -> tuple[str, str, list[str]] | None:
    if not argv or _PYTHON_EXECUTABLE.fullmatch(Path(argv[0]).name) is None:
        return None
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument in _PYTHON_FLAG_WITHOUT_VALUE:
            index += 1
            continue
        if argument in _PYTHON_FLAG_WITH_VALUE:
            if index + 1 >= len(argv):
                return None
            index += 2
            continue
        if argument == "--":
            index += 1
            if index >= len(argv):
                return None
            return "script", Path(argv[index]).name, list(argv[index + 1 :])
        if argument == "-m":
            if index + 1 >= len(argv):
                return None
            return "module", argv[index + 1], list(argv[index + 2 :])
        if argument == "-c":
            if index + 1 >= len(argv):
                return None
            # ``ps`` presents argv without preserving the original boundary
            # around Python's command string. Rejoin the remaining projection
            # so multi-word and escaped-newline launchers remain detectable.
            return "command", " ".join(argv[index + 1 :]), []
        if argument.startswith("-"):
            return None
        return "script", Path(argument).name, list(argv[index + 1 :])
    return None


def _external_codex_argv(argv: Sequence[str]) -> list[str] | None:
    if not argv:
        return None
    if Path(argv[0]).name == "codex":
        return list(argv)
    if Path(argv[0]).name != "sandbox-exec":
        return None
    try:
        separator = argv.index("--")
    except ValueError:
        return None
    nested = list(argv[separator + 1 :])
    if not nested or Path(nested[0]).name != "codex":
        return None
    return nested


def _external_codex_invocation(argv: Sequence[str]) -> str | None:
    codex_argv = _external_codex_argv(argv)
    if codex_argv is None:
        return None
    try:
        exec_index = codex_argv.index("exec")
    except ValueError:
        return None
    configs: dict[str, str] = {}
    index = 1
    while index < exec_index:
        if codex_argv[index] != "-c" or index + 1 >= exec_index:
            index += 1
            continue
        key, separator, raw_value = codex_argv[index + 1].partition("=")
        if separator:
            configs[key] = raw_value.strip("'\"")
        index += 2
    invocation = configs.get("lilies.external_builder_invocation", "")
    runtime_digest = configs.get("lilies.external_builder_runtime_sha256", "")
    workspace_digest = configs.get("lilies.external_builder_workspace_sha256", "")
    if re.fullmatch(r"t01h-[0-9a-f]{32}", invocation) is None:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", runtime_digest) is None:
        return None
    if re.fullmatch(r"[0-9a-f]{64}", workspace_digest) is None:
        return None
    return invocation


def _known_model_process_kind(argv: Sequence[str]) -> str | None:
    entrypoint = _python_entrypoint(argv)
    if entrypoint is None and argv and Path(argv[0]).name.endswith(".py"):
        entrypoint = ("script", Path(argv[0]).name, list(argv[1:]))
    if entrypoint is not None:
        entrypoint_kind, entrypoint_name, arguments = entrypoint
        if entrypoint_kind == "command":
            inline_source = entrypoint_name.replace("\\012", "\n")
            if (
                "agent_platform.api" in inline_source
                and "uvicorn.run" in inline_source
            ):
                return "platform_api"
        if entrypoint_kind == "script":
            if entrypoint_name == "run_v04_13_enterprise_experiment.py":
                return "enterprise_experiment"
            if entrypoint_name == "run_v04_13_codex_builder_child.py":
                return "external_codex_builder"
            if entrypoint_name == "run_v04_13_codex_builder.py" and "--launch-codex" in arguments:
                return "external_codex_builder"
            if entrypoint_name in _LIVE_MODEL_SCRIPTS:
                return "live_model_experiment"
        if entrypoint_kind == "module":
            if entrypoint_name in {
                "agent_platform.lilies_cli",
                "agent_platform.lilies_daemon",
                "lilies_agent.cli",
                "lilies_agent.daemon",
            } and any(argument in {"serve", "run"} for argument in arguments):
                return "local_lilies_daemon"
            if entrypoint_name == "agent_platform.cli":
                return "platform_api"
            if entrypoint_name in {
                "agent_platform.collaborative_development_worker",
                "agent_platform.worker_runner",
            }:
                return "collaborative_development_worker"
            if entrypoint_name in {"uvicorn", "uvicorn.main"} and any(
                argument.partition(":")[0].startswith("agent_platform.") for argument in arguments
            ):
                return "platform_api"

    if not argv:
        return None
    executable = Path(argv[0]).name
    if executable in {"lilies", "lilies-agent"} and any(
        argument in {"serve", "run"} for argument in argv[1:]
    ):
        return "local_lilies_daemon"
    if executable == "agent-platform":
        return "platform_api"
    if executable == "agent-platform-worker":
        return "collaborative_development_worker"
    if executable == "uvicorn" and any(
        argument.startswith("agent_platform") for argument in argv[1:]
    ):
        return "platform_api"
    return None


def _local_lilies_process_distribution(argv: Sequence[str]) -> str:
    entrypoint = _python_entrypoint(argv)
    if entrypoint is not None:
        entrypoint_kind, entrypoint_name, _arguments = entrypoint
        if entrypoint_kind == "module" and entrypoint_name in {
            "lilies_agent.cli",
            "lilies_agent.daemon",
        }:
            return "standalone"
    if argv and Path(argv[0]).name in {"lilies", "lilies-agent"}:
        return "standalone"
    return "platform_owned_legacy"


def discover_model_capable_processes(
    rows: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    strict_process_inspection = rows is None
    if rows is None:
        try:
            result = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,etime=,command="],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise TokenMonitorReadError("model process inspection failed") from error
        if result.returncode != 0 or not result.stdout.strip():
            raise TokenMonitorReadError("model process inspection failed")
        rows = result.stdout.splitlines()
    processes: list[dict[str, Any]] = []
    for raw in rows:
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", raw)
        if not match:
            if strict_process_inspection and raw.strip():
                raise TokenMonitorReadError("model process inspection returned malformed output")
            continue
        pid, parent_pid, elapsed, command = match.groups()
        if int(pid) == os.getpid():
            continue
        argv = _command_argv(command)
        if not argv:
            continue
        invocation = _external_codex_invocation(argv)
        if invocation is not None:
            processes.append(
                {
                    "kind": "external_codex_builder",
                    "pid": int(pid),
                    "parent_pid": int(parent_pid),
                    "elapsed": elapsed,
                    "executable": Path(argv[0]).name,
                    "invocation_id": invocation,
                }
            )
            continue
        kind = _known_model_process_kind(argv)
        if kind is not None:
            process = {
                "kind": kind,
                "pid": int(pid),
                "parent_pid": int(parent_pid),
                "elapsed": elapsed,
                "executable": Path(argv[0]).name,
            }
            if kind == "local_lilies_daemon":
                process["distribution"] = _local_lilies_process_distribution(argv)
            processes.append(process)
    return processes


def compact_token_monitor_snapshot(
    snapshot: Mapping[str, Any],
    *,
    delta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    usage = _json_object(snapshot.get("usage"))
    source_records = _json_object(snapshot.get("sources"))
    source_active_counts: dict[str, dict[str, int | None]] = {}
    for source_name, collection_names in _SOURCE_ACTIVE_COLLECTIONS.items():
        source = _json_object(source_records.get(source_name))
        source_active_counts[source_name] = {
            collection_name: _source_activity_count(source.get(collection_name))
            for collection_name in collection_names
        }
    processes = snapshot.get("processes")
    return {
        "generated_at": snapshot.get("generated_at"),
        "delta": dict(delta) if delta is not None else None,
        "safety": _json_object(snapshot.get("safety")),
        "usage": {
            "totals": _json_object(usage.get("totals")),
            "by_stage": (
                list(usage["by_stage"]) if isinstance(usage.get("by_stage"), list) else []
            ),
            "by_model": (
                list(usage["by_model"]) if isinstance(usage.get("by_model"), list) else []
            ),
            "by_session": (
                list(usage["by_session"]) if isinstance(usage.get("by_session"), list) else []
            ),
        },
        "processes": list(processes) if isinstance(processes, list) else [],
        "sources": source_active_counts,
    }


def _source_activity_count(value: object) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _aggregate_samples(samples: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_tokens": 0,
        "unattributed_tokens": 0,
        "tokens": 0,
        "cost_usd": 0.0,
        "model_calls": 0,
        "usage_records": 0,
        "unknown_usage_model_calls": 0,
    }
    stages: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    sessions: dict[str, dict[str, Any]] = {}
    for sample in samples:
        totals["model_calls"] += int(_number(sample.get("model_calls"), integer=True))
        totals["usage_records"] += int(_number(sample.get("usage_records"), integer=True))
        totals["unknown_usage_model_calls"] += int(
            _number(sample.get("unknown_usage_model_calls"), integer=True)
        )
        for field in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "reasoning_tokens",
            "unattributed_tokens",
            "tokens",
        ):
            totals[field] += int(_number(sample.get(field), integer=True))
        totals["cost_usd"] += float(_number(sample.get("cost_usd")))
        for dimension, key in (
            (stages, str(sample.get("stage") or "unattributed")),
            (models, str(sample.get("model") or "not_recorded")),
            (sessions, str(sample.get("session_id") or "not_recorded")),
        ):
            row = dimension.setdefault(
                key,
                {
                    "name": key,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_input_tokens": 0,
                    "reasoning_tokens": 0,
                    "unattributed_tokens": 0,
                    "tokens": 0,
                    "cost_usd": 0.0,
                    "model_calls": 0,
                    "usage_records": 0,
                    "unknown_usage_model_calls": 0,
                },
            )
            row["model_calls"] += int(_number(sample.get("model_calls"), integer=True))
            row["usage_records"] += int(_number(sample.get("usage_records"), integer=True))
            row["unknown_usage_model_calls"] += int(
                _number(sample.get("unknown_usage_model_calls"), integer=True)
            )
            for field in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
                "unattributed_tokens",
                "tokens",
            ):
                row[field] += int(_number(sample.get(field), integer=True))
            row["cost_usd"] += float(_number(sample.get("cost_usd")))
    return {
        "totals": totals,
        "by_stage": sorted(
            stages.values(),
            key=lambda item: (item["tokens"], item["cost_usd"]),
            reverse=True,
        ),
        "by_model": sorted(
            models.values(),
            key=lambda item: (item["tokens"], item["cost_usd"]),
            reverse=True,
        ),
        "by_session": sorted(
            sessions.values(),
            key=lambda item: (item["tokens"], item["cost_usd"]),
            reverse=True,
        ),
    }


def collect_token_monitor_snapshot(
    *,
    platform_db: Path,
    bridge_db: Path,
    development_db: Path,
    platform_owned_legacy_lilies_db: Path | None = None,
    standalone_usage_snapshot: Mapping[str, Any] | None = None,
    standalone_observability_snapshot: Mapping[str, Any] | None = None,
    process_rows: Sequence[str] | None = None,
    generated_at: str | None = None,
    required_sources: Sequence[str] | None = None,
    model_egress_enabled: bool | None = None,
    external_codex_spend_disabled: bool | None = None,
    process_egress_attestations: Mapping[int, bool | None] | None = None,
    process_records: Sequence[Mapping[str, Any]] | None = None,
    process_inspection_complete: bool = True,
) -> dict[str, Any]:
    legacy_lilies_path = platform_owned_legacy_lilies_db
    generated_at = generated_at or _utc_now()
    try:
        platform = _platform_snapshot(platform_db, now=generated_at)
    except TokenMonitorDataError as error:
        platform = _source_unavailable(
            platform_db,
            reason="data_mismatch",
            schema_errors=(str(error),),
            samples=[],
            active_tasks=[],
            published_schedule_nodes=[],
            due_durable_jobs=[],
        )
    try:
        platform_owned_legacy_lilies = _platform_owned_legacy_lilies_snapshot(legacy_lilies_path)
    except TokenMonitorDataError as error:
        if legacy_lilies_path is None:
            raise
        platform_owned_legacy_lilies = _source_unavailable(
            legacy_lilies_path,
            reason="data_mismatch",
            schema_errors=(str(error),),
            samples=[],
            sessions=[],
            active_sessions=[],
            startup_resumable_turns=[],
            boundary="platform_owned_legacy_state_only",
        )
    standalone_lilies = (
        _standalone_lilies_observability_snapshot(standalone_observability_snapshot)
        if standalone_observability_snapshot is not None
        else _standalone_lilies_snapshot(standalone_usage_snapshot)
    )
    if standalone_observability_snapshot is None and standalone_usage_snapshot is None:
        standalone_lilies["unavailable_reason"] = (
            "missing_authenticated_observability_snapshot"
        )
    bridge = _bridge_snapshot(bridge_db)
    try:
        development = _development_snapshot(development_db)
    except TokenMonitorDataError as error:
        development = _source_unavailable(
            development_db,
            reason="data_mismatch",
            schema_errors=(str(error),),
            samples=[],
            active_assignments=[],
            reserved_provider_costs=[],
        )
    if process_records is not None:
        processes = [dict(process) for process in process_records]
    else:
        try:
            processes = discover_model_capable_processes(process_rows)
        except TokenMonitorReadError:
            processes = []
            process_inspection_complete = False
    samples = [
        *platform["samples"],
        *platform_owned_legacy_lilies["samples"],
        *standalone_lilies["samples"],
        *development["samples"],
    ]
    aggregates = _aggregate_samples(samples)
    standalone_startup_resumable = _source_activity_count(
        standalone_lilies["startup_resumable_turns"]
    )
    startup_auto_consumers: dict[str, int | None] = {
        "published_schedule_nodes": len(platform["published_schedule_nodes"]),
        "due_durable_jobs": len(platform["due_durable_jobs"]),
        "recoverable_local_assignments": len(bridge["recoverable_assignments"]),
        "startup_resumable_lilies_turns": len(
            platform_owned_legacy_lilies["startup_resumable_turns"]
        ),
        "startup_resumable_standalone_lilies_turns": standalone_startup_resumable,
        "active_autonomous_development_assignments": len(
            [
                item
                for item in development["active_assignments"]
                if item["execution_mode"] == "autonomous"
            ]
        ),
    }
    startup_auto_consumer_count = sum(
        value for value in startup_auto_consumers.values() if isinstance(value, int)
    )
    sources = {
        "platform": platform,
        "platform_owned_legacy_lilies": platform_owned_legacy_lilies,
        "standalone_lilies": standalone_lilies,
        "bridge": bridge,
        "collaborative_development": development,
    }
    requested_sources = (
        list(required_sources)
        if required_sources is not None
        else [
            "platform",
            *(["platform_owned_legacy_lilies"] if legacy_lilies_path is not None else []),
            "bridge",
            "collaborative_development",
            *(
                ["standalone_lilies"]
                if (
                    standalone_usage_snapshot is not None
                    or standalone_observability_snapshot is not None
                )
                else []
            ),
        ]
    )
    normalized_required_sources = list(requested_sources)
    invalid_required_sources = [
        source
        for source in normalized_required_sources
        if source not in sources
        or not bool(sources[source].get("available"))
        or (
            source == "standalone_lilies"
            and standalone_lilies.get("global_evidence_complete") is not True
        )
    ]
    ledger_evidence_complete = not invalid_required_sources
    startup_required_sources = [
        "platform",
        "bridge",
        "collaborative_development",
    ]
    if legacy_lilies_path is not None:
        startup_required_sources.append("platform_owned_legacy_lilies")
    startup_ledger_evidence_complete = all(
        bool(sources[source].get("available")) for source in startup_required_sources
    )
    if "standalone_lilies" in normalized_required_sources:
        startup_ledger_evidence_complete = (
            startup_ledger_evidence_complete
            and bool(standalone_lilies.get("available"))
            and standalone_lilies.get("global_evidence_complete") is True
            and standalone_lilies.get("active_work_evidence_complete") is True
        )
    attestations = process_egress_attestations or {}
    classified_processes: list[dict[str, Any]] = []
    unblocked_processes: list[dict[str, Any]] = []
    unknown_processes: list[dict[str, Any]] = []
    for discovered in processes:
        process = dict(discovered)
        pid = process.get("pid")
        kind = process.get("kind")
        if kind not in _PROVIDER_BREAKER_GUARDED_PROCESS_KINDS:
            process["egress_attestation"] = None
            process["safety_status"] = "active_unguarded"
            unblocked_processes.append(process)
        else:
            attestation = (
                attestations.get(pid)
                if isinstance(pid, int) and not isinstance(pid, bool)
                else None
            )
            if not isinstance(attestation, bool):
                attestation = None
            process["egress_attestation"] = attestation
            if attestation is False:
                process["safety_status"] = "egress_disabled_attested"
            elif attestation is True:
                process["safety_status"] = "egress_enabled_attested"
                unblocked_processes.append(process)
            else:
                process["safety_status"] = "egress_unknown"
                unknown_processes.append(process)
        classified_processes.append(process)
    process_attestation_complete = process_inspection_complete and not unknown_processes
    evidence_complete = ledger_evidence_complete and process_attestation_complete
    standalone_model_risk: bool | None = None
    if (
        standalone_lilies.get("available")
        and standalone_lilies.get("global_evidence_complete") is True
    ):
        standalone_model_risk = bool(
            standalone_lilies.get("model_egress_enabled") is True
            or (
                isinstance(standalone_lilies.get("runtime"), Mapping)
                and any(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                    for value in standalone_lilies["runtime"].values()
                )
            )
        )
    if unblocked_processes or standalone_model_risk is True:
        safe_now: bool | None = False
    elif not evidence_complete:
        safe_now = None
    else:
        safe_now = True
    if startup_auto_consumer_count > 0:
        safe_on_start: bool | None = False
    elif not startup_ledger_evidence_complete:
        safe_on_start = None
    else:
        safe_on_start = True
    return {
        "schema_version": TOKEN_MONITOR_SCHEMA_VERSION,
        "generated_at": generated_at,
        "safety": {
            "model_capable_processes_active": len(processes),
            "unblocked_model_processes_active": len(unblocked_processes),
            "unknown_model_processes_active": len(unknown_processes),
            "standalone_model_egress_risk": standalone_model_risk,
            "background_consumption_observed": (False if safe_now is True else None),
            "evidence_complete": evidence_complete,
            "ledger_evidence_complete": ledger_evidence_complete,
            "startup_ledger_evidence_complete": startup_ledger_evidence_complete,
            "process_attestation_complete": process_attestation_complete,
            "process_inspection_complete": process_inspection_complete,
            "missing_required_sources": invalid_required_sources,
            "model_egress_enabled": model_egress_enabled,
            "model_egress_configuration_scope": ("monitor_default_only_not_a_process_attestation"),
            "external_codex_spend_disabled": external_codex_spend_disabled,
            "safe_now": safe_now,
            "safe_on_platform_or_daemon_start": safe_on_start,
            "startup_auto_consumer_count": startup_auto_consumer_count,
            "startup_auto_consumers": startup_auto_consumers,
            "claim_boundary": (
                "A true safety verdict requires every required ledger plus process inspection. "
                "Known process absence does not cover unrelated machines, credentials, "
                "containers, or unclassified programs. Every active built-in platform, "
                "worker, or Lilies daemon needs a PID-bound egress attestation; monitor "
                "environment defaults do not attest another process. External Codex and "
                "live experiment processes remain unsafe while active even when a spend "
                "sentinel exists. The standalone public usage response does not attest "
                "active or startup-resumable work. A true standalone verdict additionally "
                "requires a stable, daemon-global paired observability bracket. Its global "
                "totals are authoritative; session/stage/model rows remain paired-client "
                "ACL detail and any uncovered balance is explicitly unattributed."
            ),
        },
        "usage": {
            **aggregates,
            "accounting": {
                "cost_basis": "configured estimate, not provider billing",
                "platform_owned_legacy_lilies_granularity": "per-turn aggregate",
                "standalone_lilies_granularity": (
                    "daemon-global paired observability totals with paired-client ACL "
                    "session/stage/model detail and an explicit global remainder"
                ),
                "unknown_usage_semantics": (
                    "A model call without a persisted provider usage payload is counted "
                    "as unknown, never as a confirmed zero-token call."
                ),
                "active_turn_semantics": (
                    "Active Local Lilies calls and tools use the greater of settled "
                    "database counters and durable checkpoint metrics."
                ),
                "unknown_usage_reconciliation_semantics": (
                    "When a late provider receipt reduces the unknown-call balance, "
                    "delta reports the reduction as reconciled_unknown_usage_model_calls "
                    "instead of negative consumption."
                ),
            },
            "samples": sorted(samples, key=lambda item: str(item["created_at"])),
        },
        "processes": classified_processes,
        "sources": sources,
    }


def snapshot_delta(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
    *,
    elapsed_seconds: float,
) -> dict[str, Any]:
    current_totals = _json_object(_json_object(current.get("usage")).get("totals"))
    previous_totals = (
        _json_object(_json_object(previous.get("usage")).get("totals"))
        if previous is not None
        else {}
    )
    fields = (
        "input_tokens",
        "output_tokens",
        "cached_input_tokens",
        "reasoning_tokens",
        "unattributed_tokens",
        "tokens",
        "model_calls",
        "usage_records",
        "unknown_usage_model_calls",
    )
    delta = {
        field: int(_number(current_totals.get(field), integer=True))
        - int(_number(previous_totals.get(field), integer=True))
        for field in fields
    }
    raw_unknown_delta = delta["unknown_usage_model_calls"]
    delta["unknown_usage_model_calls"] = max(0, raw_unknown_delta)
    delta["reconciled_unknown_usage_model_calls"] = max(0, -raw_unknown_delta)
    delta["cost_usd"] = float(_number(current_totals.get("cost_usd"))) - float(
        _number(previous_totals.get("cost_usd"))
    )
    delta["tokens_per_minute"] = (
        delta["tokens"] * 60.0 / elapsed_seconds if elapsed_seconds > 0 else 0.0
    )
    return delta
