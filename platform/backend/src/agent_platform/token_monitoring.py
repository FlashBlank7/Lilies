from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote


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
_MODEL_PROCESS_PATTERNS = (
    ("enterprise_experiment", re.compile(r"run_v04_13_enterprise_experiment\.py")),
    (
        "live_model_experiment",
        re.compile(
            r"(?:e02_readable_testframe_review_experiment|"
            r"v02_78_complexity_router_bounded_live_validation|"
            r"run_v04_13_live_development_handoff)\.py"
        ),
    ),
    (
        "local_lilies_daemon",
        re.compile(r"agent_platform\.lilies_(?:cli|daemon).*(?:serve|run)"),
    ),
    ("platform_api", re.compile(r"(?:agent_platform\.cli|uvicorn.*agent_platform)")),
    (
        "collaborative_development_worker",
        re.compile(r"agent_platform\.(?:collaborative_development_worker|worker_runner)"),
    ),
)


class TokenMonitorReadError(RuntimeError):
    """A configured token-monitor source could not be read safely."""


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


def _number(value: object, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value) if integer else float(value)


def _open_read_only(path: Path) -> sqlite3.Connection | None:
    path = path.expanduser().resolve()
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(
            "file:"
            + quote(os.path.relpath(path, Path.cwd()), safe="/")
            + "?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection
    except sqlite3.Error as error:
        raise TokenMonitorReadError(f"cannot read token source: {path}") from error


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _usage_sample(
    *,
    source: str,
    stage: str,
    created_at: str,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    input_tokens = _number(metadata.get("input_tokens"), integer=True)
    output_tokens = _number(metadata.get("output_tokens"), integer=True)
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
        "cached_input_tokens": int(
            _number(metadata.get("cache_read_input_tokens"), integer=True)
            + _number(metadata.get("cache_creation_input_tokens"), integer=True)
        ),
        "reasoning_tokens": int(
            _number(metadata.get("reasoning_tokens"), integer=True)
        ),
        "unattributed_tokens": 0,
        "tokens": int(input_tokens + output_tokens),
        "cost_usd": float(_number(metadata.get("cost_usd"))),
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
    connection = _open_read_only(path)
    if connection is None:
        return {
            "available": False,
            "path": str(path.expanduser()),
            "samples": [],
            "active_tasks": [],
            "published_schedule_nodes": [],
            "due_durable_jobs": [],
        }
    try:
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
                task = _json_object(row["record_json"])
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
                if not isinstance(usage_records, list):
                    continue
                model_call_count = 0
                model_usage_count = 0
                model_call_metadata: list[dict[str, Any]] = []
                for usage in usage_records:
                    if not isinstance(usage, Mapping):
                        continue
                    if usage.get("usage_type") == "model_call":
                        model_call_count += 1
                        model_call_metadata.append(_json_object(usage.get("metadata")))
                        continue
                    if usage.get("usage_type") != "model_usage":
                        continue
                    model_usage_count += 1
                    metadata = _json_object(usage.get("metadata"))
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
            rows = connection.execute(
                "SELECT a.id,a.active_version,v.snapshot_json "
                "FROM applications a JOIN application_versions v "
                "ON v.application_id=a.id AND v.version=a.active_version "
                "WHERE a.active_version IS NOT NULL"
            ).fetchall()
            for row in rows:
                snapshot = _json_object(row["snapshot_json"])
                workflow = _json_object(snapshot.get("workflow"))
                nodes = workflow.get("nodes")
                if not isinstance(nodes, list):
                    continue
                for node in nodes:
                    if not isinstance(node, Mapping) or node.get("type") != "schedule_trigger":
                        continue
                    published_schedule_nodes.append(
                        {
                            "application_id": str(row["id"]),
                            "version": int(row["active_version"]),
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
                next_attempt_at = str(row["next_attempt_at"])
                if str(row["status"]) == "running" or next_attempt_at <= now:
                    due_durable_jobs.append(
                        {
                            "job_id": str(row["id"]),
                            "application_id": str(row["application_id"]),
                            "status": str(row["status"]),
                            "next_attempt_at": next_attempt_at,
                            "attempt_count": int(row["attempt_count"]),
                            "max_attempts": int(row["max_attempts"]),
                            "updated_at": str(row["updated_at"]),
                        }
                    )
        return {
            "available": True,
            "path": str(path.expanduser().resolve()),
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


def _local_lilies_snapshot(path: Path) -> dict[str, Any]:
    connection = _open_read_only(path)
    if connection is None:
        return {
            "available": False,
            "path": str(path.expanduser()),
            "samples": [],
            "sessions": [],
            "active_sessions": [],
            "startup_resumable_turns": [],
        }
    try:
        if not _table_exists(connection, "sessions"):
            return {
                "available": True,
                "path": str(path.expanduser().resolve()),
                "samples": [],
                "sessions": [],
                "active_sessions": [],
                "startup_resumable_turns": [],
            }
        session_rows = connection.execute(
            "SELECT id,status,assignment_id,config_json,assignment_json,"
            "token_count,cost_usd,tool_count,model_call_count,created_at,updated_at "
            "FROM sessions ORDER BY updated_at,id"
        ).fetchall()
        turn_rows = (
            connection.execute(
                "SELECT id,session_id,status,phase,token_count,cost_usd,"
                "tool_count,model_call_count,checkpoint_json,created_at,updated_at "
                "FROM turns ORDER BY created_at,id"
            ).fetchall()
            if _table_exists(connection, "turns")
            else []
        )
        turns_by_session: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in turn_rows:
            turns_by_session[str(row["session_id"])].append(row)

        samples: list[dict[str, Any]] = []
        sessions: list[dict[str, Any]] = []
        active_sessions: list[dict[str, Any]] = []
        startup_resumable_turns: list[dict[str, Any]] = []
        for row in session_rows:
            session_id = str(row["id"])
            config = _json_object(row["config_json"])
            assignment = _json_object(row["assignment_json"])
            stage = _local_stage(config, assignment)
            assignment_id = row["assignment_id"]
            detailed_tokens = 0
            detailed_cost = 0.0
            detailed_calls = 0
            detailed_tools = 0
            session_turns = turns_by_session.get(session_id, [])
            for turn in session_turns:
                checkpoint = _json_object(turn["checkpoint_json"])
                metrics = _json_object(checkpoint.get("metrics"))
                usage = _json_object(metrics.get("usage"))
                sample = _usage_sample(
                    source="local_lilies",
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
                    _number(metrics.get("model_calls"), integer=True)
                )
                effective_model_calls = max(
                    int(turn["model_call_count"]),
                    checkpoint_model_calls,
                )
                checkpoint_tool_calls = int(
                    _number(metrics.get("tool_calls"), integer=True)
                )
                effective_tool_calls = max(
                    int(turn["tool_count"]),
                    checkpoint_tool_calls,
                )
                sample["model_calls"] = effective_model_calls
                sample["usage_records"] = 1 if usage else 0
                usage_backed_calls = int(
                    _number(
                        metrics.get("usage_backed_model_calls"),
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
                pending = _json_object(checkpoint.get("pending"))
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
            authoritative_tokens = int(row["token_count"])
            authoritative_cost = float(row["cost_usd"])
            authoritative_calls = int(row["model_call_count"])
            missing_tokens = max(0, authoritative_tokens - detailed_tokens)
            missing_cost = max(0.0, authoritative_cost - detailed_cost)
            missing_calls = max(0, authoritative_calls - detailed_calls)
            if missing_tokens or missing_cost or missing_calls:
                unattributed = _usage_sample(
                    source="local_lilies",
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
                unattributed["usage_records"] = int(
                    bool(missing_tokens or missing_cost)
                )
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
                "tool_calls": max(int(row["tool_count"]), detailed_tools),
                "updated_at": str(row["updated_at"]),
            }
            sessions.append(session_view)
            if str(row["status"]) in _ACTIVE_SESSION_STATUSES:
                active_sessions.append(session_view)
        return {
            "available": True,
            "path": str(path.expanduser().resolve()),
            "samples": samples,
            "sessions": sessions,
            "active_sessions": active_sessions,
            "startup_resumable_turns": startup_resumable_turns,
        }
    finally:
        connection.close()


def _bridge_snapshot(path: Path) -> dict[str, Any]:
    connection = _open_read_only(path)
    if connection is None:
        return {
            "available": False,
            "path": str(path.expanduser()),
            "recoverable_assignments": [],
        }
    try:
        recoverable: list[dict[str, Any]] = []
        if _table_exists(connection, "local_lilies_assignments"):
            rows = connection.execute(
                "SELECT assignment_id,session_id,status,phase,desired_state,updated_at "
                "FROM local_lilies_assignments WHERE desired_state='active' "
                "ORDER BY updated_at,assignment_id"
            ).fetchall()
            for row in rows:
                if (
                    str(row["status"]) in _TERMINAL_ASSIGNMENT_STATUSES
                    or str(row["phase"]) in {"cancelled", "completed", "error"}
                ):
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
            "path": str(path.expanduser().resolve()),
            "recoverable_assignments": recoverable,
        }
    finally:
        connection.close()


def _development_snapshot(path: Path) -> dict[str, Any]:
    connection = _open_read_only(path)
    if connection is None:
        return {
            "available": False,
            "path": str(path.expanduser()),
            "samples": [],
            "active_assignments": [],
            "reserved_provider_costs": [],
        }
    try:
        samples: list[dict[str, Any]] = []
        active_assignments: list[dict[str, Any]] = []
        reservations: list[dict[str, Any]] = []
        if _table_exists(connection, "collaborative_development_assignments"):
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
        if _table_exists(connection, "collaborative_development_provider_cost_reservations"):
            rows = connection.execute(
                "SELECT reservation_id,assignment_id,provider,status,record_json,"
                "reserved_at,settled_at FROM collaborative_development_provider_cost_reservations "
                "ORDER BY reserved_at,reservation_id"
            ).fetchall()
            for row in rows:
                record = _json_object(row["record_json"])
                receipt = _json_object(record.get("receipt"))
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
            "path": str(path.expanduser().resolve()),
            "samples": samples,
            "active_assignments": active_assignments,
            "reserved_provider_costs": reservations,
        }
    finally:
        connection.close()


def discover_model_capable_processes(
    rows: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    if rows is None:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        rows = result.stdout.splitlines() if result.returncode == 0 else []
    processes: list[dict[str, Any]] = []
    for raw in rows:
        match = re.match(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", raw)
        if not match:
            continue
        pid, parent_pid, elapsed, command = match.groups()
        if int(pid) == os.getpid() or "token_monitoring" in command:
            continue
        for kind, pattern in _MODEL_PROCESS_PATTERNS:
            if pattern.search(command):
                processes.append(
                    {
                        "kind": kind,
                        "pid": int(pid),
                        "parent_pid": int(parent_pid),
                        "elapsed": elapsed,
                        "executable": Path(command.split(maxsplit=1)[0]).name,
                    }
                )
                break
    return processes


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
    for sample in samples:
        totals["model_calls"] += int(_number(sample.get("model_calls"), integer=True))
        totals["usage_records"] += int(
            _number(sample.get("usage_records"), integer=True)
        )
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
            row["usage_records"] += int(
                _number(sample.get("usage_records"), integer=True)
            )
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
    }


def collect_token_monitor_snapshot(
    *,
    platform_db: Path,
    lilies_db: Path,
    bridge_db: Path,
    development_db: Path,
    process_rows: Sequence[str] | None = None,
    generated_at: str | None = None,
    required_sources: Sequence[str] = (
        "platform",
        "local_lilies",
        "bridge",
        "collaborative_development",
    ),
    model_egress_enabled: bool | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or _utc_now()
    platform = _platform_snapshot(platform_db, now=generated_at)
    local_lilies = _local_lilies_snapshot(lilies_db)
    bridge = _bridge_snapshot(bridge_db)
    development = _development_snapshot(development_db)
    processes = discover_model_capable_processes(process_rows)
    samples = [
        *platform["samples"],
        *local_lilies["samples"],
        *development["samples"],
    ]
    aggregates = _aggregate_samples(samples)
    startup_auto_consumers = {
        "published_schedule_nodes": len(platform["published_schedule_nodes"]),
        "due_durable_jobs": len(platform["due_durable_jobs"]),
        "recoverable_local_assignments": len(bridge["recoverable_assignments"]),
        "startup_resumable_lilies_turns": len(local_lilies["startup_resumable_turns"]),
        "active_autonomous_development_assignments": len(
            [
                item
                for item in development["active_assignments"]
                if item["execution_mode"] == "autonomous"
            ]
        ),
    }
    startup_auto_consumer_count = sum(startup_auto_consumers.values())
    sources = {
        "platform": platform,
        "local_lilies": local_lilies,
        "bridge": bridge,
        "collaborative_development": development,
    }
    invalid_required_sources = [
        source
        for source in required_sources
        if source not in sources or not bool(sources[source].get("available"))
    ]
    evidence_complete = not invalid_required_sources
    safe_now = None if not evidence_complete else not processes
    safe_on_start = (
        None
        if not evidence_complete
        else (
            True
            if model_egress_enabled is False
            else startup_auto_consumer_count == 0
        )
    )
    return {
        "schema_version": TOKEN_MONITOR_SCHEMA_VERSION,
        "generated_at": generated_at,
        "safety": {
            "model_capable_processes_active": len(processes),
            "background_consumption_observed": (
                False if evidence_complete and not processes else None
            ),
            "evidence_complete": evidence_complete,
            "missing_required_sources": invalid_required_sources,
            "model_egress_enabled": model_egress_enabled,
            "safe_now": safe_now,
            "safe_on_platform_or_daemon_start": safe_on_start,
            "startup_auto_consumer_count": startup_auto_consumer_count,
            "startup_auto_consumers": startup_auto_consumers,
            "claim_boundary": (
                "A true safety verdict requires every required ledger plus process inspection. "
                "Known process absence does not cover unrelated machines, credentials, "
                "containers, or unclassified programs. A disabled provider breaker prevents "
                "built-in startup paths from issuing model HTTP."
            ),
        },
        "usage": {
            **aggregates,
            "accounting": {
                "cost_basis": "configured estimate, not provider billing",
                "local_lilies_granularity": "per-turn aggregate",
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
        "processes": processes,
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
