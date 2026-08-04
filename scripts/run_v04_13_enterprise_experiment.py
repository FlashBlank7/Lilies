from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import selectors
import secrets
import signal
import shutil
import socket
import stat
import subprocess
import sys
import time
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from agent_platform.task_packages import BudgetSpec, TaskPackageManager
from agent_platform.token_monitoring import (
    collect_token_monitor_snapshot,
    snapshot_delta,
)


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "EXP-LILIES-001"
REVISION = 28
TASK_ROOT = ROOT / "docs" / "experiments" / "lilies-collaboration" / TASK_ID / str(REVISION)
TASK_REVISIONS_ROOT = TASK_ROOT.parent
ENVIRONMENT_CONTROL = ROOT / "scripts" / "experiments" / "exp_lilies_001" / "environment_control.py"
HOST_SNAPSHOT_VERIFIER = (
    ROOT / "scripts" / "experiments" / "exp_lilies_001" / "verify_host_snapshot.py"
)
DEFAULT_PLATFORM_PORT = 18100
DEFAULT_DAEMON_PORT = 18101
TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})
MAX_SESSION_TOKENS = 1_000_000
LIFECYCLE_PHASES = (
    "environment",
    "discovery",
    "pairing",
    "assignment",
    "builder",
    "host_verification",
    "platform_verification",
    "cleanup",
)
MAX_HTTP_BYTES = 32 * 1024 * 1024
STANDALONE_LILIES_ROOT = (ROOT.parent / "LiliesAgent").resolve()
STANDALONE_LILIES_PYTHON = STANDALONE_LILIES_ROOT / ".venv" / "bin" / "python"
STANDALONE_LILIES_DISTRIBUTION = "lilies-local-agent"
STANDALONE_LILIES_VERSION = "0.1.1"
STANDALONE_PROBE_TIMEOUT_SECONDS = 10.0
STANDALONE_PROBE_MAX_STDOUT_BYTES = 4 * 1024
STANDALONE_PAIR_TIMEOUT_SECONDS = 10.0
STANDALONE_PAIR_MAX_STDOUT_BYTES = 8 * 1024
STANDALONE_SUBPROCESS_MAX_STDERR_BYTES = 8 * 1024
STANDALONE_PAIR_MAX_TTL_SECONDS = 660
STANDALONE_USAGE_PAGE_SIZE = 100
STANDALONE_USAGE_MAX_PAGES = 1_000
STANDALONE_USAGE_SNAPSHOT_TIMEOUT_SECONDS = 30.0
STANDALONE_USAGE_INTEGER_MAX = 9_223_372_036_854_775_807
STANDALONE_USAGE_COST_MAX = 1_000_000_000_000
STANDALONE_EVENT_STREAM_MAX_BYTES = 8 * 1024 * 1024
STANDALONE_EVENT_STREAM_MAX_EVENTS = 20_000
STANDALONE_SOURCE_MAX_FILES = 4_096
STANDALONE_SOURCE_MAX_FILE_BYTES = 16 * 1024 * 1024
STANDALONE_SOURCE_MAX_TOTAL_BYTES = 128 * 1024 * 1024
STANDALONE_GIT_STATUS_MAX_BYTES = 256 * 1024
OLLAMA_BINARY_MAX_BYTES = 512 * 1024 * 1024
MIN_POST_STOP_CONFIRMATIONS = 2
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
MODEL_ACCESS_CONTROL_SCOPES = (
    "lilies.daemon:control",
    "lilies.credential:write",
)
FROZEN_MUTATION_QUALIFICATION_CONTRACTS = (
    "append_only_draft_mutation_provenance",
    "meaningful_lilies_authored_draft_chain",
    "no_post_terminal_draft_mutation",
    "reconstruct_assignment_baseline_and_every_draft_mutation",
    "reject_unattributed_noop_discontinuous_or_post_terminal_mutations",
)
STANDALONE_USAGE_FIELDS = frozenset(
    {
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
)
STANDALONE_USAGE_ITEM_FIELDS = frozenset(
    {
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
)
STANDALONE_OBSERVABILITY_FIELDS = frozenset(
    {
        "schema_version",
        "scope",
        "coverage_complete",
        "daemon_fingerprint",
        "daemon_instance_id",
        "captured_at",
        "activity_revision",
        "model_egress_enabled",
        "max_session_tokens",
        "usage",
        "runtime",
        "startup",
    }
)
STANDALONE_OBSERVABILITY_USAGE_FIELDS = frozenset(
    {
        "attempted_calls",
        "recorded_calls",
        "unknown_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "cost_usd",
        "ledger_cursor",
    }
)
STANDALONE_OBSERVABILITY_RUNTIME_FIELDS = frozenset(
    {
        "active_sessions",
        "active_model_turns",
        "active_provider_calls",
        "active_development_model_calls",
    }
)
STANDALONE_OBSERVABILITY_STARTUP_FIELDS = frozenset(
    {
        "recovery_completed",
        "automatic_resume_policy",
        "automatic_model_resume_count",
        "explicit_resume_candidate_count",
        "interrupted_sessions",
        "interrupted_turns",
        "interrupted_development_assignments",
        "reconciliation_required_development_invocations",
        "unreaped_development_processes",
    }
)
STANDALONE_OBSERVABILITY_USAGE_COUNTER_FIELDS = (
    "attempted_calls",
    "recorded_calls",
    "unknown_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
)
PLATFORM_BRIDGE_SCOPES = (
    "lilies.session:read",
    "lilies.session:write",
    "lilies.permission:resolve",
    "lilies.credential:write",
    "lilies.observability:read",
)
PAIRING_CODE_PATTERN = re.compile(r"^[A-Z2-9][A-Z2-9-]{7,79}$")
DAEMON_FINGERPRINT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
OPERATIONAL_PERMISSION_POLICIES = ("manual", "task_local_workspace")
PROVIDER_SECRET_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "LILIES_DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
    }
)
PROVIDER_CONTROL_ENV_NAMES = frozenset(
    {
        "LILIES_MODEL_EGRESS_ENABLED",
        "MODEL_EGRESS_ENABLED",
    }
)
MINIMAL_CHILD_ENV_NAMES = frozenset(
    {
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
)
SESSION_USAGE_COUNTER_FIELDS = (
    "attempted_calls",
    "recorded_calls",
    "unknown_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)
SESSION_USAGE_MONOTONIC_FIELDS = (*SESSION_USAGE_COUNTER_FIELDS, "ledger_cursor")
SESSION_USAGE_EXACT_DELTA_FIELDS = (
    "attempted_calls",
    "recorded_calls",
    "unknown_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cost_usd",
)
ERROR_PROJECTIONS = {
    "enterprise_experiment_error": "The controlled enterprise experiment operation failed.",
    "interrupted": "The controlled enterprise experiment was interrupted.",
    "local_io_error": "A controlled local I/O operation failed.",
    "subprocess_error": "A controlled subprocess operation failed.",
    "upstream_unavailable": "A controlled upstream service operation failed.",
    "runner_error": "The controlled enterprise experiment runner failed.",
}
TASK_LOCAL_PERMISSION_TOOLS = frozenset({"workspace_write", "workspace_patch"})
TASK_LOCAL_WRITABLE_PREFIXES = frozenset({"work", "artifacts"})
TASK_LOCAL_DENIED_SEGMENTS = frozenset(
    {
        ".git",
        ".hg",
        ".lilies-mount-manifest.json",
        ".lilies-workspace-policy.json",
        ".svn",
        "__pycache__",
        "expected-state",
        "oracle",
        "platform-data",
        "platform_data",
        "protected",
    }
)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Reject every redirect so bearer tokens never cross an origin boundary."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


_HTTP_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


class EnterpriseExperimentError(RuntimeError):
    """The controlled EXP-LILIES-001 run could not advance safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _LifecycleRecorder:
    """Record real phase time in an append-only, fsynced execution journal."""

    def __init__(
        self,
        *,
        mode: str,
        journal_path: Path | None = None,
        started_at: str | None = None,
    ) -> None:
        if mode not in {"run", "resume"}:
            raise EnterpriseExperimentError("lifecycle mode is invalid")
        if journal_path is not None and (journal_path.exists() or journal_path.is_symlink()):
            raise EnterpriseExperimentError("lifecycle journal already exists")
        self._mode = mode
        self._started_at = started_at or _now()
        self._started_monotonic_ns = time.monotonic_ns()
        self._last_finished_monotonic_ns = self._started_monotonic_ns
        self._last_finished_at = self._started_at
        self._active_phase: str | None = None
        self._spans: dict[str, dict[str, Any]] = {}
        self._transitions: list[dict[str, Any]] = []
        self._snapshot: dict[str, Any] | None = None
        self._journal_path = journal_path
        self._journal_record_count = 0
        self._journal_tail_digest = "sha256:" + "0" * 64
        self._journal_sealed = False
        self._append_journal(
            "lifecycle.started",
            {"mode": mode, "started_at": self._started_at},
        )

    @property
    def started_at(self) -> str:
        return self._started_at

    def _append_journal(self, event: str, data: Mapping[str, Any]) -> None:
        if self._journal_sealed:
            raise EnterpriseExperimentError("lifecycle journal was already sealed")
        unsigned = {
            "schema_version": "v0.4.13-t01h-execution-journal-1",
            "sequence": self._journal_record_count + 1,
            "previous_record_digest": self._journal_tail_digest,
            "event": event,
            "recorded_at": _now(),
            "data": dict(data),
        }
        record = {**unsigned, "record_digest": _digest(_canonical_json(unsigned))}
        if self._journal_path is not None:
            self._journal_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(
                self._journal_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                os.fchmod(descriptor, 0o600)
                os.write(descriptor, _canonical_json(record) + b"\n")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        self._journal_record_count += 1
        self._journal_tail_digest = str(record["record_digest"])

    @staticmethod
    def _positive_boundary(started_ns: int) -> int:
        finished_ns = time.monotonic_ns()
        while finished_ns <= started_ns:
            finished_ns = time.monotonic_ns()
        return finished_ns

    def _skip(self, phase: str) -> None:
        started_at = self._last_finished_at
        started_ns = self._last_finished_monotonic_ns
        self._append_journal(
            "phase.started",
            {"phase": phase, "started_at": started_at, "outcome": "skipped"},
        )
        self._append_journal(
            "phase.finished",
            {"phase": phase, "outcome": "skipped"},
        )
        finished_ns = self._positive_boundary(started_ns)
        finished_at = _now()
        self._spans[phase] = {
            "phase": phase,
            "started_at": started_at,
            "finished_at": finished_at,
            "started_monotonic_ns": started_ns,
            "finished_monotonic_ns": finished_ns,
            "outcome": "skipped",
        }
        self._transitions.append(
            {
                "phase": phase,
                "event": "skipped",
                "at": finished_at,
            }
        )
        self._last_finished_monotonic_ns = finished_ns
        self._last_finished_at = finished_at

    def start(self, phase: str) -> None:
        if self._snapshot is not None:
            raise EnterpriseExperimentError("lifecycle was already finalized")
        if phase not in LIFECYCLE_PHASES:
            raise EnterpriseExperimentError("lifecycle phase is invalid")
        if self._active_phase is not None:
            raise EnterpriseExperimentError("another lifecycle phase is still active")
        if phase in self._spans:
            raise EnterpriseExperimentError("lifecycle phase was already recorded")
        phase_index = LIFECYCLE_PHASES.index(phase)
        if any(existing in self._spans for existing in LIFECYCLE_PHASES[phase_index + 1 :]):
            raise EnterpriseExperimentError("lifecycle phases are out of order")
        for skipped_phase in LIFECYCLE_PHASES[:phase_index]:
            if skipped_phase not in self._spans:
                self._skip(skipped_phase)
        self._active_phase = phase
        self._spans[phase] = {
            "phase": phase,
            "started_at": self._last_finished_at,
            "started_monotonic_ns": self._last_finished_monotonic_ns,
        }
        self._append_journal(
            "phase.started",
            {"phase": phase, "started_at": self._last_finished_at},
        )
        self._transitions.append(
            {
                "phase": phase,
                "event": "started",
                "at": self._last_finished_at,
            }
        )

    def finish(self, *, outcome: str = "completed") -> None:
        phase = self._active_phase
        if phase is None:
            raise EnterpriseExperimentError("no lifecycle phase is active")
        if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", outcome) is None:
            raise EnterpriseExperimentError("lifecycle outcome is invalid")
        self._append_journal(
            "phase.finished",
            {"phase": phase, "outcome": outcome},
        )
        span = self._spans[phase]
        started_ns = int(span["started_monotonic_ns"])
        finished_ns = self._positive_boundary(started_ns)
        timestamp = _now()
        span.update(
            {
                "finished_at": timestamp,
                "finished_monotonic_ns": finished_ns,
                "outcome": outcome,
            }
        )
        self._transitions.append(
            {
                "phase": phase,
                "event": "finished",
                "at": timestamp,
                "outcome": outcome,
            }
        )
        self._last_finished_monotonic_ns = finished_ns
        self._last_finished_at = timestamp
        self._active_phase = None

    def fail_active(self) -> None:
        if self._active_phase is not None:
            self.finish(outcome="failed")

    def snapshot(self) -> dict[str, Any]:
        if self._snapshot is not None:
            return json.loads(json.dumps(self._snapshot))
        if self._active_phase is not None:
            raise EnterpriseExperimentError("active lifecycle phase cannot be serialized")
        for phase in LIFECYCLE_PHASES:
            if phase not in self._spans:
                self._skip(phase)
        self._append_journal(
            "lifecycle.seal",
            {
                "phase_count": len(LIFECYCLE_PHASES),
                "cleanup_finished_at": self._spans["cleanup"]["finished_at"],
                "human_report_derivation": "after_lifecycle_seal",
            },
        )
        self._journal_sealed = True
        cleanup_finished_ns = int(self._spans["cleanup"]["finished_monotonic_ns"])
        total_duration_ns = cleanup_finished_ns - self._started_monotonic_ns
        durations = {
            phase: (
                int(self._spans[phase]["finished_monotonic_ns"])
                - int(self._spans[phase]["started_monotonic_ns"])
            )
            / 1_000_000_000
            for phase in LIFECYCLE_PHASES
        }
        if any(duration <= 0 for duration in durations.values()):
            raise EnterpriseExperimentError("lifecycle contains a non-positive phase")
        total_duration_raw = total_duration_ns / 1_000_000_000
        if total_duration_raw > 0:
            shares = {
                phase: round(
                    durations[phase] * 100.0 / total_duration_raw,
                    3,
                )
                for phase in LIFECYCLE_PHASES
            }
            correction_phase = max(
                LIFECYCLE_PHASES,
                key=lambda phase: durations[phase],
            )
            shares[correction_phase] = round(
                shares[correction_phase] + 100.0 - sum(shares.values()),
                3,
            )
        else:
            shares = {phase: 0.0 for phase in LIFECYCLE_PHASES}
            shares["cleanup"] = 100.0
        rounded_durations = {phase: round(durations[phase], 9) for phase in LIFECYCLE_PHASES}
        rounded_residue = round(
            round(total_duration_raw, 9) - sum(rounded_durations.values()),
            9,
        )
        rounded_durations["cleanup"] = round(
            max(0.0, rounded_durations["cleanup"] + rounded_residue),
            9,
        )
        spans: list[dict[str, Any]] = []
        for phase in LIFECYCLE_PHASES:
            span = dict(self._spans[phase])
            span.pop("started_monotonic_ns")
            span.pop("finished_monotonic_ns")
            span["duration_seconds"] = rounded_durations[phase]
            spans.append(span)
        total_duration = round(total_duration_raw, 9)
        self._snapshot = {
            "schema_version": "2.0",
            "mode": self._mode,
            "clock": {
                "timestamps": "UTC",
                "durations": "monotonic",
            },
            "private_reasoning_captured": False,
            "started_at": self._started_at,
            "finished_at": self._spans["cleanup"]["finished_at"],
            "total_duration_seconds": total_duration,
            "measured_phase_duration_seconds": total_duration,
            "accounting_residual_seconds": 0.0,
            "accounting_residual_percent": 0.0,
            "phase_share_denominator": "total_duration_seconds",
            "phase_share_percent": shares,
            "spans": spans,
            "transitions": list(self._transitions),
            "execution_journal": {
                "schema_version": "v0.4.13-t01h-execution-journal-1",
                "record_count": self._journal_record_count,
                "tail_digest": self._journal_tail_digest,
                "fsync_per_record": self._journal_path is not None,
                "private_path_name": (
                    None if self._journal_path is None else self._journal_path.name
                ),
                "sealed_after_cleanup": True,
            },
            "sealing_boundary": {
                "phase_denominator_ends_at": "cleanup_finished_at",
                "journal_seal_fsync": "after_phase_denominator",
                "human_report_derivation": "after_lifecycle_and_journal_seal",
                "reason": (
                    "A file cannot attest completion of its own final fsync; the seal and "
                    "human-readable report are therefore explicit post-denominator work."
                ),
            },
        }
        return json.loads(json.dumps(self._snapshot))


class _RunResourceScope(ExitStack):
    """Tear down child processes and the host environment as one real phase."""

    def __init__(
        self,
        lifecycle: _LifecycleRecorder,
        *,
        state_root: Path,
        environment: Mapping[str, str],
    ) -> None:
        super().__init__()
        self._lifecycle = lifecycle
        self._state_root = state_root
        self._environment = environment
        self._environment_up_attempted = False
        self._closed = False
        self._cleanup_outcome: str | None = None

    def mark_environment_up_attempted(self) -> None:
        self._environment_up_attempted = True

    def __exit__(self, *exception: object) -> bool:
        if self._closed:
            return False
        failures: list[tuple[str, BaseException]] = []
        suppressed = False
        had_process_callbacks = bool(self._exit_callbacks)
        cleanup_started = False
        try:
            try:
                if exception[0] is not None:
                    self._lifecycle.fail_active()
                self._lifecycle.start("cleanup")
                cleanup_started = True
            except BaseException as error:
                failures.append(("cleanup_lifecycle_start", error))
            try:
                suppressed = super().__exit__(*exception)
            except BaseException as error:
                failures.append(("process_teardown", error))
            if self._environment_up_attempted:
                try:
                    _environment_command(
                        self._state_root,
                        "down",
                        environment=self._environment,
                    )
                except BaseException as error:
                    failures.append(("environment_down", error))
            self._cleanup_outcome = (
                "failed"
                if failures
                else "completed"
                if self._environment_up_attempted or had_process_callbacks
                else "not_required"
            )
            if cleanup_started:
                try:
                    self._lifecycle.finish(outcome=self._cleanup_outcome)
                except BaseException as error:
                    failures.append(("cleanup_lifecycle_finish", error))
                    self._cleanup_outcome = "failed"
        finally:
            self._closed = True
        if failures:
            labels = ", ".join(label for label, _ in failures)
            cleanup_error = EnterpriseExperimentError(f"controlled cleanup failed: {labels}")
            original = exception[1]
            if isinstance(original, BaseException):
                raise cleanup_error from original
            raise cleanup_error from failures[0][1]
        return suppressed

    def ensure_closed(self, error: BaseException) -> None:
        if not self._closed:
            self.__exit__(type(error), error, error.__traceback__)

    def finish_reporting(self, *, report_failed: bool = False) -> None:
        del report_failed
        if not self._closed or self._cleanup_outcome is None:
            raise EnterpriseExperimentError("cleanup must finish before report finalization")
        if self._lifecycle._active_phase is not None:
            raise EnterpriseExperimentError("a lifecycle phase remained active after cleanup")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _validated_receipt(
    value: Mapping[str, Any],
    *,
    schema_version: str,
    label: str,
) -> dict[str, Any]:
    unsigned = {key: item for key, item in value.items() if key != "receipt_digest"}
    digest = value.get("receipt_digest")
    if (
        value.get("schema_version") != schema_version
        or not isinstance(digest, str)
        or not secrets.compare_digest(digest, _digest(_canonical_json(unsigned)))
    ):
        raise EnterpriseExperimentError(f"{label} receipt is invalid")
    return dict(value)


def _atomic_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_evidence_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _read_private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise EnterpriseExperimentError(f"private run state is unavailable: {path.name}")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise EnterpriseExperimentError(f"private run state must have mode 0600: {path.name}")
    try:
        value = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise EnterpriseExperimentError(f"private run state is invalid: {path.name}") from error
    if not isinstance(value, dict):
        raise EnterpriseExperimentError(f"private run state is not an object: {path.name}")
    return value


def _runner_secrets(state_root: Path, *, create: bool) -> dict[str, str]:
    path = state_root / "runner-secrets.json"
    if path.exists():
        value = _read_private_json(path)
        base_required = (
            "platform_api_token",
            "platform_envelope_key",
            "collaboration_developer_token",
            "collaboration_verifier_token",
            "formal_hidden_seed_key",
        )
        if (
            value.get("schema_version") not in {"1.0", "1.1"}
            or value.get("task_id") != TASK_ID
            or any(
                not isinstance(value.get(key), str) or len(str(value[key])) < 32
                for key in base_required
            )
        ):
            raise EnterpriseExperimentError("runner secret state is invalid")
        signing_key = value.get("collaborative_development_signing_key")
        if signing_key is None and value.get("schema_version") == "1.0":
            value = dict(value)
            value["schema_version"] = "1.1"
            value["collaborative_development_signing_key"] = secrets.token_urlsafe(48)
            _atomic_private_json(path, value)
            signing_key = value["collaborative_development_signing_key"]
        if not isinstance(signing_key, str) or len(signing_key.encode("utf-8")) < 32:
            raise EnterpriseExperimentError("runner secret state is invalid")
        distinct_secrets = [
            str(value[key])
            for key in (
                "platform_api_token",
                "collaboration_developer_token",
                "collaboration_verifier_token",
                "collaborative_development_signing_key",
            )
        ]
        if len(set(distinct_secrets)) != len(distinct_secrets):
            raise EnterpriseExperimentError("runner secret state is invalid")
        return {str(key): str(item) for key, item in value.items()}
    if not create:
        raise EnterpriseExperimentError("runner secrets have not been created")
    value = {
        "schema_version": "1.1",
        "task_id": TASK_ID,
        "platform_api_token": secrets.token_urlsafe(48),
        "platform_envelope_key": secrets.token_urlsafe(48),
        "collaboration_developer_token": secrets.token_urlsafe(48),
        "collaboration_verifier_token": secrets.token_urlsafe(48),
        "formal_hidden_seed_key": secrets.token_urlsafe(48),
        "collaborative_development_signing_key": secrets.token_urlsafe(48),
    }
    _atomic_private_json(path, value)
    return value


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    token: str | None = None,
    value: Any = None,
    timeout: float = 60.0,
) -> Any:
    payload = None if value is None else _canonical_json(value)
    headers = {
        "Accept": "application/json",
        "User-Agent": "Lilies-EXP-LILIES-001-Runner/1.0",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=payload,
        method=method,
        headers=headers,
    )
    try:
        with _HTTP_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(MAX_HTTP_BYTES + 1)
            if len(raw) > MAX_HTTP_BYTES:
                raise EnterpriseExperimentError("platform response exceeds the runner limit")
    except HTTPError as error:
        raise EnterpriseExperimentError("controlled HTTP request was rejected") from error
    except (URLError, OSError, TimeoutError) as error:
        raise EnterpriseExperimentError("controlled HTTP request failed") from error
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnterpriseExperimentError("platform response is not JSON") from error


def _wait_json(
    base_url: str,
    path: str,
    *,
    timeout_seconds: float,
) -> Any:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return _request_json(base_url, path, timeout=2.0)
        except EnterpriseExperimentError as error:
            last_error = error
            time.sleep(0.2)
    raise EnterpriseExperimentError(f"service did not become ready: {path}") from last_error


def _wait_tcp(host: str, port: int, *, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return
        except OSError as error:
            last_error = error
            time.sleep(0.2)
    raise EnterpriseExperimentError(
        f"service did not open its controlled port: {host}:{port}"
    ) from last_error


def _run_checked(arguments: Sequence[str], *, environment: Mapping[str, str]) -> None:
    completed = subprocess.run(
        list(arguments),
        cwd=ROOT,
        env=dict(environment),
        check=False,
    )
    if completed.returncode != 0:
        raise EnterpriseExperimentError(
            f"controlled command failed with status {completed.returncode}: {arguments[0]}"
        )


def _environment_command(
    state_root: Path,
    *arguments: str,
    environment: Mapping[str, str],
) -> None:
    _run_checked(
        (
            sys.executable,
            str(ENVIRONMENT_CONTROL),
            "--state-root",
            str(state_root / "environment"),
            "--package-root",
            str(TASK_ROOT),
            *arguments,
        ),
        environment=environment,
    )


def _terminate(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def _managed_process(
    stack: ExitStack,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str],
    log_path: Path,
) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab", buffering=0)
    stack.callback(log.close)
    process = subprocess.Popen(
        list(arguments),
        cwd=ROOT,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    stack.callback(_terminate, process)
    return process


def _canonical_ollama_inventory_digest(value: Any) -> str:
    if not isinstance(value, str):
        raise EnterpriseExperimentError(
            "managed Ollama model inventory digest is invalid"
        )
    if DAEMON_FINGERPRINT_PATTERN.fullmatch(value) is not None:
        return value
    if re.fullmatch(r"[0-9a-f]{64}", value) is not None:
        return f"sha256:{value}"
    raise EnterpriseExperimentError(
        "managed Ollama model inventory digest is invalid"
    )


def _managed_ollama_model_manifest(
    inventory: Any,
    *,
    configured_model: str,
    frozen_manifest_digest: str,
) -> tuple[int, str]:
    if not isinstance(inventory, Mapping) or not isinstance(
        inventory.get("models"), list
    ):
        raise EnterpriseExperimentError(
            "managed Ollama returned an invalid model inventory"
        )
    models = inventory["models"]
    matching: list[Mapping[str, Any]] = []
    for item in models:
        if not isinstance(item, Mapping):
            raise EnterpriseExperimentError(
                "managed Ollama returned an invalid model inventory"
            )
        names: list[str] = []
        for field in ("name", "model"):
            value = item.get(field)
            if value is not None:
                if not isinstance(value, str) or not value:
                    raise EnterpriseExperimentError(
                        "managed Ollama returned an invalid model inventory"
                    )
                names.append(value)
        if not names:
            raise EnterpriseExperimentError(
                "managed Ollama returned an invalid model inventory"
            )
        if configured_model not in names:
            continue
        if any(name != configured_model for name in names):
            raise EnterpriseExperimentError(
                "managed Ollama configured model inventory identity is invalid"
            )
        matching.append(item)
    if len(matching) != 1:
        raise EnterpriseExperimentError(
            "managed Ollama inventory must contain exactly one configured model"
        )
    inventory_digest = _canonical_ollama_inventory_digest(
        matching[0].get("digest")
    )
    expected_digest = _canonical_ollama_inventory_digest(
        frozen_manifest_digest
    )
    if not secrets.compare_digest(inventory_digest, expected_digest):
        raise EnterpriseExperimentError(
            "managed Ollama configured model manifest digest does not match"
        )
    return len(models), inventory_digest


def _managed_ollama_runtime_home(
    state_root: Path,
    *,
    attempt_id: str,
) -> tuple[Path, str]:
    attempt_key = _attempt_storage_key(attempt_id)
    runtime_root = state_root / "managed-ollama-runtime"
    attempt_root = runtime_root / attempt_key
    for directory in (runtime_root, attempt_root):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise EnterpriseExperimentError(
                "managed Ollama runtime home parent is unavailable"
            ) from error
        try:
            metadata = directory.lstat()
        except OSError as error:
            raise EnterpriseExperimentError(
                "managed Ollama runtime home parent is unavailable"
            ) from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise EnterpriseExperimentError(
                "managed Ollama runtime home parent is unsafe"
            )

    runtime_home = attempt_root / "home"
    created = False
    try:
        runtime_home.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    except OSError as error:
        raise EnterpriseExperimentError(
            "managed Ollama runtime home is unavailable"
        ) from error
    try:
        metadata = runtime_home.lstat()
        resolved = runtime_home.resolve(strict=True)
        resolved.relative_to(state_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as error:
        raise EnterpriseExperimentError(
            "managed Ollama runtime home is unsafe"
        ) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise EnterpriseExperimentError("managed Ollama runtime home is unsafe")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(runtime_home, flags)
    except OSError as error:
        raise EnterpriseExperimentError(
            "managed Ollama runtime home cannot be opened safely"
        ) from error
    try:
        if created:
            os.fchmod(descriptor, 0o700)
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino)
        != (metadata.st_dev, metadata.st_ino)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o700
    ):
        raise EnterpriseExperimentError(
            "managed Ollama runtime home has unsafe ownership or permissions"
        )
    private_identity = {
        "schema_version": "v0.4.13-t01h-managed-ollama-runtime-home-1",
        "attempt_id": attempt_id,
        "resolved_path": str(resolved),
        "device": opened.st_dev,
        "inode": opened.st_ino,
        "owner_uid": opened.st_uid,
        "mode": "0700",
    }
    return resolved, _digest(_canonical_json(private_identity))


def _start_managed_ollama(
    stack: ExitStack,
    *,
    state_root: Path,
    attempt_id: str,
    provider_configuration: Mapping[str, Any],
    log_path: Path,
) -> dict[str, Any] | None:
    identity = provider_configuration.get("identity")
    if not isinstance(identity, Mapping) or identity.get("provider") != "ollama-local":
        return None
    parsed = urlsplit(str(identity.get("base_url") or ""))
    port = parsed.port
    if parsed.hostname != "127.0.0.1" or port is None:
        raise EnterpriseExperimentError("managed Ollama origin is invalid")
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            pass
    except OSError:
        pass
    else:
        raise EnterpriseExperimentError(
            "the selected Ollama port is already owned by another process"
        )
    executable = provider_configuration.get("ollama_executable")
    if not isinstance(executable, str) or not executable:
        raise EnterpriseExperimentError("managed Ollama executable is invalid")
    models_directory_value = provider_configuration.get("ollama_models_directory")
    if not isinstance(models_directory_value, str) or not models_directory_value:
        raise EnterpriseExperimentError("managed Ollama models directory is invalid")
    models_directory, models_directory_identity_digest = (
        _ollama_models_directory_configuration(Path(models_directory_value))
    )
    expected_models_directory_identity_digest = identity.get(
        "ollama_models_directory_identity_digest"
    )
    if not isinstance(expected_models_directory_identity_digest, str) or not (
        secrets.compare_digest(
            models_directory_identity_digest,
            expected_models_directory_identity_digest,
        )
    ):
        raise EnterpriseExperimentError(
            "managed Ollama models directory identity changed before launch"
        )
    runtime_home, runtime_home_identity_digest = _managed_ollama_runtime_home(
        state_root,
        attempt_id=attempt_id,
    )
    environment = _standalone_base_environment()
    environment.update(
        {
            "HOME": str(runtime_home),
            "OLLAMA_HOST": f"127.0.0.1:{port}",
            "OLLAMA_MODELS": str(models_directory),
            "OLLAMA_NO_CLOUD": "1",
            "OLLAMA_NOHISTORY": "1",
        }
    )
    process = _managed_process(
        stack,
        (executable, "serve"),
        environment=environment,
        log_path=log_path,
    )
    inventory = _wait_json(str(identity["base_url"]), "/api/tags", timeout_seconds=60)
    _, post_start_models_directory_identity_digest = (
        _ollama_models_directory_configuration(Path(models_directory_value))
    )
    if not secrets.compare_digest(
        models_directory_identity_digest,
        post_start_models_directory_identity_digest,
    ):
        raise EnterpriseExperimentError(
            "managed Ollama models directory identity changed during startup"
        )
    _, post_start_runtime_home_identity_digest = _managed_ollama_runtime_home(
        state_root,
        attempt_id=attempt_id,
    )
    if not secrets.compare_digest(
        runtime_home_identity_digest,
        post_start_runtime_home_identity_digest,
    ):
        raise EnterpriseExperimentError(
            "managed Ollama runtime home identity changed during startup"
        )
    if process.poll() is not None:
        raise EnterpriseExperimentError("managed Ollama exited during startup")
    configured_model = identity.get("model")
    frozen_manifest_digest = identity.get("model_manifest_digest")
    if not isinstance(configured_model, str) or not isinstance(
        frozen_manifest_digest, str
    ):
        raise EnterpriseExperimentError("managed Ollama provider identity is invalid")
    inventory_model_count, inventory_manifest_digest = (
        _managed_ollama_model_manifest(
            inventory,
            configured_model=configured_model,
            frozen_manifest_digest=frozen_manifest_digest,
        )
    )
    unsigned = {
        "schema_version": "v0.4.13-t01h-managed-ollama-1",
        "provider_identity_digest": identity.get("receipt_digest"),
        "cloud_disabled": True,
        "isolated_runtime_home": True,
        "runtime_home_check": "pre_and_post_start",
        "runtime_home_identity_digest": runtime_home_identity_digest,
        "directory_check": "pre_and_post_start",
        "pre_start_models_directory_identity_digest": (
            models_directory_identity_digest
        ),
        "post_start_models_directory_identity_digest": (
            post_start_models_directory_identity_digest
        ),
        "configured_model": configured_model,
        "configured_model_manifest_digest": inventory_manifest_digest,
        "frozen_model_manifest_digest": frozen_manifest_digest,
        "base_url": identity.get("base_url"),
        "process_id": process.pid,
        "process_group_managed": True,
        "inventory_model_count": inventory_model_count,
        "configured_model_inventory_match_count": 1,
        "started_at": _now(),
    }
    return {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}


def _scrub_provider_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """Project an exact non-secret allowlist for host and platform children.

    The historical name is retained for call-site compatibility.  A denylist
    cannot safely enumerate future providers, CI credentials, shell injection
    variables, or user-home credential locations, so every unlisted variable
    is deliberately dropped.
    """

    projected = {
        str(name): str(value)
        for name, value in environment.items()
        if name in MINIMAL_CHILD_ENV_NAMES and isinstance(value, str) and value
    }
    projected.setdefault("PATH", os.defpath)
    projected.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    projected.setdefault("PYTHONNOUSERSITE", "1")
    return projected


def _platform_environment(
    state_root: Path,
    secrets_state: Mapping[str, str],
    *,
    port: int,
    collaboration_policy: str,
    enable_model_egress: bool = False,
) -> dict[str, str]:
    if enable_model_egress:
        raise EnterpriseExperimentError(
            "platform model egress is forbidden when standalone Lilies is the Builder"
        )
    environment = _scrub_provider_environment(os.environ)
    environment.update(
        {
            "API_TOKEN": secrets_state["platform_api_token"],
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "DATA_DIR": str(state_root / "platform-data"),
            "WORKSPACE_ROOT": str(state_root / "platform-workspaces"),
            "MODEL_EGRESS_ENABLED": "false",
            "PLATFORM_HARNESS_SECRET_ENVELOPE_KEY": secrets_state["platform_envelope_key"],
            "LILIES_LOCAL_AGENT_ENABLED": "true",
            "LILIES_COLLABORATION_ENABLED": "true",
            "LILIES_COLLABORATION_DEVELOPER_TOKEN": secrets_state["collaboration_developer_token"],
            "LILIES_COLLABORATION_VERIFIER_TOKEN": secrets_state["collaboration_verifier_token"],
            "LILIES_FORMAL_HIDDEN_SEED_KEY": secrets_state["formal_hidden_seed_key"],
            "LILIES_COLLABORATIVE_DEVELOPMENT_ENABLED": "true",
            "LILIES_COLLABORATIVE_DEVELOPMENT_SIGNING_KEY": secrets_state[
                "collaborative_development_signing_key"
            ],
            "LILIES_AUTONOMOUS_COLLABORATION_ENABLED": (
                "true" if collaboration_policy == "auto_forward" else "false"
            ),
            "LILIES_PLATFORM_BASE_URL": f"http://127.0.0.1:{port}",
            "LILIES_LOCAL_DISCOVERY_FILE": str(state_root / "lilies-data" / "daemon.json"),
        }
    )
    return environment


def _task_max_turns() -> int:
    try:
        budget = BudgetSpec.model_validate_json((TASK_ROOT / "budget.json").read_bytes())
    except (OSError, ValueError) as error:
        raise EnterpriseExperimentError("frozen task budget is unavailable or invalid") from error
    if budget.task_id != TASK_ID or budget.revision != REVISION:
        raise EnterpriseExperimentError("frozen task budget identity is invalid")
    return budget.max_build_repair_turns


def _standalone_base_environment() -> dict[str, str]:
    return _scrub_provider_environment(os.environ)


class _BoundedSubprocessOutputError(RuntimeError):
    """A standalone child exceeded a fixed stdout or stderr boundary."""


def _kill_isolated_subprocess(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def _run_bounded_subprocess(
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        list(arguments),
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        close_fds=True,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        _kill_isolated_subprocess(process)
        raise EnterpriseExperimentError("standalone subprocess pipes are unavailable")

    streams = {
        process.stdout.fileno(): ("stdout", process.stdout, max_stdout_bytes),
        process.stderr.fileno(): ("stderr", process.stderr, max_stderr_bytes),
    }
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout_seconds
    try:
        for descriptor, (_, stream, _) in streams.items():
            os.set_blocking(descriptor, False)
            selector.register(stream, selectors.EVENT_READ, descriptor)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout_seconds)
            ready = selector.select(remaining)
            if not ready:
                raise subprocess.TimeoutExpired(arguments, timeout_seconds)
            for key, _ in ready:
                descriptor = int(key.data)
                label, stream, maximum = streams[descriptor]
                try:
                    chunk = os.read(descriptor, 64 * 1024)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if len(buffers[label]) + len(chunk) > maximum:
                    raise _BoundedSubprocessOutputError(
                        f"standalone subprocess {label} exceeded its limit"
                    )
                buffers[label].extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(arguments, timeout_seconds)
        returncode = process.wait(timeout=remaining)
    except BaseException:
        _kill_isolated_subprocess(process)
        raise
    finally:
        selector.close()
        if not process.stdout.closed:
            process.stdout.close()
        if not process.stderr.closed:
            process.stderr.close()
    return subprocess.CompletedProcess(
        list(arguments),
        returncode,
        stdout=bytes(buffers["stdout"]),
        stderr=bytes(buffers["stderr"]),
    )


def _parse_bounded_subprocess_json(
    completed: subprocess.CompletedProcess[bytes],
    *,
    label: str,
    max_stdout_bytes: int,
) -> dict[str, Any]:
    stdout = completed.stdout
    stderr = completed.stderr
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise EnterpriseExperimentError(f"{label} returned an invalid byte stream")
    if len(stdout) > max_stdout_bytes or len(stderr) > STANDALONE_SUBPROCESS_MAX_STDERR_BYTES:
        raise EnterpriseExperimentError(f"{label} exceeded its response limit")
    if completed.returncode != 0:
        raise EnterpriseExperimentError(f"{label} failed")
    try:
        value = json.loads(stdout)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise EnterpriseExperimentError(f"{label} returned invalid JSON") from error
    if not isinstance(value, dict):
        raise EnterpriseExperimentError(f"{label} returned an invalid JSON object")
    return value


def _standalone_source_tree_identity(source_root: Path) -> dict[str, Any]:
    """Hash the exact regular files beneath the executed ``src/lilies_agent`` tree."""

    try:
        root_metadata = source_root.lstat()
    except OSError as error:
        raise EnterpriseExperimentError("standalone Lilies source tree is unavailable") from error
    if not stat.S_ISDIR(root_metadata.st_mode) or stat.S_ISLNK(root_metadata.st_mode):
        raise EnterpriseExperimentError("standalone Lilies source tree is unsafe")
    hasher = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for directory, names, files in os.walk(source_root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        retained_names: list[str] = []
        for name in sorted(names):
            candidate = directory_path / name
            try:
                metadata = candidate.lstat()
            except OSError as error:
                raise EnterpriseExperimentError(
                    "standalone Lilies source tree changed during verification"
                ) from error
            if stat.S_ISLNK(metadata.st_mode):
                raise EnterpriseExperimentError("standalone Lilies source tree contains a symlink")
            if not stat.S_ISDIR(metadata.st_mode):
                raise EnterpriseExperimentError(
                    "standalone Lilies source tree contains a non-directory entry"
                )
            if name != "__pycache__":
                retained_names.append(name)
        names[:] = retained_names
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            path = directory_path / name
            relative = path.relative_to(source_root).as_posix()
            encoded_relative = relative.encode("utf-8")
            if not encoded_relative or len(encoded_relative) > 4_096:
                raise EnterpriseExperimentError("standalone Lilies source path exceeds its limit")
            try:
                before = path.lstat()
            except OSError as error:
                raise EnterpriseExperimentError(
                    "standalone Lilies source tree changed during verification"
                ) from error
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise EnterpriseExperimentError(
                    "standalone Lilies source tree contains a non-regular file"
                )
            if before.st_size > STANDALONE_SOURCE_MAX_FILE_BYTES:
                raise EnterpriseExperimentError("standalone Lilies source file exceeds its limit")
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
            except OSError as error:
                raise EnterpriseExperimentError(
                    "standalone Lilies source file could not be opened safely"
                ) from error
            payload = bytearray()
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                    before.st_dev,
                    before.st_ino,
                ):
                    raise EnterpriseExperimentError(
                        "standalone Lilies source file changed before hashing"
                    )
                while True:
                    chunk = os.read(descriptor, 64 * 1024)
                    if not chunk:
                        break
                    payload.extend(chunk)
                    if len(payload) > STANDALONE_SOURCE_MAX_FILE_BYTES:
                        raise EnterpriseExperimentError(
                            "standalone Lilies source file exceeds its limit"
                        )
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
            ) or len(payload) != after.st_size:
                raise EnterpriseExperimentError(
                    "standalone Lilies source file changed while hashing"
                )
            file_count += 1
            total_bytes += len(payload)
            if file_count > STANDALONE_SOURCE_MAX_FILES:
                raise EnterpriseExperimentError(
                    "standalone Lilies source tree contains too many files"
                )
            if total_bytes > STANDALONE_SOURCE_MAX_TOTAL_BYTES:
                raise EnterpriseExperimentError(
                    "standalone Lilies source tree exceeds its byte limit"
                )
            hasher.update(encoded_relative)
            hasher.update(b"\0")
            hasher.update(payload)
            hasher.update(b"\0")
    if file_count < 1:
        raise EnterpriseExperimentError("standalone Lilies source tree is empty")
    return {
        "source_tree_digest": f"sha256:{hasher.hexdigest()}",
        "source_file_count": file_count,
        "source_total_bytes": total_bytes,
    }


def _verify_standalone_lilies_runtime() -> dict[str, Any]:
    sibling_path = ROOT.parent / "LiliesAgent"
    if sibling_path.is_symlink():
        raise EnterpriseExperimentError("standalone Lilies sibling must not be a symlink")
    expected_root = sibling_path.resolve()
    if STANDALONE_LILIES_ROOT != expected_root:
        raise EnterpriseExperimentError("standalone Lilies root is not the fixed sibling")
    expected_python = STANDALONE_LILIES_ROOT / ".venv" / "bin" / "python"
    if STANDALONE_LILIES_PYTHON != expected_python:
        raise EnterpriseExperimentError(
            "standalone Lilies interpreter is not the fixed sibling interpreter"
        )
    try:
        interpreter_metadata = STANDALONE_LILIES_PYTHON.lstat()
    except OSError as error:
        raise EnterpriseExperimentError("standalone Lilies interpreter is unavailable") from error
    if not (
        stat.S_ISREG(interpreter_metadata.st_mode) or stat.S_ISLNK(interpreter_metadata.st_mode)
    ):
        raise EnterpriseExperimentError("standalone Lilies interpreter is invalid")

    source_root = STANDALONE_LILIES_ROOT / "src" / "lilies_agent"
    source_identity = _standalone_source_tree_identity(source_root)
    probe = f"""
import importlib.metadata as metadata
import json
from pathlib import Path
import lilies_agent

distribution = metadata.distribution({STANDALONE_LILIES_DISTRIBUTION!r})
print(json.dumps({{
    'distribution': distribution.metadata['Name'],
    'version': distribution.version,
    'distribution_root': str(Path(distribution.locate_file('')).resolve()),
    'module_file': str(Path(lilies_agent.__file__).resolve()),
}}, separators=(',', ':')))
"""
    try:
        completed = _run_bounded_subprocess(
            (str(STANDALONE_LILIES_PYTHON), "-I", "-c", probe),
            cwd=STANDALONE_LILIES_ROOT,
            environment=_standalone_base_environment(),
            timeout_seconds=STANDALONE_PROBE_TIMEOUT_SECONDS,
            max_stdout_bytes=STANDALONE_PROBE_MAX_STDOUT_BYTES,
            max_stderr_bytes=STANDALONE_SUBPROCESS_MAX_STDERR_BYTES,
        )
    except subprocess.TimeoutExpired as error:
        raise EnterpriseExperimentError(
            "standalone Lilies distribution verification timed out"
        ) from error
    except _BoundedSubprocessOutputError as error:
        raise EnterpriseExperimentError(
            "standalone Lilies distribution verification exceeded its response limit"
        ) from error
    except OSError as error:
        raise EnterpriseExperimentError(
            "standalone Lilies distribution verification could not start"
        ) from error
    identity = _parse_bounded_subprocess_json(
        completed,
        label="standalone Lilies distribution verification",
        max_stdout_bytes=STANDALONE_PROBE_MAX_STDOUT_BYTES,
    )
    if set(identity) != {
        "distribution",
        "version",
        "distribution_root",
        "module_file",
    }:
        raise EnterpriseExperimentError("standalone Lilies distribution identity schema is invalid")
    if (
        identity["distribution"] != STANDALONE_LILIES_DISTRIBUTION
        or identity["version"] != STANDALONE_LILIES_VERSION
    ):
        raise EnterpriseExperimentError("standalone Lilies distribution identity is invalid")
    try:
        distribution_root = Path(str(identity["distribution_root"])).resolve(strict=True)
        module_file = Path(str(identity["module_file"])).resolve(strict=True)
        distribution_root.relative_to(STANDALONE_LILIES_ROOT / ".venv")
        expected_module_file = (source_root / "__init__.py").resolve(strict=True)
    except (OSError, ValueError) as error:
        raise EnterpriseExperimentError(
            "standalone Lilies distribution escaped the fixed sibling"
        ) from error
    if module_file != expected_module_file:
        raise EnterpriseExperimentError("standalone Lilies module identity is invalid")
    try:
        commit_result = _run_bounded_subprocess(
            ("git", "rev-parse", "--verify", "HEAD"),
            cwd=STANDALONE_LILIES_ROOT,
            environment=_standalone_base_environment(),
            timeout_seconds=STANDALONE_PROBE_TIMEOUT_SECONDS,
            max_stdout_bytes=128,
            max_stderr_bytes=STANDALONE_SUBPROCESS_MAX_STDERR_BYTES,
        )
    except (OSError, subprocess.TimeoutExpired, _BoundedSubprocessOutputError) as error:
        raise EnterpriseExperimentError("standalone Lilies commit verification failed") from error
    if commit_result.returncode != 0:
        raise EnterpriseExperimentError("standalone Lilies commit verification failed")
    try:
        sibling_commit = commit_result.stdout.decode("ascii").strip().casefold()
    except UnicodeDecodeError as error:
        raise EnterpriseExperimentError("standalone Lilies commit identity is invalid") from error
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sibling_commit) is None:
        raise EnterpriseExperimentError("standalone Lilies commit identity is invalid")
    try:
        status_result = _run_bounded_subprocess(
            (
                "git",
                "-c",
                "core.quotepath=false",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--",
                "src/lilies_agent",
            ),
            cwd=STANDALONE_LILIES_ROOT,
            environment=_standalone_base_environment(),
            timeout_seconds=STANDALONE_PROBE_TIMEOUT_SECONDS,
            max_stdout_bytes=STANDALONE_GIT_STATUS_MAX_BYTES,
            max_stderr_bytes=STANDALONE_SUBPROCESS_MAX_STDERR_BYTES,
        )
    except (OSError, subprocess.TimeoutExpired, _BoundedSubprocessOutputError) as error:
        raise EnterpriseExperimentError(
            "standalone Lilies dirty-state verification failed"
        ) from error
    if status_result.returncode != 0:
        raise EnterpriseExperimentError("standalone Lilies dirty-state verification failed")
    dirty_entries = [entry for entry in status_result.stdout.split(b"\0") if entry]
    return {
        "builder_actor": "lilies",
        "python": str(STANDALONE_LILIES_PYTHON),
        "sibling_root": str(STANDALONE_LILIES_ROOT),
        "sibling_commit": sibling_commit,
        "distribution": str(identity["distribution"]),
        "version": str(identity["version"]),
        **source_identity,
        "package_digest": source_identity["source_tree_digest"],
        "package_file_count": source_identity["source_file_count"],
        "package_digest_source": "executed_src/lilies_agent_path_bytes",
        "sibling_dirty": bool(dirty_entries),
        "sibling_dirty_entry_count": len(dirty_entries),
        "sibling_dirty_status_digest": _digest(status_result.stdout),
    }


def _bounded_regular_file_digest(path: Path, *, maximum_bytes: int, label: str) -> str:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise EnterpriseExperimentError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_size > maximum_bytes
    ):
        raise EnterpriseExperimentError(f"{label} is unsafe")
    hasher = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise EnterpriseExperimentError(f"{label} changed before hashing")
        observed = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            observed += len(chunk)
            if observed > maximum_bytes:
                raise EnterpriseExperimentError(f"{label} exceeds its limit")
            hasher.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if observed != after.st_size or (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise EnterpriseExperimentError(f"{label} changed while hashing")
    return f"sha256:{hasher.hexdigest()}"


def _ollama_models_directory_configuration(path: Path) -> tuple[Path, str]:
    requested = path
    try:
        requested_metadata = requested.lstat()
        resolved = requested.resolve(strict=True)
        resolved_metadata = resolved.lstat()
    except (OSError, RuntimeError) as error:
        raise EnterpriseExperimentError(
            "Ollama models directory is unavailable"
        ) from error
    if (
        stat.S_ISLNK(requested_metadata.st_mode)
        or not stat.S_ISDIR(requested_metadata.st_mode)
        or stat.S_ISLNK(resolved_metadata.st_mode)
        or not stat.S_ISDIR(resolved_metadata.st_mode)
    ):
        raise EnterpriseExperimentError("Ollama models directory is unsafe")

    dangerous_directories = {
        Path(resolved.anchor),
        ROOT.resolve(),
        STANDALONE_LILIES_ROOT.resolve(),
    }
    if len(resolved.parts) <= 3 or resolved in dangerous_directories:
        raise EnterpriseExperimentError(
            "Ollama models directory is an overly broad dangerous path"
        )

    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(resolved, flags)
    except OSError as error:
        raise EnterpriseExperimentError(
            "Ollama models directory cannot be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or (opened.st_dev, opened.st_ino)
        != (resolved_metadata.st_dev, resolved_metadata.st_ino)
        or opened.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) & 0o022
    ):
        raise EnterpriseExperimentError(
            "Ollama models directory changed or has unsafe ownership or permissions"
        )
    private_identity = {
        "schema_version": "v0.4.13-t01h-ollama-models-directory-1",
        "resolved_path": str(resolved),
        "device": opened.st_dev,
        "inode": opened.st_ino,
    }
    return resolved, _digest(_canonical_json(private_identity))


def _provider_launch_configuration(args: argparse.Namespace) -> dict[str, Any]:
    if not bool(getattr(args, "enable_model_egress", False)):
        raise EnterpriseExperimentError(
            "real model egress remains disabled; pass --enable-model-egress for this authorized run"
        )
    provider = str(getattr(args, "model_provider", "deepseek"))
    max_output_tokens = int(getattr(args, "provider_max_output_tokens", 16_384))
    if not 256 <= max_output_tokens <= 384_000:
        raise EnterpriseExperimentError("provider maximum output tokens are invalid")
    model_argument = getattr(args, "model", None)
    if provider == "deepseek":
        if any(
            getattr(args, name, None) is not None
            for name in (
                "ollama_base_url",
                "ollama_model_manifest_digest",
                "ollama_template_digest",
                "ollama_context_window_tokens",
                "ollama_binary",
                "ollama_models_dir",
            )
        ):
            raise EnterpriseExperimentError("Ollama arguments are forbidden for the DeepSeek route")
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise EnterpriseExperimentError(
                "DEEPSEEK_API_KEY is required for a real DeepSeek model run"
            )
        model = DEFAULT_DEEPSEEK_MODEL if model_argument is None else str(model_argument)
        if model != DEFAULT_DEEPSEEK_MODEL:
            raise EnterpriseExperimentError("the DeepSeek route model is fixed")
        unsigned = {
            "schema_version": "v0.4.13-t01h-provider-identity-1",
            "provider": "deepseek",
            "model": model,
            "base_url": DEFAULT_DEEPSEEK_BASE_URL,
            "max_output_tokens": max_output_tokens,
            "credential_class": "paid_process_environment",
            "managed_local_process": False,
        }
        return {
            "identity": {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))},
            "ollama_executable": None,
        }
    if provider != "ollama-local":
        raise EnterpriseExperimentError("model provider is invalid")
    model = str(model_argument or "")
    base_url = str(getattr(args, "ollama_base_url", None) or "")
    manifest_digest = str(getattr(args, "ollama_model_manifest_digest", None) or "")
    template_digest = str(getattr(args, "ollama_template_digest", None) or "")
    context_window = getattr(args, "ollama_context_window_tokens", None)
    binary_argument = getattr(args, "ollama_binary", None)
    models_directory_argument = getattr(args, "ollama_models_dir", None)
    if models_directory_argument is None:
        raise EnterpriseExperimentError(
            "ollama-local requires --ollama-models-dir"
        )
    if (
        not model
        or len(model.encode("utf-8")) > 200
        or any(character in model for character in "\x00\r\n")
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(manifest_digest) is None
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(template_digest) is None
        or isinstance(context_window, bool)
        or not isinstance(context_window, int)
        or not 256 <= context_window <= 2_000_000
        or max_output_tokens > context_window
        or binary_argument is None
    ):
        raise EnterpriseExperimentError(
            "ollama-local requires exact model, digests, context, output, and binary configuration"
        )
    parsed = urlsplit(base_url)
    try:
        ollama_port = parsed.port
    except ValueError as error:
        raise EnterpriseExperimentError("Ollama base URL is invalid") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or ollama_port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise EnterpriseExperimentError(
            "Ollama base URL must be an explicit numeric loopback HTTP origin"
        )
    binary_text = str(binary_argument)
    resolved_binary_text = (
        shutil.which(binary_text)
        if len(Path(binary_text).parts) == 1
        else str(Path(binary_text).expanduser().resolve())
    )
    if not resolved_binary_text:
        raise EnterpriseExperimentError("Ollama executable is unavailable")
    executable = Path(resolved_binary_text).resolve(strict=True)
    binary_digest = _bounded_regular_file_digest(
        executable,
        maximum_bytes=OLLAMA_BINARY_MAX_BYTES,
        label="Ollama executable",
    )
    models_directory, models_directory_identity_digest = (
        _ollama_models_directory_configuration(Path(models_directory_argument))
    )
    unsigned = {
        "schema_version": "v0.4.13-t01h-provider-identity-1",
        "provider": "ollama-local",
        "model": model,
        "base_url": f"http://127.0.0.1:{ollama_port}",
        "model_manifest_digest": manifest_digest,
        "template_digest": template_digest,
        "context_window_tokens": context_window,
        "max_output_tokens": max_output_tokens,
        "credential_class": "credential_free_loopback",
        "managed_local_process": True,
        "ollama_executable_name": executable.name,
        "ollama_executable_digest": binary_digest,
        "ollama_models_directory_identity_digest": (
            models_directory_identity_digest
        ),
    }
    return {
        "identity": {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))},
        "ollama_executable": str(executable),
        "ollama_models_directory": str(models_directory),
    }


def _daemon_environment(
    state_root: Path,
    *,
    port: int,
    provider_configuration: Mapping[str, Any],
) -> dict[str, str]:
    identity = provider_configuration.get("identity")
    if not isinstance(identity, Mapping):
        raise EnterpriseExperimentError("model provider configuration is invalid")
    provider = identity.get("provider")
    environment = _standalone_base_environment()
    environment.update(
        {
            "HOME": str(state_root / "lilies-home"),
            "LILIES_DATA_DIR": str(state_root / "lilies-data"),
            "LILIES_WORKSPACE_ROOT": str(state_root / "lilies-workspaces"),
            "LILIES_HOST": "127.0.0.1",
            "LILIES_PORT": str(port),
            "LILIES_DEFAULT_MAX_TURNS": str(_task_max_turns()),
            "LILIES_MAX_SESSION_TOKENS": str(MAX_SESSION_TOKENS),
            "LILIES_WORKFLOW_STUDIO_ENABLED": "true",
            "LILIES_MODEL_PROVIDER": str(provider),
            "LILIES_MODEL": str(identity.get("model")),
            "LILIES_MAX_OUTPUT_TOKENS": str(identity.get("max_output_tokens")),
            "LILIES_MODEL_EGRESS_ENABLED": ("true" if provider == "deepseek" else "false"),
        }
    )
    if provider == "deepseek":
        provider_key = os.environ.get("DEEPSEEK_API_KEY")
        if not provider_key:
            raise EnterpriseExperimentError(
                "authorized DEEPSEEK_API_KEY is unavailable for standalone Lilies"
            )
        environment["LILIES_DEEPSEEK_API_KEY"] = provider_key
        environment["LILIES_DEEPSEEK_BASE_URL"] = str(identity["base_url"])
    elif provider == "ollama-local":
        environment.update(
            {
                "LILIES_OLLAMA_BASE_URL": str(identity["base_url"]),
                "LILIES_OLLAMA_MODEL_MANIFEST_DIGEST": str(identity["model_manifest_digest"]),
                "LILIES_OLLAMA_TEMPLATE_DIGEST": str(identity["template_digest"]),
                "LILIES_OLLAMA_CONTEXT_WINDOW_TOKENS": str(identity["context_window_tokens"]),
                "LILIES_CLI_TOKEN_TTL_SECONDS": "300",
            }
        )
        environment.pop("LILIES_DEEPSEEK_API_KEY", None)
    else:
        raise EnterpriseExperimentError("model provider configuration is invalid")
    return environment


def _standalone_daemon_command(
    standalone_python: Path,
    *,
    state_root: Path,
    port: int,
) -> tuple[str, ...]:
    if standalone_python != STANDALONE_LILIES_PYTHON:
        raise EnterpriseExperimentError("unverified standalone Lilies interpreter")
    return (
        str(standalone_python),
        "-I",
        "-m",
        "lilies_agent.cli",
        "--data-dir",
        str(state_root / "lilies-data"),
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    )


def _freeze_package(platform_data: Path) -> dict[str, Any]:
    manager = TaskPackageManager(platform_data / "task-packages")
    temporary = Path(tempfile.mkdtemp(prefix="lilies-t01h-runner-source-"))
    try:
        source_manager = TaskPackageManager(temporary)
        source = None
        for revision in range(1, REVISION + 1):
            source_root = TASK_ROOT if revision == REVISION else TASK_REVISIONS_ROOT / str(revision)
            source = source_manager.freeze_revision(source_root)
            if revision < REVISION:
                manager.freeze_revision(source_root)
        assert source is not None
        if manager.has_frozen_revision(TASK_ID, REVISION):
            package = manager.load_frozen(TASK_ID, REVISION)
            if (
                package.record.public_summary_digest != source.record.public_summary_digest
                or package.record.sealed_package_digest != source.record.sealed_package_digest
            ):
                raise EnterpriseExperimentError(
                    f"run state already freezes another EXP-LILIES-001 revision-{REVISION} payload"
                )
        else:
            package = manager.freeze_revision(TASK_ROOT)
    finally:
        for path in sorted(temporary.rglob("*"), reverse=True):
            try:
                os.chmod(path, stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR)
            except FileNotFoundError:
                continue
        shutil.rmtree(temporary)
    return package.record.model_dump(mode="json")


def _host_secrets(state_root: Path) -> dict[str, Any]:
    environment_root = state_root / "environment"
    secrets_state = _read_private_json(environment_root / "secrets.json")
    credentials = _read_private_json(environment_root / "credentials.json")
    required = {
        "exp-lilies-001-environment-attestation": secrets_state.get("attestation_secret"),
        "exp-lilies-001-paperless-builder-token": credentials.get("paperless_builder_token"),
        "exp-lilies-001-inventree-builder-token": credentials.get("inventree_builder_token"),
        "exp-lilies-001-paperless-verifier-token": credentials.get("paperless_verifier_token"),
        "exp-lilies-001-inventree-verifier-token": credentials.get("inventree_verifier_token"),
    }
    if any(not isinstance(value, str) or not value for value in required.values()):
        raise EnterpriseExperimentError("scoped host credentials are incomplete")
    return required


def _install_environment_secrets(
    platform_url: str,
    platform_token: str,
    values: Mapping[str, Any],
) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for name, value in sorted(values.items()):
        receipt = _request_json(
            platform_url,
            "/api/v1/platform/secrets",
            method="POST",
            token=platform_token,
            value={
                "owner_id": "formal-environment",
                "name": name,
                "value": str(value),
                "description": f"{TASK_ID} revision {REVISION} controlled secret",
            },
        )
        if not isinstance(receipt, dict) or receipt.get("encrypted") is not True:
            raise EnterpriseExperimentError("platform did not encrypt a formal environment secret")
        receipts.append(
            {
                "owner_id": receipt.get("owner_id"),
                "name": receipt.get("name"),
                "encrypted": True,
            }
        )
    return receipts


def _assert_platform_discovered_daemon(
    platform_url: str,
    platform_token: str,
    *,
    daemon_url: str,
    daemon_pid: int,
    daemon_health: Mapping[str, Any],
    expected_model_egress_enabled: bool = True,
) -> dict[str, Any]:
    """Bind platform discovery to the daemon process started by this run."""

    daemon_fingerprint = daemon_health.get("daemon_fingerprint")
    if (
        not isinstance(daemon_fingerprint, str)
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(daemon_fingerprint) is None
    ):
        raise EnterpriseExperimentError("standalone Lilies health fingerprint is invalid")
    if daemon_health.get("model_egress_enabled") is not expected_model_egress_enabled:
        raise EnterpriseExperimentError(
            "standalone Lilies public health changed the expected model-access state"
        )
    if isinstance(daemon_pid, bool) or not isinstance(daemon_pid, int) or daemon_pid <= 0:
        raise EnterpriseExperimentError("standalone Lilies process id is invalid")
    status = _request_json(
        platform_url,
        "/api/v1/local-lilies/status",
        token=platform_token,
    )
    discovery = status.get("discovery") if isinstance(status, dict) else None
    if not isinstance(discovery, dict) or discovery.get("status") != "available":
        raise EnterpriseExperimentError(
            "platform did not automatically discover the isolated Lilies daemon"
        )
    discovered_fingerprint = discovery.get("daemon_fingerprint")
    discovered_pid = discovery.get("pid")
    if (
        discovery.get("base_url") != daemon_url
        or not isinstance(discovered_fingerprint, str)
        or not secrets.compare_digest(discovered_fingerprint, daemon_fingerprint)
        or isinstance(discovered_pid, bool)
        or discovered_pid != daemon_pid
    ):
        raise EnterpriseExperimentError(
            "platform discovery does not match the isolated Lilies daemon"
        )
    return {
        "status": "available",
        "base_url": daemon_url,
        "daemon_fingerprint": daemon_fingerprint,
        "pid": daemon_pid,
    }


def _safe_discovery_projection(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    projection = {
        key: value.get(key) for key in ("status", "base_url", "daemon_fingerprint", "pid")
    }
    projection.update(
        {
            "provider_identity": value.get("provider_identity"),
            "managed_ollama": value.get("managed_ollama"),
            "local_model_authorization": value.get("local_model_authorization"),
        }
    )
    return projection


def _create_daemon_pairing_code(
    *,
    state_root: Path,
    standalone_python: Path,
    daemon_environment: Mapping[str, str],
    scopes: Sequence[str],
    expected_daemon_fingerprint: str,
) -> dict[str, Any]:
    if standalone_python != STANDALONE_LILIES_PYTHON:
        raise EnterpriseExperimentError("unverified standalone Lilies interpreter")
    if not scopes or len(set(scopes)) != len(scopes):
        raise EnterpriseExperimentError("standalone Lilies pairing scopes are invalid")
    command = [
        str(standalone_python),
        "-I",
        "-m",
        "lilies_agent.cli",
        "--data-dir",
        str(state_root / "lilies-data"),
        "pair",
    ]
    for scope in scopes:
        command.extend(("--scope", scope))
    pairing_environment = dict(daemon_environment)
    pairing_environment["LILIES_MODEL_EGRESS_ENABLED"] = "false"
    pairing_environment.pop("LILIES_MAX_SESSION_TOKENS", None)
    pairing_environment.pop("LILIES_DEEPSEEK_API_KEY", None)
    try:
        completed = _run_bounded_subprocess(
            command,
            cwd=STANDALONE_LILIES_ROOT,
            environment=pairing_environment,
            timeout_seconds=STANDALONE_PAIR_TIMEOUT_SECONDS,
            max_stdout_bytes=STANDALONE_PAIR_MAX_STDOUT_BYTES,
            max_stderr_bytes=STANDALONE_SUBPROCESS_MAX_STDERR_BYTES,
        )
    except subprocess.TimeoutExpired as error:
        raise EnterpriseExperimentError("standalone Lilies pairing command timed out") from error
    except _BoundedSubprocessOutputError as error:
        raise EnterpriseExperimentError(
            "standalone Lilies pairing command exceeded its response limit"
        ) from error
    except OSError as error:
        raise EnterpriseExperimentError(
            "standalone Lilies pairing command could not start"
        ) from error
    pairing = _parse_bounded_subprocess_json(
        completed,
        label="standalone Lilies pairing command",
        max_stdout_bytes=STANDALONE_PAIR_MAX_STDOUT_BYTES,
    )
    if set(pairing) != {
        "allowed_scopes",
        "daemon_fingerprint",
        "expires_at",
        "pairing_code",
    } or pairing.get("allowed_scopes") != sorted(scopes):
        raise EnterpriseExperimentError("standalone Lilies pairing schema is invalid")
    pairing_code = pairing.get("pairing_code")
    daemon_fingerprint = pairing.get("daemon_fingerprint")
    expires_at = pairing.get("expires_at")
    if (
        not isinstance(pairing_code, str)
        or PAIRING_CODE_PATTERN.fullmatch(pairing_code) is None
        or not isinstance(daemon_fingerprint, str)
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(daemon_fingerprint) is None
        or not secrets.compare_digest(
            daemon_fingerprint,
            expected_daemon_fingerprint,
        )
        or not isinstance(expires_at, str)
    ):
        raise EnterpriseExperimentError("standalone Lilies pairing identity is invalid")
    try:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise EnterpriseExperimentError("standalone Lilies pairing expiry is invalid") from error
    current_time = datetime.now(timezone.utc)
    if (
        expiry.tzinfo is None
        or expiry.utcoffset() != timezone.utc.utcoffset(expiry)
        or not current_time
        < expiry
        <= current_time + timedelta(seconds=STANDALONE_PAIR_MAX_TTL_SECONDS)
    ):
        raise EnterpriseExperimentError("standalone Lilies pairing expiry is invalid")
    return pairing


def _pair_daemon(
    *,
    state_root: Path,
    daemon_port: int,
    platform_url: str,
    platform_token: str,
    standalone_python: Path,
    daemon_environment: Mapping[str, str],
    expected_daemon_fingerprint: str,
) -> dict[str, Any]:
    pairing = _create_daemon_pairing_code(
        state_root=state_root,
        standalone_python=standalone_python,
        daemon_environment=daemon_environment,
        scopes=PLATFORM_BRIDGE_SCOPES,
        expected_daemon_fingerprint=expected_daemon_fingerprint,
    )
    pairing_code = str(pairing["pairing_code"])
    daemon_fingerprint = str(pairing["daemon_fingerprint"])
    status = _request_json(
        platform_url,
        "/api/v1/local-lilies/connections",
        method="POST",
        token=platform_token,
        value={
            "idempotency_key": (
                f"{TASK_ID.lower()}.pair.{daemon_port:05d}."
                f"{daemon_fingerprint.removeprefix('sha256:')[:24]}"
            ),
            "base_url": f"http://127.0.0.1:{daemon_port}",
            "pairing_code": pairing_code,
            "expected_daemon_fingerprint": daemon_fingerprint,
        },
    )
    connections = status.get("connections") if isinstance(status, dict) else None
    if not isinstance(connections, list):
        raise EnterpriseExperimentError("platform returned no paired connection inventory")
    connected = [
        item
        for item in connections
        if isinstance(item, dict)
        and item.get("status") == "connected"
        and item.get("base_url") == f"http://127.0.0.1:{daemon_port}"
        and isinstance(item.get("daemon_fingerprint"), str)
        and secrets.compare_digest(
            str(item["daemon_fingerprint"]),
            expected_daemon_fingerprint,
        )
    ]
    if len(connected) != 1:
        raise EnterpriseExperimentError("platform did not establish one exact daemon connection")
    return connected[0]


def _exchange_model_control_token(
    *,
    state_root: Path,
    standalone_python: Path,
    daemon_environment: Mapping[str, str],
    daemon_url: str,
    expected_daemon_fingerprint: str,
) -> tuple[str, dict[str, Any]]:
    pairing = _create_daemon_pairing_code(
        state_root=state_root,
        standalone_python=standalone_python,
        daemon_environment=daemon_environment,
        scopes=MODEL_ACCESS_CONTROL_SCOPES,
        expected_daemon_fingerprint=expected_daemon_fingerprint,
    )
    exchange = _request_json(
        daemon_url,
        "/local/v1/pairings/exchange",
        method="POST",
        value={
            "pairing_code": pairing["pairing_code"],
            "client_name": "cli:t01h-local-model-control",
            "requested_scopes": list(MODEL_ACCESS_CONTROL_SCOPES),
            "client_nonce": secrets.token_urlsafe(32),
        },
        timeout=10.0,
    )
    if not isinstance(exchange, Mapping):
        raise EnterpriseExperimentError("local model control pairing returned invalid data")
    token = exchange.get("access_token")
    expires_at = exchange.get("expires_at")
    try:
        client_id = str(UUID(str(exchange.get("client_id"))))
        expiry = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise EnterpriseExperimentError("local model control token identity is invalid") from error
    current_time = datetime.now(timezone.utc)
    if (
        not isinstance(token, str)
        or len(token) < 32
        or exchange.get("granted_scopes") != sorted(MODEL_ACCESS_CONTROL_SCOPES)
        or exchange.get("daemon_fingerprint") != expected_daemon_fingerprint
        or expiry.tzinfo is None
        or not current_time < expiry <= current_time + timedelta(seconds=360)
    ):
        raise EnterpriseExperimentError("local model control token is invalid")
    safe = {
        "client_id": client_id,
        "granted_scopes": list(exchange["granted_scopes"]),
        "expires_at": str(expires_at),
        "daemon_fingerprint": expected_daemon_fingerprint,
        "token_persisted": False,
    }
    return token, safe


def _disable_local_model_access(
    *,
    state_root: Path,
    standalone_python: Path,
    daemon_environment: Mapping[str, str],
    daemon_url: str,
    expected_daemon_fingerprint: str,
) -> None:
    token, _ = _exchange_model_control_token(
        state_root=state_root,
        standalone_python=standalone_python,
        daemon_environment=daemon_environment,
        daemon_url=daemon_url,
        expected_daemon_fingerprint=expected_daemon_fingerprint,
    )
    result = _request_json(
        daemon_url,
        "/local/v1/control/model-access",
        method="PUT",
        token=token,
        value={"enabled": False},
        timeout=10.0,
    )
    if not isinstance(result, Mapping) or result.get("model_egress_enabled") is not False:
        raise EnterpriseExperimentError("local model access cleanup was not confirmed")


def _authorize_local_model_access(
    stack: ExitStack,
    *,
    state_root: Path,
    standalone_python: Path,
    daemon_environment: Mapping[str, str],
    daemon_url: str,
    expected_daemon_fingerprint: str,
    provider_identity: Mapping[str, Any],
) -> dict[str, Any]:
    if provider_identity.get("provider") != "ollama-local":
        raise EnterpriseExperimentError("local model authorization provider is invalid")
    token, pairing = _exchange_model_control_token(
        state_root=state_root,
        standalone_python=standalone_python,
        daemon_environment=daemon_environment,
        daemon_url=daemon_url,
        expected_daemon_fingerprint=expected_daemon_fingerprint,
    )
    result = _request_json(
        daemon_url,
        "/local/v1/control/model-access",
        method="PUT",
        token=token,
        value={"enabled": True, "confirmation": "allow_local_model_calls"},
        timeout=10.0,
    )
    if (
        not isinstance(result, Mapping)
        or result.get("provider") != "ollama-local"
        or result.get("model") != provider_identity.get("model")
        or result.get("model_egress_enabled") is not True
        or result.get("provider_credential_loaded") is not False
        or result.get("active_turns_draining") != 0
    ):
        raise EnterpriseExperimentError("local model access authorization was not confirmed")
    stack.callback(
        _disable_local_model_access,
        state_root=state_root,
        standalone_python=standalone_python,
        daemon_environment=daemon_environment,
        daemon_url=daemon_url,
        expected_daemon_fingerprint=expected_daemon_fingerprint,
    )
    unsigned = {
        "schema_version": "v0.4.13-t01h-local-model-authorization-1",
        "provider_identity_digest": provider_identity.get("receipt_digest"),
        "daemon_fingerprint": expected_daemon_fingerprint,
        "confirmation": "allow_local_model_calls",
        "credential_loaded": False,
        "pairing": pairing,
        "authorized_at": _now(),
    }
    return {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}


def _create_application(
    platform_url: str,
    platform_token: str,
    *,
    seed: str,
) -> dict[str, Any]:
    application = _request_json(
        platform_url,
        "/api/v1/applications",
        method="POST",
        token=platform_token,
        value={
            "name": f"{TASK_ID} · {seed} · workflow pending",
            "description": (
                "Formal task and environment seed only. The application is not a "
                "completed business workflow until its requirement, graph, tests, "
                "and delivery evidence are present."
            ),
            "requirement": "",
            "mode": "workflow",
            "delivery_mode": "guided",
            "governed_hard_gate": True,
        },
    )
    if not isinstance(application, dict) or not isinstance(application.get("id"), str):
        raise EnterpriseExperimentError("platform did not create an empty application")
    draft = _request_json(
        platform_url,
        f"/api/v1/applications/{application['id']}/draft",
        token=platform_token,
    )
    snapshot = draft.get("snapshot") if isinstance(draft, Mapping) else None
    workflow = snapshot.get("workflow") if isinstance(snapshot, Mapping) else None
    if (
        not isinstance(draft, Mapping)
        or draft.get("application_id") != application["id"]
        or draft.get("revision") != 0
        or not isinstance(draft.get("content_hash"), str)
        or not isinstance(snapshot, Mapping)
        or snapshot.get("requirement") != ""
        or snapshot.get("capability_build_contract") is not None
        or snapshot.get("agents") != {}
        or snapshot.get("tests") != []
        or not isinstance(workflow, Mapping)
        or workflow.get("nodes") != []
        or workflow.get("edges") != []
        or application.get("active_version") is not None
        or application.get("draft_revision") != 0
    ):
        raise EnterpriseExperimentError("platform application did not expose an exact empty draft")
    empty_unsigned = {
        "schema_version": "v0.4.13-t01h-empty-draft-1",
        "application_id": application["id"],
        "draft_revision": 0,
        "draft_content_hash": draft["content_hash"],
        "requirement_empty": True,
        "workflow_node_count": 0,
        "workflow_edge_count": 0,
        "agent_count": 0,
        "test_count": 0,
        "active_version": None,
        "observed_at": _now(),
    }
    application = {
        **application,
        "runner_empty_draft_receipt": {
            **empty_unsigned,
            "receipt_digest": _digest(_canonical_json(empty_unsigned)),
        },
    }
    return application


def _start_formal_build(
    platform_url: str,
    platform_token: str,
    *,
    application_id: str,
    connection_id: str,
    seed: str,
    environment_instance_id: str,
) -> dict[str, Any]:
    environment_key = hashlib.sha256(environment_instance_id.encode("utf-8")).hexdigest()
    result = _request_json(
        platform_url,
        f"/api/v1/local-lilies/applications/{application_id}/formal-builds",
        method="POST",
        token=platform_token,
        value={
            "idempotency_key": (
                f"{TASK_ID.lower()}.formal.seed-{seed}.revision-{REVISION}."
                f"environment-{environment_key[:24]}"
            ),
            "connection_id": connection_id,
            "task_id": TASK_ID,
            "revision": REVISION,
            "environment_instance_id": environment_instance_id,
            "user_notified": True,
        },
        timeout=180.0,
    )
    if not isinstance(result, dict) or not isinstance(result.get("assignment_id"), str):
        raise EnterpriseExperimentError("platform did not return a formal assignment")
    return result


def _set_auto_forward(
    platform_url: str,
    platform_token: str,
    *,
    assignment_id: str,
) -> dict[str, Any]:
    inventory = _request_json(
        platform_url,
        "/api/v1/studio/collaboration/channels?limit=500",
        token=platform_token,
    )
    channels = inventory.get("channels") if isinstance(inventory, dict) else None
    if not isinstance(channels, list):
        raise EnterpriseExperimentError("formal collaboration channel inventory is invalid")
    matching = [
        item
        for item in channels
        if isinstance(item, dict) and item.get("assignment_id") == assignment_id
    ]
    if len(matching) != 1:
        raise EnterpriseExperimentError("formal collaboration channel is not uniquely visible")
    channel = matching[0]
    if channel.get("approval_mode") == "auto_forward":
        return channel
    return _request_json(
        platform_url,
        f"/api/v1/studio/collaboration/channels/{channel['channel_id']}/settings",
        method="PATCH",
        token=platform_token,
        value={
            "expected_channel_revision": channel["revision"],
            "approval_mode": "auto_forward",
            "confirmed": True,
            "idempotency_key": f"{TASK_ID.lower()}.auto-forward.{assignment_id}",
        },
    )


def _task_local_workspace_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise EnterpriseExperimentError("unattended workspace permission path is not canonical")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise EnterpriseExperimentError("unattended workspace permission path is not canonical")
    parts = tuple(part for part in path.parts if part != ".")
    if not parts or any(part in {"", ".."} for part in parts):
        raise EnterpriseExperimentError("unattended workspace permission path is not canonical")
    denied = {item.casefold() for item in TASK_LOCAL_DENIED_SEGMENTS}
    if any(part.casefold() in denied for part in parts):
        raise EnterpriseExperimentError("unattended workspace permission targets a denied segment")
    canonical = PurePosixPath(*parts).as_posix()
    if canonical != value or parts[0] not in TASK_LOCAL_WRITABLE_PREFIXES:
        raise EnterpriseExperimentError(
            "unattended workspace permission exceeds the frozen writable prefixes"
        )
    return canonical


def _task_local_permission_idempotency_key(
    *,
    task_id: str,
    task_revision: int,
    assignment_id: str,
    session_id: str,
    request_id: str,
    input_digest: str,
) -> str:
    bindings = {
        "task_id": task_id,
        "task_revision": task_revision,
        "assignment_id": assignment_id,
        "session_id": session_id,
        "permission_request_id": request_id,
        "input_digest": input_digest,
    }
    if (
        not all(
            isinstance(bindings[field], str) and bool(bindings[field])
            for field in (
                "task_id",
                "assignment_id",
                "session_id",
                "permission_request_id",
                "input_digest",
            )
        )
        or isinstance(task_revision, bool)
        or not isinstance(task_revision, int)
        or task_revision < 1
    ):
        raise EnterpriseExperimentError("task-local permission idempotency bindings are invalid")
    digest = hashlib.sha256(_canonical_json(bindings)).hexdigest()
    return f"task-local-permission:{digest}"


def _pending_studio_permission(
    platform_url: str,
    platform_token: str,
    *,
    assignment: Mapping[str, Any],
) -> dict[str, Any]:
    assignment_id = str(assignment.get("assignment_id") or "")
    session_id = str(assignment.get("session_id") or "")
    inventory = _request_json(
        platform_url,
        "/api/v1/studio/collaboration/channels?limit=500",
        token=platform_token,
    )
    channels = inventory.get("channels") if isinstance(inventory, dict) else None
    if not isinstance(channels, list):
        raise EnterpriseExperimentError("formal collaboration channel inventory is invalid")
    matching = [
        item
        for item in channels
        if isinstance(item, dict) and item.get("assignment_id") == assignment_id
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("channel_id"), str):
        raise EnterpriseExperimentError("formal collaboration channel is not uniquely visible")
    detail = _request_json(
        platform_url,
        (f"/api/v1/studio/collaboration/channels/{matching[0]['channel_id']}"),
        token=platform_token,
    )
    context = detail.get("context") if isinstance(detail, dict) else None
    context_assignment = context.get("assignment") if isinstance(context, dict) else None
    if (
        not isinstance(context_assignment, dict)
        or context_assignment.get("task_id") != TASK_ID
        or context_assignment.get("task_revision") != REVISION
        or context_assignment.get("assignment_id") != assignment_id
        or context_assignment.get("session_id") != session_id
        or context_assignment.get("daemon_status") != "waiting_permission"
    ):
        raise EnterpriseExperimentError(
            "pending permission is not bound to the frozen formal assignment"
        )
    events = context.get("observable_events")
    if not isinstance(events, list):
        raise EnterpriseExperimentError("formal collaboration permission timeline is invalid")
    resolved: set[str] = set()
    pending: list[tuple[int, dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        request_id = event.get("permission_request_id")
        if isinstance(request_id, str) and request_id:
            resolved.add(request_id)
        request = event.get("permission_request")
        if (
            isinstance(request, dict)
            and request.get("status") == "pending"
            and isinstance(request.get("request_id"), str)
            and request["request_id"] not in resolved
        ):
            pending.append((int(event.get("seq") or 0), request))
    unresolved = [
        (seq, request) for seq, request in pending if request["request_id"] not in resolved
    ]
    if len(unresolved) != 1:
        raise EnterpriseExperimentError(
            "formal assignment does not expose one exact pending permission"
        )
    return dict(max(unresolved, key=lambda item: item[0])[1])


def _builder_provenance_receipt(
    platform_url: str,
    platform_token: str,
    *,
    assignment: Mapping[str, Any],
    discovery: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
    qualified_verification: Mapping[str, Any],
) -> dict[str, Any]:
    assignment_id = str(assignment.get("assignment_id") or "")
    application_id = str(assignment.get("application_id") or "")
    session_id = str(assignment.get("session_id") or "")
    inventory = _request_json(
        platform_url,
        "/api/v1/studio/collaboration/channels?limit=500",
        token=platform_token,
    )
    channels = inventory.get("channels") if isinstance(inventory, Mapping) else None
    matching = [
        item
        for item in channels or []
        if isinstance(item, Mapping) and item.get("assignment_id") == assignment_id
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("channel_id"), str):
        raise EnterpriseExperimentError("Builder provenance channel is not uniquely visible")
    detail = _request_json(
        platform_url,
        f"/api/v1/studio/collaboration/channels/{matching[0]['channel_id']}",
        token=platform_token,
    )
    context = detail.get("context") if isinstance(detail, Mapping) else None
    context_assignment = context.get("assignment") if isinstance(context, Mapping) else None
    events = context.get("observable_events") if isinstance(context, Mapping) else None
    applications = context.get("applications") if isinstance(context, Mapping) else None
    if (
        not isinstance(context_assignment, Mapping)
        or context_assignment.get("assignment_id") != assignment_id
        or context_assignment.get("application_id") != application_id
        or context_assignment.get("session_id") != session_id
        or context_assignment.get("task_id") != TASK_ID
        or context_assignment.get("task_revision") != REVISION
        or not isinstance(events, list)
        or not isinstance(applications, list)
    ):
        raise EnterpriseExperimentError("Builder provenance context is invalid")
    application_context = [
        item
        for item in applications
        if isinstance(item, Mapping) and item.get("application_id") == application_id
    ]
    if len(application_context) != 1:
        raise EnterpriseExperimentError("Builder provenance application is not unique")
    draft = application_context[0].get("draft")
    if not isinstance(draft, Mapping) or int(draft.get("revision") or 0) <= 0:
        raise EnterpriseExperimentError("Builder provenance contains no workflow mutation")
    public_tool_events: list[dict[str, Any]] = []
    previous_seq = 0
    for event in events:
        if not isinstance(event, Mapping):
            raise EnterpriseExperimentError("Builder provenance event is invalid")
        seq = event.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq <= previous_seq:
            raise EnterpriseExperimentError("Builder provenance event sequence is invalid")
        previous_seq = seq
        if event.get("kind") != "tool":
            continue
        if event.get("actor") != "lilies":
            raise EnterpriseExperimentError("workflow tool activity was not attributed to Lilies")
        public_tool_events.append(
            {
                "seq": seq,
                "event_type": event.get("event_type"),
                "actor": "lilies",
                "tool_name": event.get("tool_name"),
                "status": event.get("status"),
            }
        )
    completed_tools = [
        item for item in public_tool_events if item.get("event_type") == "tool.completed"
    ]
    if not completed_tools:
        raise EnterpriseExperimentError(
            "Builder provenance contains no completed Lilies tool activity"
        )
    verification = _validated_receipt(
        qualified_verification,
        schema_version="1.1",
        label="platform independent verification",
    )
    verification_digest_fields = (
        "task_package_digest",
        "environment_ready_digest",
        "archive_manifest_digest",
        "frozen_context_digest",
        "verification_process_digest",
    )
    expected_verification_binding = _digest(
        _canonical_json(
            {
                "assignment_id": assignment_id,
                "claim_id": verification.get("claim_id"),
                **{field: verification.get(field) for field in verification_digest_fields},
                "validation_mode": "real_host",
            }
        )
    )
    if (
        verification.get("assignment_id") != assignment_id
        or verification.get("claim_status") != "independently_verified"
        or verification.get("verdict") != "independently_verified"
        or verification.get("difference_count") != 0
        or verification.get("validation_mode") != "real_host"
        or any(
            not isinstance(verification.get(field), str)
            or DAEMON_FINGERPRINT_PATTERN.fullmatch(str(verification[field])) is None
            for field in verification_digest_fields
        )
        or verification.get("frozen_verification_binding_digest") != expected_verification_binding
    ):
        raise EnterpriseExperimentError(
            "Builder provenance lacks qualified frozen mutation evidence"
        )
    if (
        runtime_identity.get("builder_actor") != "lilies"
        or not isinstance(runtime_identity.get("sibling_commit"), str)
        or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(runtime_identity["sibling_commit"]))
        is None
        or not isinstance(runtime_identity.get("package_digest"), str)
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(str(runtime_identity["package_digest"])) is None
        or discovery.get("daemon_fingerprint")
        != assignment.get("daemon_fingerprint", discovery.get("daemon_fingerprint"))
    ):
        raise EnterpriseExperimentError("Builder runtime provenance is invalid")
    mutation_provenance = {
        "status": "append_only_formal_mutation_chain_qualified_transitively",
        "qualified_actor_kind": "lilies_blackbox",
        "qualification_source": "schema_1.1_real_host_frozen_archive_verification_process",
        "contracts": list(FROZEN_MUTATION_QUALIFICATION_CONTRACTS),
        "claim_id": verification["claim_id"],
        **{field: verification[field] for field in verification_digest_fields},
        "validation_mode": "real_host",
        "independent_verification_receipt_digest": verification["receipt_digest"],
    }
    public_tool_attribution = {
        "status": "all_observed_public_tool_events_attributed_to_lilies",
        "event_count": len(public_tool_events),
        "completed_event_count": len(completed_tools),
        "events_digest": _digest(_canonical_json(public_tool_events)),
    }
    unsigned = {
        "schema_version": "v0.4.13-t01h-builder-provenance-2",
        "builder_actor": "lilies",
        "assignment_id": assignment_id,
        "application_id": application_id,
        "session_id": session_id,
        "daemon_fingerprint": discovery.get("daemon_fingerprint"),
        "sibling_commit": runtime_identity["sibling_commit"],
        "sibling_package_digest": runtime_identity["package_digest"],
        "sibling_distribution": runtime_identity.get("distribution"),
        "sibling_version": runtime_identity.get("version"),
        "final_draft_revision": int(draft["revision"]),
        "mutation_provenance": mutation_provenance,
        "public_tool_attribution": public_tool_attribution,
        "observed_at": _now(),
    }
    return {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}


def _resolve_task_local_workspace_permission(
    platform_url: str,
    platform_token: str,
    *,
    assignment: Mapping[str, Any],
) -> dict[str, Any]:
    permission = _pending_studio_permission(
        platform_url,
        platform_token,
        assignment=assignment,
    )
    tool_name = permission.get("tool_name")
    request_id = permission.get("request_id")
    input_digest = permission.get("input_digest")
    redacted_input = permission.get("redacted_input")
    if (
        tool_name not in TASK_LOCAL_PERMISSION_TOOLS
        or not isinstance(request_id, str)
        or not isinstance(input_digest, str)
        or not isinstance(redacted_input, dict)
    ):
        raise EnterpriseExperimentError(
            "unattended permission is outside the task-local workspace policy"
        )
    path = _task_local_workspace_path(redacted_input.get("path"))
    assignment_id = str(assignment["assignment_id"])
    session_id = str(assignment["session_id"])
    decision = _request_json(
        platform_url,
        (f"/api/v1/local-lilies/assignments/{assignment_id}/permissions/{request_id}"),
        method="POST",
        token=platform_token,
        value={
            "idempotency_key": _task_local_permission_idempotency_key(
                task_id=TASK_ID,
                task_revision=REVISION,
                assignment_id=assignment_id,
                session_id=session_id,
                request_id=request_id,
                input_digest=input_digest,
            ),
            "behavior": "allow",
            "expected_input_digest": input_digest,
            "message": (
                "Allowed by the user-authorized unattended task-local workspace "
                "policy for this exact request and input digest."
            ),
        },
    )
    receipt = decision.get("permission") if isinstance(decision, dict) else None
    if (
        not isinstance(receipt, dict)
        or receipt.get("request_id") != request_id
        or receipt.get("status") != "allowed"
        or receipt.get("input_digest") != input_digest
    ):
        raise EnterpriseExperimentError("daemon returned an invalid unattended permission receipt")
    return {
        "request_id": request_id,
        "tool_name": tool_name,
        "input_digest": input_digest,
        "path": path,
        "status": "allowed",
    }


def _poll_assignment_inner(
    platform_url: str,
    platform_token: str,
    *,
    assignment_id: str,
    deadline_seconds: float,
    operational_permission_policy: str = "manual",
    token_state_root: Path | None = None,
    token_monitor_interval: float = 5.0,
    connection_id: str | None = None,
    token_attempt_id: str | None = None,
    session_budget_sequence: _SessionBudgetSequence | None = None,
) -> dict[str, Any]:
    if operational_permission_policy not in OPERATIONAL_PERMISSION_POLICIES:
        raise EnterpriseExperimentError("operational permission policy is invalid")
    deadline = time.monotonic() + deadline_seconds
    last: dict[str, Any] | None = None
    permission_receipts: list[dict[str, Any]] = []
    previous_token_snapshot: dict[str, Any] | None = None
    previous_token_at = time.monotonic()
    next_token_snapshot_at = 0.0
    while time.monotonic() < deadline:
        now_monotonic = time.monotonic()
        if (
            token_state_root is not None
            and token_monitor_interval > 0
            and now_monotonic >= next_token_snapshot_at
        ):
            previous_token_snapshot, previous_token_at = _record_token_monitor_snapshot(
                token_state_root,
                previous=previous_token_snapshot,
                previous_at=previous_token_at,
                observed_at=now_monotonic,
                **(
                    {
                        "platform_url": platform_url,
                        "platform_token": platform_token,
                        "connection_id": connection_id,
                    }
                    if connection_id is not None
                    else {}
                ),
                attempt_id=token_attempt_id,
                session_budget_sequence=session_budget_sequence,
            )
            next_token_snapshot_at = now_monotonic + token_monitor_interval
        relay_error: EnterpriseExperimentError | None = None
        try:
            _request_json(
                platform_url,
                f"/api/v1/local-lilies/assignments/{assignment_id}/relay",
                method="POST",
                token=platform_token,
                value={"max_events": 1000},
                timeout=30.0,
            )
        except EnterpriseExperimentError as error:
            # The following exact assignment read determines whether this was
            # a transient relay loss or a durable terminal failure.
            relay_error = error
        value = _request_json(
            platform_url,
            f"/api/v1/local-lilies/assignments/{assignment_id}",
            token=platform_token,
        )
        if not isinstance(value, dict):
            raise EnterpriseExperimentError("formal assignment status is invalid")
        last = value
        if relay_error is not None and "security_boundary_violation" in str(relay_error):
            return {
                **value,
                "runner_auto_permissions": permission_receipts,
                "runner_terminal": "relay_security_boundary_rejected",
                "runner_terminal_detail": str(relay_error),
            }
        phase = str(value.get("phase") or "")
        if phase in TERMINAL_PHASES:
            return {
                **value,
                "runner_auto_permissions": permission_receipts,
            }
        if phase == "waiting":
            if (
                operational_permission_policy == "task_local_workspace"
                and value.get("daemon_status") == "waiting_permission"
            ):
                # The relay immediately before the assignment read may finish
                # while the model's tool request is still being committed.  A
                # waiting_permission session proves the permission row and its
                # event now exist, so synchronize once more before consulting
                # the Studio projection.  This keeps the decision bound to the
                # durable, redacted collaboration event instead of reaching
                # around the platform boundary to query the daemon directly.
                try:
                    _request_json(
                        platform_url,
                        (f"/api/v1/local-lilies/assignments/{assignment_id}/relay"),
                        method="POST",
                        token=platform_token,
                        value={"max_events": 1000},
                        timeout=30.0,
                    )
                except EnterpriseExperimentError as error:
                    if "security_boundary_violation" in str(error):
                        return {
                            **value,
                            "runner_auto_permissions": permission_receipts,
                            "runner_terminal": "relay_security_boundary_rejected",
                            "runner_terminal_detail": str(error),
                        }
                    return {
                        **value,
                        "runner_auto_permissions": permission_receipts,
                        "runner_terminal": "unattended_permission_rejected",
                        "runner_terminal_detail": (
                            f"pending permission synchronization failed: {error}"
                        ),
                    }
                try:
                    receipt = _resolve_task_local_workspace_permission(
                        platform_url,
                        platform_token,
                        assignment=value,
                    )
                except EnterpriseExperimentError as error:
                    return {
                        **value,
                        "runner_auto_permissions": permission_receipts,
                        "runner_terminal": "unattended_permission_rejected",
                        "runner_terminal_detail": str(error),
                    }
                if receipt["request_id"] not in {
                    item["request_id"] for item in permission_receipts
                }:
                    permission_receipts.append(receipt)
                time.sleep(1.0)
                continue
            return {
                **value,
                "runner_auto_permissions": permission_receipts,
            }
        if (
            phase == "running"
            and value.get("status") == "ready"
            and value.get("daemon_status") == "ready"
        ):
            return {
                **value,
                "runner_terminal": "builder_ready_without_completion_claim",
                "runner_auto_permissions": permission_receipts,
            }
        time.sleep(1.0)
    if last is None:
        raise EnterpriseExperimentError("formal assignment produced no durable status")
    return {
        **last,
        "runner_timeout": True,
        "runner_auto_permissions": permission_receipts,
    }


def _poll_assignment(
    platform_url: str,
    platform_token: str,
    *,
    assignment_id: str,
    deadline_seconds: float,
    operational_permission_policy: str = "manual",
    token_state_root: Path | None = None,
    token_monitor_interval: float = 5.0,
    connection_id: str | None = None,
    token_attempt_id: str | None = None,
    expected_session_id: str | None = None,
    global_usage_baseline: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    session_budget_sequence: _SessionBudgetSequence | None = None
    if (
        token_state_root is not None
        or token_attempt_id is not None
        or expected_session_id is not None
        or global_usage_baseline is not None
    ):
        if (
            token_state_root is None
            or token_attempt_id is None
            or expected_session_id is None
            or connection_id is None
            or global_usage_baseline is None
        ):
            raise EnterpriseExperimentError("session budget monitoring identity is incomplete")
        session_budget_sequence = _SessionBudgetSequence(
            token_state_root,
            attempt_id=token_attempt_id,
            assignment_id=assignment_id,
            session_id=expected_session_id,
            global_baseline=global_usage_baseline,
        )
    result: dict[str, Any] | None = None
    try:
        result = _poll_assignment_inner(
            platform_url,
            platform_token,
            assignment_id=assignment_id,
            deadline_seconds=deadline_seconds,
            operational_permission_policy=operational_permission_policy,
            token_state_root=token_state_root,
            token_monitor_interval=token_monitor_interval,
            connection_id=connection_id,
            token_attempt_id=token_attempt_id,
            session_budget_sequence=session_budget_sequence,
        )
    finally:
        if token_state_root is not None and token_monitor_interval > 0:
            observed_at = time.monotonic()
            _record_token_monitor_snapshot(
                token_state_root,
                previous=None,
                previous_at=observed_at,
                observed_at=observed_at,
                **(
                    {
                        "platform_url": platform_url,
                        "platform_token": platform_token,
                        "connection_id": connection_id,
                    }
                    if connection_id is not None
                    else {}
                ),
                attempt_id=token_attempt_id,
                session_budget_sequence=session_budget_sequence,
            )
    if result is None:
        raise EnterpriseExperimentError("formal assignment produced no terminal result")
    if session_budget_sequence is not None:
        if result.get("session_id") != expected_session_id:
            raise EnterpriseExperimentError(
                "formal assignment changed its monitored session identity"
            )
        relay_cursor = result.get("relay_cursor")
        if isinstance(relay_cursor, bool) or not isinstance(relay_cursor, int):
            raise EnterpriseExperimentError("formal assignment relay cursor is invalid")
        event_scan = _assignment_budget_event_receipt(
            platform_url,
            platform_token,
            assignment_id=assignment_id,
            session_id=str(expected_session_id),
            relay_cursor=relay_cursor,
        )
        if event_scan["budget_exceeded"] is True:
            observed_at = time.monotonic()
            _record_token_monitor_snapshot(
                token_state_root,
                previous=None,
                previous_at=observed_at,
                observed_at=observed_at,
                platform_url=platform_url,
                platform_token=platform_token,
                connection_id=connection_id,
                attempt_id=token_attempt_id,
                session_budget_sequence=session_budget_sequence,
                budget_event=event_scan,
            )
        while session_budget_sequence.requires_stop_confirmation:
            observed_at = time.monotonic()
            _record_token_monitor_snapshot(
                token_state_root,
                previous=None,
                previous_at=observed_at,
                observed_at=observed_at,
                platform_url=platform_url,
                platform_token=platform_token,
                connection_id=connection_id,
                attempt_id=token_attempt_id,
                session_budget_sequence=session_budget_sequence,
            )
        result = {
            **result,
            "runner_session_budget_sequence": session_budget_sequence.receipt(
                event_scan=event_scan
            ),
        }
    return result


def _standalone_nonnegative_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= STANDALONE_USAGE_INTEGER_MAX
    )


def _standalone_nonnegative_cost(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 0 <= value <= STANDALONE_USAGE_COST_MAX
    return (
        isinstance(value, float)
        and math.isfinite(value)
        and 0 <= value <= STANDALONE_USAGE_COST_MAX
    )


def _standalone_observability_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != STANDALONE_OBSERVABILITY_FIELDS:
        raise EnterpriseExperimentError("standalone observability schema is invalid")
    usage = value.get("usage")
    runtime = value.get("runtime")
    startup = value.get("startup")
    if (
        not isinstance(usage, dict)
        or set(usage) != STANDALONE_OBSERVABILITY_USAGE_FIELDS
        or not isinstance(runtime, dict)
        or set(runtime) != STANDALONE_OBSERVABILITY_RUNTIME_FIELDS
        or not isinstance(startup, dict)
        or set(startup) != STANDALONE_OBSERVABILITY_STARTUP_FIELDS
    ):
        raise EnterpriseExperimentError("standalone observability sections are invalid")
    daemon_fingerprint = value.get("daemon_fingerprint")
    daemon_instance_id = value.get("daemon_instance_id")
    captured_at = value.get("captured_at")
    max_session_tokens = value.get("max_session_tokens")
    if (
        value.get("schema_version") != "1.0"
        or value.get("scope") != "daemon_global"
        or value.get("coverage_complete") is not True
        or not isinstance(daemon_fingerprint, str)
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(daemon_fingerprint) is None
        or not isinstance(daemon_instance_id, str)
        or not isinstance(captured_at, str)
        or not isinstance(value.get("model_egress_enabled"), bool)
        or isinstance(max_session_tokens, bool)
        or not isinstance(max_session_tokens, int)
        or max_session_tokens != MAX_SESSION_TOKENS
        or not _standalone_nonnegative_integer(value.get("activity_revision"))
    ):
        raise EnterpriseExperimentError("standalone observability receipt is invalid")
    try:
        if str(UUID(daemon_instance_id)) != daemon_instance_id:
            raise ValueError("non-canonical daemon instance")
        parsed_capture = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        captured_at_size = len(captured_at.encode("utf-8"))
    except (TypeError, UnicodeEncodeError, ValueError) as error:
        raise EnterpriseExperimentError(
            "standalone observability identity or timestamp is invalid"
        ) from error
    if (
        captured_at != captured_at.strip()
        or captured_at_size > 64
        or parsed_capture.tzinfo is None
        or parsed_capture.utcoffset() != timedelta(0)
    ):
        raise EnterpriseExperimentError("standalone observability timestamp is invalid")

    integer_usage_fields = (
        "attempted_calls",
        "recorded_calls",
        "unknown_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "ledger_cursor",
    )
    if any(not _standalone_nonnegative_integer(usage.get(field)) for field in integer_usage_fields):
        raise EnterpriseExperimentError("standalone observability usage counters are invalid")
    if any(
        not _standalone_nonnegative_integer(runtime.get(field))
        for field in STANDALONE_OBSERVABILITY_RUNTIME_FIELDS
    ):
        raise EnterpriseExperimentError("standalone observability runtime counters are invalid")
    if (
        runtime["active_development_model_calls"] > runtime["active_provider_calls"]
        or runtime["active_provider_calls"] - runtime["active_development_model_calls"]
        > runtime["active_model_turns"]
        or runtime["active_model_turns"] > runtime["active_sessions"]
    ):
        raise EnterpriseExperimentError("standalone observability runtime accounting is invalid")
    cost = usage.get("cost_usd")
    if (
        not _standalone_nonnegative_cost(cost)
        or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
        or usage["ledger_cursor"] < usage["attempted_calls"]
        or usage["attempted_calls"]
        != usage["recorded_calls"] + usage["unknown_calls"] + runtime["active_provider_calls"]
    ):
        raise EnterpriseExperimentError("standalone observability usage accounting is invalid")
    if (
        startup.get("recovery_completed") is not True
        or startup.get("automatic_resume_policy") != "explicit_request_only"
        or any(
            not _standalone_nonnegative_integer(startup.get(field))
            for field in STANDALONE_OBSERVABILITY_STARTUP_FIELDS
            if field not in {"recovery_completed", "automatic_resume_policy"}
        )
    ):
        raise EnterpriseExperimentError("standalone observability startup receipt is invalid")
    return value


def _standalone_observability_snapshot(
    *,
    platform_url: str | None,
    platform_token: str | None,
    connection_id: str | None,
) -> dict[str, Any] | None:
    if not platform_url or not platform_token or not connection_id:
        return None
    try:
        normalized_connection_id = str(UUID(connection_id))
    except (AttributeError, ValueError):
        return None
    if normalized_connection_id != connection_id:
        return None

    pages: list[dict[str, Any]] = []
    dimensions: set[tuple[str, str, str]] = set()
    total_items: int | None = None
    total_pages: int | None = None
    page = 1
    deadline = time.monotonic() + STANDALONE_USAGE_SNAPSHOT_TIMEOUT_SECONDS
    observability_path = (
        f"/api/v1/local-lilies/connections/{normalized_connection_id}/observability-snapshot"
    )
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EnterpriseExperimentError("standalone observability snapshot timed out")
        before = _standalone_observability_receipt(
            _request_json(
                platform_url,
                observability_path,
                token=platform_token,
                timeout=min(2.0, remaining),
            )
        )
        while total_pages is None or page <= total_pages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EnterpriseExperimentError("standalone observability snapshot timed out")
            value = _request_json(
                platform_url,
                (
                    f"/api/v1/local-lilies/connections/{normalized_connection_id}/usage"
                    "?group_by=session&group_by=stage&group_by=model"
                    f"&page={page}&page_size={STANDALONE_USAGE_PAGE_SIZE}"
                ),
                token=platform_token,
                timeout=min(2.0, remaining),
            )
            if not isinstance(value, dict) or set(value) != STANDALONE_USAGE_FIELDS:
                raise EnterpriseExperimentError("standalone usage page schema is invalid")
            items = value.get("items")
            returned_count = value.get("returned_count")
            observed_total_items = value.get("total_items")
            observed_total_pages = value.get("total_pages")
            if (
                value.get("schema_version") != "1.0"
                or value.get("group_by") != ["session", "stage", "model"]
                or isinstance(value.get("page"), bool)
                or not isinstance(value.get("page"), int)
                or value.get("page") != page
                or isinstance(value.get("page_size"), bool)
                or not isinstance(value.get("page_size"), int)
                or value.get("page_size") != STANDALONE_USAGE_PAGE_SIZE
                or value.get("truncated") is not False
                or not isinstance(items, list)
                or len(items) > STANDALONE_USAGE_PAGE_SIZE
                or isinstance(returned_count, bool)
                or not isinstance(returned_count, int)
                or returned_count != len(items)
                or isinstance(observed_total_items, bool)
                or not isinstance(observed_total_items, int)
                or observed_total_items < 0
                or observed_total_items > STANDALONE_USAGE_PAGE_SIZE * STANDALONE_USAGE_MAX_PAGES
                or isinstance(observed_total_pages, bool)
                or not isinstance(observed_total_pages, int)
                or not 0 <= observed_total_pages <= STANDALONE_USAGE_MAX_PAGES
            ):
                raise EnterpriseExperimentError("standalone usage page receipt is invalid")
            expected_pages = (
                0
                if observed_total_items == 0
                else (observed_total_items + STANDALONE_USAGE_PAGE_SIZE - 1)
                // STANDALONE_USAGE_PAGE_SIZE
            )
            if observed_total_pages != expected_pages:
                raise EnterpriseExperimentError("standalone usage page count is invalid")
            if total_items is None:
                total_items = observed_total_items
                total_pages = observed_total_pages
            elif observed_total_items != total_items or observed_total_pages != total_pages:
                raise EnterpriseExperimentError("standalone usage pages drifted")
            expected_returned = min(
                STANDALONE_USAGE_PAGE_SIZE,
                max(
                    0,
                    observed_total_items - ((page - 1) * STANDALONE_USAGE_PAGE_SIZE),
                ),
            )
            if returned_count != expected_returned:
                raise EnterpriseExperimentError("standalone usage page is incomplete")
            for item in items:
                if (
                    not isinstance(item, dict)
                    or set(item) != STANDALONE_USAGE_ITEM_FIELDS
                    or not isinstance(item.get("session_id"), str)
                    or not isinstance(item.get("stage"), str)
                    or not isinstance(item.get("model"), str)
                ):
                    raise EnterpriseExperimentError("standalone usage item schema is invalid")
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
                    or not 0 <= item[field] <= STANDALONE_USAGE_INTEGER_MAX
                    for field in integer_fields
                ):
                    raise EnterpriseExperimentError("standalone usage item counters are invalid")
                cost = item.get("cost_usd")
                if (
                    not _standalone_nonnegative_cost(cost)
                    or item["total_tokens"] != item["input_tokens"] + item["output_tokens"]
                    or item["recorded_calls"] + item["unknown_calls"] > STANDALONE_USAGE_INTEGER_MAX
                ):
                    raise EnterpriseExperimentError("standalone usage item accounting is invalid")
                try:
                    normalized_session_id = str(UUID(item["session_id"]))
                except (AttributeError, ValueError) as error:
                    raise EnterpriseExperimentError(
                        "standalone usage session identity is invalid"
                    ) from error
                if (
                    normalized_session_id != item["session_id"]
                    or not item["stage"]
                    or item["stage"] != item["stage"].strip()
                    or len(item["stage"].encode("utf-8")) > 120
                    or not item["model"]
                    or item["model"] != item["model"].strip()
                    or len(item["model"].encode("utf-8")) > 200
                ):
                    raise EnterpriseExperimentError("standalone usage dimensions are invalid")
                dimension = (
                    normalized_session_id,
                    item["stage"],
                    item["model"],
                )
                if dimension in dimensions:
                    raise EnterpriseExperimentError(
                        "standalone usage pages contain duplicate dimensions"
                    )
                dimensions.add(dimension)
                pages.append(item)
            if observed_total_pages == 0:
                break
            page += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise EnterpriseExperimentError("standalone observability snapshot timed out")
        after = _standalone_observability_receipt(
            _request_json(
                platform_url,
                observability_path,
                token=platform_token,
                timeout=min(2.0, remaining),
            )
        )

        before_usage = before["usage"]
        after_usage = after["usage"]
        before_captured_at = datetime.fromisoformat(before["captured_at"].replace("Z", "+00:00"))
        after_captured_at = datetime.fromisoformat(after["captured_at"].replace("Z", "+00:00"))
        if (
            before["daemon_fingerprint"] != after["daemon_fingerprint"]
            or before["daemon_instance_id"] != after["daemon_instance_id"]
            or before_usage["ledger_cursor"] != after_usage["ledger_cursor"]
            or any(
                (
                    Decimal(str(before_usage[field])) != Decimal(str(after_usage[field]))
                    if field == "cost_usd"
                    else before_usage[field] != after_usage[field]
                )
                for field in STANDALONE_OBSERVABILITY_USAGE_COUNTER_FIELDS
            )
            or after_captured_at < before_captured_at
            or after["activity_revision"] < before["activity_revision"]
        ):
            raise EnterpriseExperimentError("standalone observability bracket drifted")
        if total_items is None or total_pages is None or len(pages) != total_items:
            raise EnterpriseExperimentError("standalone usage merge is incomplete")

        acl_totals: dict[str, int | Decimal] = {
            "recorded_calls": 0,
            "unknown_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost_usd": Decimal("0"),
        }
        for item in pages:
            for field in (
                "recorded_calls",
                "unknown_calls",
                "input_tokens",
                "output_tokens",
                "total_tokens",
            ):
                acl_totals[field] = int(acl_totals[field]) + int(item[field])
            acl_totals["cost_usd"] = Decimal(str(acl_totals["cost_usd"])) + Decimal(
                str(item["cost_usd"])
            )
        for field in (
            "recorded_calls",
            "unknown_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        ):
            if (
                int(acl_totals[field]) > int(after_usage[field])
                or int(acl_totals[field]) > STANDALONE_USAGE_INTEGER_MAX
            ):
                raise EnterpriseExperimentError(
                    "standalone ACL usage exceeds the daemon-global ledger"
                )
        if Decimal(str(acl_totals["cost_usd"])) > Decimal(str(after_usage["cost_usd"])) or Decimal(
            str(acl_totals["cost_usd"])
        ) > Decimal(str(STANDALONE_USAGE_COST_MAX)):
            raise EnterpriseExperimentError("standalone ACL cost exceeds the daemon-global ledger")
        merged_usage = {
            "schema_version": "1.0",
            "group_by": ["session", "stage", "model"],
            "items": pages,
            "page": 1,
            "page_size": STANDALONE_USAGE_PAGE_SIZE,
            "returned_count": total_items,
            "total_items": total_items,
            "total_pages": total_pages,
            "truncated": False,
            "snapshot_kind": "complete_paginated_merge",
        }
        return {
            "schema_version": "1.0",
            "snapshot_kind": "paired_observability_bracket",
            "before": before,
            "client_acl_usage": merged_usage,
            "after": after,
        }
    except (
        ArithmeticError,
        EnterpriseExperimentError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        return None


def _session_budget_receipt(
    snapshot: Mapping[str, Any] | None,
    *,
    assignment: Mapping[str, Any],
    discovery: Mapping[str, Any],
    sequence: Mapping[str, Any] | None,
    global_baseline: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        raise EnterpriseExperimentError("public standalone runtime budget receipt is unavailable")
    before = snapshot.get("before")
    after = snapshot.get("after")
    usage = snapshot.get("client_acl_usage")
    session_id = assignment.get("session_id")
    try:
        canonical_session_id = str(UUID(str(session_id)))
    except (AttributeError, TypeError, ValueError) as error:
        raise EnterpriseExperimentError(
            "public session budget receipt identity is invalid"
        ) from error
    if (
        snapshot.get("schema_version") != "1.0"
        or snapshot.get("snapshot_kind") != "paired_observability_bracket"
        or not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or not isinstance(usage, Mapping)
        or not isinstance(session_id, str)
        or canonical_session_id != session_id
    ):
        raise EnterpriseExperimentError("public session budget receipt identity is invalid")
    assignment_id = assignment.get("assignment_id")
    try:
        canonical_assignment_id = str(UUID(str(assignment_id)))
    except (AttributeError, TypeError, ValueError) as error:
        raise EnterpriseExperimentError(
            "public session budget receipt assignment is invalid"
        ) from error
    if not isinstance(assignment_id, str) or canonical_assignment_id != assignment_id:
        raise EnterpriseExperimentError("public session budget receipt assignment is invalid")
    fingerprint = discovery.get("daemon_fingerprint")
    if (
        not isinstance(fingerprint, str)
        or before.get("daemon_fingerprint") != fingerprint
        or after.get("daemon_fingerprint") != fingerprint
        or before.get("max_session_tokens") != MAX_SESSION_TOKENS
        or after.get("max_session_tokens") != MAX_SESSION_TOKENS
        or before.get("model_egress_enabled") is not True
        or after.get("model_egress_enabled") is not True
    ):
        raise EnterpriseExperimentError("public runtime did not attest the exact session token cap")
    after_runtime = after.get("runtime")
    if not isinstance(after_runtime, Mapping) or after_runtime.get("active_provider_calls") != 0:
        raise EnterpriseExperimentError("public runtime still has an in-flight provider call")
    items = usage.get("items")
    if (
        usage.get("schema_version") != "1.0"
        or usage.get("snapshot_kind") != "complete_paginated_merge"
        or usage.get("group_by") != ["session", "stage", "model"]
        or usage.get("truncated") is not False
        or not isinstance(items, list)
        or usage.get("returned_count") != len(items)
        or usage.get("total_items") != len(items)
    ):
        raise EnterpriseExperimentError("public per-session usage receipt is invalid")
    matched = [
        item for item in items if isinstance(item, Mapping) and item.get("session_id") == session_id
    ]
    totals = {
        "recorded_calls": sum(int(item["recorded_calls"]) for item in matched),
        "unknown_calls": sum(int(item["unknown_calls"]) for item in matched),
        "input_tokens": sum(int(item["input_tokens"]) for item in matched),
        "output_tokens": sum(int(item["output_tokens"]) for item in matched),
        "total_tokens": sum(int(item["total_tokens"]) for item in matched),
        "cost_usd": round(sum(float(item["cost_usd"]) for item in matched), 12),
    }
    reconciled = _session_usage_checkpoint(
        snapshot,
        session_id=session_id,
        global_baseline=global_baseline,
    )
    if reconciled is None or reconciled.get("global_delta_exact") is not True:
        raise EnterpriseExperimentError(
            "public per-session usage is not exactly globally reconciled"
        )
    if not matched or totals["recorded_calls"] <= 0:
        raise EnterpriseExperimentError("public per-session usage contains no completed model call")
    if totals["total_tokens"] != totals["input_tokens"] + totals["output_tokens"]:
        raise EnterpriseExperimentError("public per-session token totals are inconsistent")
    if totals["unknown_calls"] != 0:
        raise EnterpriseExperimentError(
            "public per-session usage contains an unknown provider call"
        )
    if totals["total_tokens"] > MAX_SESSION_TOKENS:
        raise EnterpriseExperimentError("public per-session token cap was exceeded")
    sequence_unsigned = (
        {key: value for key, value in sequence.items() if key != "receipt_digest"}
        if isinstance(sequence, Mapping)
        else {}
    )
    sequence_digest = sequence.get("receipt_digest") if isinstance(sequence, Mapping) else None
    sequence_last = sequence.get("last_checkpoint") if isinstance(sequence, Mapping) else None
    event_scan = sequence.get("event_scan") if isinstance(sequence, Mapping) else None
    event_aggregate = (
        event_scan.get("final_usage_aggregate") if isinstance(event_scan, Mapping) else None
    )
    hard_stop = sequence.get("hard_stop") if isinstance(sequence, Mapping) else None
    try:
        sequence_cost = (
            Decimal(str(sequence_last.get("cost_usd")))
            if isinstance(sequence_last, Mapping)
            else None
        )
    except ArithmeticError:
        sequence_cost = None
    if (
        not isinstance(sequence, Mapping)
        or sequence.get("schema_version") != "v0.4.13-t01h-session-budget-sequence-1"
        or sequence.get("status") != "complete"
        or sequence.get("assignment_id") != assignment_id
        or sequence.get("session_id") != session_id
        or sequence.get("max_session_tokens") != MAX_SESSION_TOKENS
        or sequence.get("global_baseline") != _validated_global_usage_baseline(global_baseline)
        or not isinstance(sequence_last, Mapping)
        or sequence_last.get("unknown_calls") != 0
        or sequence_last.get("total_tokens") != totals["total_tokens"]
        or sequence_last.get("recorded_calls") != totals["recorded_calls"]
        or sequence_last.get("input_tokens") != totals["input_tokens"]
        or sequence_last.get("output_tokens") != totals["output_tokens"]
        or sequence_cost != Decimal(str(totals["cost_usd"]))
        or not isinstance(event_scan, Mapping)
        or event_scan.get("status") != "complete"
        or event_scan.get("post_cap_usage_event_count") != 0
        or not isinstance(event_aggregate, Mapping)
        or any(
            Decimal(str(event_aggregate.get(field))) != Decimal(str(reconciled.get(field)))
            for field in (*SESSION_USAGE_COUNTER_FIELDS, "cost_usd")
        )
        or (
            event_scan.get("budget_exceeded") is True
            and (
                not isinstance(hard_stop, Mapping)
                or hard_stop.get("status") != "hard_stop_attested"
                or int(hard_stop.get("post_trigger_confirmation_count") or 0)
                < MIN_POST_STOP_CONFIRMATIONS
                or event_scan.get("event_aggregate") != event_aggregate
                or not isinstance(event_scan.get("preceding_usage_event_seq"), int)
            )
        )
        or not isinstance(sequence_digest, str)
        or not secrets.compare_digest(
            sequence_digest,
            _digest(_canonical_json(sequence_unsigned)),
        )
    ):
        raise EnterpriseExperimentError(
            "public per-session budget sequence is incomplete or inconsistent"
        )
    receipt = {
        "schema_version": "v0.4.13-t01h-session-budget-1",
        "assignment_id": assignment.get("assignment_id"),
        "session_id": session_id,
        "daemon_fingerprint": fingerprint,
        "daemon_instance_id": after.get("daemon_instance_id"),
        "captured_at": after.get("captured_at"),
        "max_session_tokens": MAX_SESSION_TOKENS,
        "runtime_cap_attested": True,
        "usage_group_by": ["session", "stage", "model"],
        "usage": totals,
        "global_reconciliation": {
            "baseline": _validated_global_usage_baseline(global_baseline),
            "final_ledger_cursor": reconciled["ledger_cursor"],
            "global_delta": reconciled["global_delta"],
            "status": "exact",
        },
        "sequence": dict(sequence),
        "status": "within_cap_complete_usage",
    }
    return {**receipt, "receipt_digest": _digest(_canonical_json(receipt))}


def _attempt_storage_key(attempt_id: str) -> str:
    if re.fullmatch(r"sha256:[0-9a-f]{64}", attempt_id) is None:
        raise EnterpriseExperimentError("run attempt identity is invalid")
    return attempt_id.removeprefix("sha256:")


def _token_monitor_root(state_root: Path, attempt_id: str) -> Path:
    return state_root / "monitoring" / "attempts" / _attempt_storage_key(attempt_id)


def _global_usage_baseline(
    snapshot: Mapping[str, Any] | None,
    *,
    require_fresh: bool,
) -> dict[str, Any]:
    """Freeze the daemon-global usage immediately before assignment authority."""

    if not isinstance(snapshot, Mapping):
        raise EnterpriseExperimentError("assignment global usage baseline is unavailable")
    after = snapshot.get("after")
    if not isinstance(after, Mapping):
        raise EnterpriseExperimentError("assignment global usage baseline is invalid")
    usage = after.get("usage")
    runtime = after.get("runtime")
    if not isinstance(usage, Mapping) or not isinstance(runtime, Mapping):
        raise EnterpriseExperimentError("assignment global usage baseline is invalid")
    if any(int(runtime[field]) != 0 for field in STANDALONE_OBSERVABILITY_RUNTIME_FIELDS):
        raise EnterpriseExperimentError("assignment global usage baseline has active model work")
    counters = {
        field: int(usage[field]) for field in (*SESSION_USAGE_COUNTER_FIELDS, "ledger_cursor")
    }
    counters["cost_usd"] = float(Decimal(str(usage["cost_usd"])))
    if require_fresh and (
        any(counters[field] != 0 for field in SESSION_USAGE_COUNTER_FIELDS)
        or Decimal(str(counters["cost_usd"])) != 0
    ):
        raise EnterpriseExperimentError(
            "fresh daemon already contains model usage before assignment"
        )
    unsigned = {
        "schema_version": "v0.4.13-t01h-global-usage-baseline-1",
        "daemon_fingerprint": after.get("daemon_fingerprint"),
        "daemon_instance_id": after.get("daemon_instance_id"),
        "captured_at": after.get("captured_at"),
        **counters,
    }
    return {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}


def _validated_global_usage_baseline(value: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = {key: item for key, item in value.items() if key != "receipt_digest"}
    digest = value.get("receipt_digest")
    if (
        value.get("schema_version") != "v0.4.13-t01h-global-usage-baseline-1"
        or not isinstance(value.get("daemon_fingerprint"), str)
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(str(value["daemon_fingerprint"])) is None
        or not isinstance(value.get("daemon_instance_id"), str)
        or any(
            not _standalone_nonnegative_integer(value.get(field))
            for field in (*SESSION_USAGE_COUNTER_FIELDS, "ledger_cursor")
        )
        or not _standalone_nonnegative_cost(value.get("cost_usd"))
        or not isinstance(digest, str)
        or not secrets.compare_digest(digest, _digest(_canonical_json(unsigned)))
    ):
        raise EnterpriseExperimentError("assignment global usage baseline receipt is invalid")
    return dict(value)


def _session_usage_checkpoint(
    snapshot: Mapping[str, Any] | None,
    *,
    session_id: str,
    global_baseline: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return one exactly reconciled session/global cumulative checkpoint."""

    if not isinstance(snapshot, Mapping):
        return None
    baseline = _validated_global_usage_baseline(global_baseline)
    usage = snapshot.get("client_acl_usage")
    after = snapshot.get("after")
    if not isinstance(usage, Mapping) or not isinstance(after, Mapping):
        return None
    items = usage.get("items")
    global_usage = after.get("usage")
    runtime = after.get("runtime")
    if (
        not isinstance(items, list)
        or not isinstance(global_usage, Mapping)
        or not isinstance(runtime, Mapping)
    ):
        return None
    matched = [
        item for item in items if isinstance(item, Mapping) and item.get("session_id") == session_id
    ]
    recorded_calls = sum(int(item["recorded_calls"]) for item in matched)
    unknown_calls = sum(int(item["unknown_calls"]) for item in matched)
    input_tokens = sum(int(item["input_tokens"]) for item in matched)
    output_tokens = sum(int(item["output_tokens"]) for item in matched)
    total_tokens = sum(int(item["total_tokens"]) for item in matched)
    cost = sum(Decimal(str(item["cost_usd"])) for item in matched)
    if total_tokens != input_tokens + output_tokens:
        raise EnterpriseExperimentError("public session usage totals are inconsistent")
    if unknown_calls != 0:
        raise EnterpriseExperimentError("public session usage contains an unknown provider call")
    if total_tokens > MAX_SESSION_TOKENS:
        raise EnterpriseExperimentError("public per-session token cap was exceeded")
    if (
        after.get("daemon_fingerprint") != baseline["daemon_fingerprint"]
        or after.get("daemon_instance_id") != baseline["daemon_instance_id"]
    ):
        raise EnterpriseExperimentError("public session usage changed its baseline daemon identity")
    global_delta: dict[str, int | float] = {}
    for field in SESSION_USAGE_COUNTER_FIELDS:
        delta = int(global_usage[field]) - int(baseline[field])
        if delta < 0:
            raise EnterpriseExperimentError(
                f"daemon-global usage counter regressed from assignment baseline: {field}"
            )
        global_delta[field] = delta
    cost_delta = Decimal(str(global_usage["cost_usd"])) - Decimal(str(baseline["cost_usd"]))
    if cost_delta < 0:
        raise EnterpriseExperimentError(
            "daemon-global usage cost regressed from assignment baseline"
        )
    global_delta["cost_usd"] = float(cost_delta)
    ledger_cursor = int(global_usage["ledger_cursor"])
    if ledger_cursor < int(baseline["ledger_cursor"]):
        raise EnterpriseExperimentError(
            "daemon-global usage ledger regressed from assignment baseline"
        )
    session_totals: dict[str, int | float] = {
        "attempted_calls": (recorded_calls + unknown_calls + int(runtime["active_provider_calls"])),
        "recorded_calls": recorded_calls,
        "unknown_calls": unknown_calls,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cost_usd": float(cost),
    }
    for field in SESSION_USAGE_EXACT_DELTA_FIELDS:
        observed = Decimal(str(global_delta[field]))
        expected = Decimal(str(session_totals[field]))
        if observed != expected:
            raise EnterpriseExperimentError(
                f"daemon-global usage delta is not exactly attributable to the session: {field}"
            )
    return {
        "daemon_fingerprint": after.get("daemon_fingerprint"),
        "daemon_instance_id": after.get("daemon_instance_id"),
        "captured_at": after.get("captured_at"),
        "baseline_ledger_cursor": int(baseline["ledger_cursor"]),
        "ledger_cursor": ledger_cursor,
        **session_totals,
        "global_delta": global_delta,
        "global_delta_exact": True,
    }


class _SessionBudgetSequence:
    """Persist and validate the public cumulative usage of one exact session."""

    _MONOTONIC_INTEGER_FIELDS = SESSION_USAGE_MONOTONIC_FIELDS
    _FROZEN_AFTER_STOP_FIELDS = (*SESSION_USAGE_MONOTONIC_FIELDS, "cost_usd")
    _EVENT_AGGREGATE_FIELDS = (*SESSION_USAGE_COUNTER_FIELDS, "cost_usd")

    def __init__(
        self,
        state_root: Path,
        *,
        attempt_id: str,
        assignment_id: str,
        session_id: str,
        global_baseline: Mapping[str, Any],
    ) -> None:
        self._root = _token_monitor_root(state_root, attempt_id)
        self._attempt_id = attempt_id
        self._assignment_id = str(UUID(assignment_id))
        self._session_id = str(UUID(session_id))
        self._global_baseline = _validated_global_usage_baseline(global_baseline)
        self._previous: dict[str, Any] | None = None
        self._observation_count = 0
        self._snapshot_count = 0
        self._coverage_gaps = 0
        self._hard_stop: dict[str, Any] | None = None
        self._post_stop_confirmations = 0
        self._load_state()

    @property
    def requires_stop_confirmation(self) -> bool:
        return (
            self._hard_stop is not None
            and self._post_stop_confirmations < MIN_POST_STOP_CONFIRMATIONS
        )

    def _persist(self, value: Mapping[str, Any]) -> None:
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _atomic_private_json(self._root / "session-budget-sequence.latest.json", value)
        descriptor = os.open(
            self._root / "session-budget-sequence.jsonl",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            os.write(descriptor, _canonical_json(value) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._persist_state()

    def _state_payload(self) -> dict[str, Any]:
        unsigned = {
            "schema_version": "v0.4.13-t01h-session-budget-state-1",
            "attempt_id": self._attempt_id,
            "assignment_id": self._assignment_id,
            "session_id": self._session_id,
            "global_baseline": self._global_baseline,
            "previous": self._previous,
            "observation_count": self._observation_count,
            "snapshot_count": self._snapshot_count,
            "coverage_gap_count": self._coverage_gaps,
            "hard_stop": self._hard_stop,
            "post_stop_confirmation_count": self._post_stop_confirmations,
        }
        return {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}

    def _persist_state(self) -> None:
        _atomic_private_json(
            self._root / "session-budget-sequence.state.json",
            self._state_payload(),
        )

    def _load_state(self) -> None:
        path = self._root / "session-budget-sequence.state.json"
        if not path.exists():
            return
        state = _read_private_json(path)
        unsigned = {key: value for key, value in state.items() if key != "receipt_digest"}
        digest = state.get("receipt_digest")
        if (
            state.get("schema_version") != "v0.4.13-t01h-session-budget-state-1"
            or state.get("attempt_id") != self._attempt_id
            or state.get("assignment_id") != self._assignment_id
            or state.get("session_id") != self._session_id
            or state.get("global_baseline") != self._global_baseline
            or not isinstance(digest, str)
            or not secrets.compare_digest(digest, _digest(_canonical_json(unsigned)))
        ):
            raise EnterpriseExperimentError(
                "persisted session budget sequence changed its attempt binding"
            )
        for field in (
            "observation_count",
            "snapshot_count",
            "coverage_gap_count",
            "post_stop_confirmation_count",
        ):
            if not _standalone_nonnegative_integer(state.get(field)):
                raise EnterpriseExperimentError(
                    "persisted session budget sequence counters are invalid"
                )
        previous = state.get("previous")
        hard_stop = state.get("hard_stop")
        if previous is not None and not isinstance(previous, dict):
            raise EnterpriseExperimentError("persisted session budget checkpoint is invalid")
        if hard_stop is not None and not isinstance(hard_stop, dict):
            raise EnterpriseExperimentError("persisted session hard stop is invalid")
        self._previous = previous
        self._observation_count = int(state["observation_count"])
        self._snapshot_count = int(state["snapshot_count"])
        self._coverage_gaps = int(state["coverage_gap_count"])
        self._hard_stop = hard_stop
        self._post_stop_confirmations = int(state["post_stop_confirmation_count"])

    def observe(
        self,
        snapshot: Mapping[str, Any] | None,
        *,
        budget_event: Mapping[str, Any] | None = None,
    ) -> None:
        self._observation_count += 1
        checkpoint = _session_usage_checkpoint(
            snapshot,
            session_id=self._session_id,
            global_baseline=self._global_baseline,
        )
        if checkpoint is None:
            self._coverage_gaps += 1
            self._persist(
                {
                    "schema_version": "v0.4.13-t01h-session-budget-checkpoint-1",
                    "attempt_id": self._attempt_id,
                    "assignment_id": self._assignment_id,
                    "session_id": self._session_id,
                    "sequence_index": self._observation_count,
                    "status": "coverage_gap",
                }
            )
            return
        previous = self._previous
        if previous is not None:
            for field in self._MONOTONIC_INTEGER_FIELDS:
                if int(checkpoint[field]) < int(previous[field]):
                    raise EnterpriseExperimentError(
                        f"public session usage counter regressed: {field}"
                    )
            if Decimal(str(checkpoint["cost_usd"])) < Decimal(str(previous["cost_usd"])):
                raise EnterpriseExperimentError("public session usage cost regressed")
            if (
                checkpoint["daemon_fingerprint"] != previous["daemon_fingerprint"]
                or checkpoint["daemon_instance_id"] != previous["daemon_instance_id"]
            ):
                raise EnterpriseExperimentError("public session usage changed its daemon identity")
        if self._hard_stop is not None:
            frozen = self._hard_stop["frozen_counters"]
            if any(checkpoint[field] != frozen[field] for field in self._FROZEN_AFTER_STOP_FIELDS):
                raise EnterpriseExperimentError(
                    "provider usage advanced after the session hard stop"
                )
            previous_confirmation = self._hard_stop.get("last_confirmation_captured_at")
            captured_at = checkpoint.get("captured_at")
            if not isinstance(captured_at, str) or (
                previous_confirmation is not None and captured_at <= previous_confirmation
            ):
                raise EnterpriseExperimentError(
                    "post-stop usage confirmations are not distinct later snapshots"
                )
            self._post_stop_confirmations += 1
            self._hard_stop["last_confirmation_captured_at"] = captured_at
        trigger = None
        if budget_event is not None:
            trigger = "budget.exceeded"
        elif checkpoint["total_tokens"] == MAX_SESSION_TOKENS:
            trigger = "token_cap_reached"
        if trigger is not None and self._hard_stop is None:
            if trigger == "budget.exceeded":
                event_aggregate = budget_event.get("event_aggregate")
                if not isinstance(event_aggregate, Mapping):
                    raise EnterpriseExperimentError(
                        "budget.exceeded omitted its durable usage aggregate"
                    )
                for field in self._EVENT_AGGREGATE_FIELDS:
                    observed = Decimal(str(checkpoint[field]))
                    expected = Decimal(str(event_aggregate.get(field)))
                    if observed != expected:
                        raise EnterpriseExperimentError(
                            "provider usage advanced after budget.exceeded"
                        )
                event_created_at = budget_event.get("event_created_at")
                if (
                    not isinstance(event_created_at, str)
                    or not isinstance(checkpoint.get("captured_at"), str)
                    or str(checkpoint["captured_at"]) < event_created_at
                ):
                    raise EnterpriseExperimentError(
                        "budget.exceeded was not followed by a later usage snapshot"
                    )
            self._hard_stop = {
                "trigger": trigger,
                "trigger_sequence_index": self._observation_count,
                "event_seq": (None if budget_event is None else budget_event.get("event_seq")),
                "frozen_counters": {
                    field: checkpoint[field] for field in self._FROZEN_AFTER_STOP_FIELDS
                },
                "last_confirmation_captured_at": checkpoint.get("captured_at"),
            }
        self._snapshot_count += 1
        record = {
            "schema_version": "v0.4.13-t01h-session-budget-checkpoint-1",
            "attempt_id": self._attempt_id,
            "assignment_id": self._assignment_id,
            "session_id": self._session_id,
            "sequence_index": self._observation_count,
            "status": "validated",
            "checkpoint": checkpoint,
            "hard_stop_active": self._hard_stop is not None,
        }
        self._previous = checkpoint
        self._persist(record)

    def receipt(self, *, event_scan: Mapping[str, Any]) -> dict[str, Any]:
        hard_stop = None
        if self._hard_stop is not None:
            hard_stop = {
                **self._hard_stop,
                "post_trigger_confirmation_count": self._post_stop_confirmations,
                "status": (
                    "hard_stop_attested"
                    if self._post_stop_confirmations >= MIN_POST_STOP_CONFIRMATIONS
                    else "hard_stop_unconfirmed"
                ),
            }
        complete = bool(
            self._snapshot_count >= 2
            and self._coverage_gaps == 0
            and event_scan.get("status") == "complete"
            and event_scan.get("post_cap_usage_event_count", 0) == 0
            and (hard_stop is None or hard_stop["status"] == "hard_stop_attested")
        )
        if hard_stop is not None and event_scan.get("budget_exceeded") is True:
            aggregate = event_scan.get("event_aggregate")
            if not isinstance(aggregate, Mapping) or self._previous is None:
                complete = False
            elif any(
                Decimal(str(self._previous[field])) != Decimal(str(aggregate.get(field)))
                for field in self._EVENT_AGGREGATE_FIELDS
            ):
                complete = False
        unsigned = {
            "schema_version": "v0.4.13-t01h-session-budget-sequence-1",
            "attempt_id": self._attempt_id,
            "assignment_id": self._assignment_id,
            "session_id": self._session_id,
            "max_session_tokens": MAX_SESSION_TOKENS,
            "global_baseline": self._global_baseline,
            "snapshot_count": self._snapshot_count,
            "observation_count": self._observation_count,
            "coverage_gap_count": self._coverage_gaps,
            "event_scan": dict(event_scan),
            "last_checkpoint": self._previous,
            "hard_stop": hard_stop,
            "status": "complete" if complete else "incomplete",
        }
        receipt = {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}
        _atomic_private_json(self._root / "session-budget-sequence.receipt.json", receipt)
        return receipt


def _assignment_budget_event_receipt(
    platform_url: str,
    platform_token: str,
    *,
    assignment_id: str,
    session_id: str,
    relay_cursor: int,
) -> dict[str, Any]:
    """Bind the token fence to durable model-call events and their exact aggregate."""

    if relay_cursor < 0:
        raise EnterpriseExperimentError("formal assignment relay cursor is invalid")
    zero_aggregate = {
        "attempted_calls": 0,
        "recorded_calls": 0,
        "unknown_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }
    if relay_cursor == 0:
        return {
            "status": "complete",
            "scanned_through_cursor": 0,
            "budget_exceeded": False,
            "event_seq": None,
            "event_created_at": None,
            "event_aggregate": None,
            "preceding_usage_event_seq": None,
            "usage_event_count": 0,
            "usage_event_digest": _digest(_canonical_json([])),
            "final_usage_aggregate": zero_aggregate,
            "post_cap_usage_event_count": 0,
            "post_event_count": 0,
        }
    request = Request(
        (
            f"{platform_url.rstrip('/')}/api/v1/local-lilies/assignments/"
            f"{assignment_id}/events?after=0"
        ),
        method="GET",
        headers={
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {platform_token}",
            "User-Agent": "Lilies-EXP-LILIES-001-Runner/1.0",
        },
    )
    highest_cursor = 0
    budget_event_seq: int | None = None
    budget_event_created_at: str | None = None
    budget_event_aggregate: dict[str, Any] | None = None
    budget_reason: str | None = None
    preceding_usage_event_seq: int | None = None
    post_event_count = 0
    post_cap_usage_event_count = 0
    event_count = 0
    usage_events: list[dict[str, Any]] = []
    usage_call_ids: set[str] = set()
    aggregate = dict(zero_aggregate)
    last_usage_event_seq: int | None = None
    byte_count = 0
    event_name = ""
    event_id = ""
    data_lines: list[str] = []

    def consume() -> None:
        nonlocal highest_cursor, budget_event_seq, budget_event_created_at
        nonlocal budget_event_aggregate, budget_reason, preceding_usage_event_seq
        nonlocal post_event_count, post_cap_usage_event_count, event_count
        nonlocal event_name, event_id, data_lines, last_usage_event_seq
        if not event_id and not data_lines:
            event_name = ""
            return
        try:
            cursor = int(event_id)
            payload = json.loads("\n".join(data_lines))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise EnterpriseExperimentError("public assignment event replay is invalid") from error
        if (
            not isinstance(payload, dict)
            or payload.get("assignment_id") != assignment_id
            or payload.get("session_id") != session_id
            or payload.get("seq") != cursor
            or payload.get("daemon_seq") != cursor
            or payload.get("event_type") != event_name
        ):
            raise EnterpriseExperimentError("public assignment event identity is invalid")
        if cursor <= highest_cursor:
            raise EnterpriseExperimentError("public assignment event cursor regressed")
        highest_cursor = cursor
        event_count += 1
        if event_count > STANDALONE_EVENT_STREAM_MAX_EVENTS:
            raise EnterpriseExperimentError("public assignment event replay exceeds its limit")
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise EnterpriseExperimentError("public assignment event data is invalid")
        if event_name == "usage.model_call":
            if budget_event_seq is not None:
                post_cap_usage_event_count += 1
                raise EnterpriseExperimentError("provider usage advanced after budget.exceeded")
            required = {
                "session_id",
                "turn_id",
                "call_id",
                "stage",
                "model",
                "call_index",
                "usage_status",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cost_usd",
            }
            if not required.issubset(data) or set(data) - (required | {"unknown_reason"}):
                raise EnterpriseExperimentError("usage.model_call schema is invalid")
            call_id = data.get("call_id")
            turn_id = data.get("turn_id")
            call_index = data.get("call_index")
            if (
                data.get("session_id") != session_id
                or not isinstance(turn_id, str)
                or not turn_id
                or not isinstance(call_id, str)
                or not call_id
                or len(call_id) > 256
                or call_id in usage_call_ids
                or isinstance(call_index, bool)
                or not isinstance(call_index, int)
                or call_index < 1
                or not isinstance(data.get("stage"), str)
                or not isinstance(data.get("model"), str)
            ):
                raise EnterpriseExperimentError("usage.model_call identity is invalid")
            if data.get("usage_status") != "recorded":
                raise EnterpriseExperimentError(
                    "public assignment contains an unknown provider call"
                )
            if any(
                not _standalone_nonnegative_integer(data.get(field))
                for field in ("input_tokens", "output_tokens", "total_tokens")
            ) or (
                data.get("cost_usd") is not None
                and not _standalone_nonnegative_cost(data.get("cost_usd"))
            ):
                raise EnterpriseExperimentError("usage.model_call accounting is invalid")
            if int(data["total_tokens"]) != int(data["input_tokens"]) + int(data["output_tokens"]):
                raise EnterpriseExperimentError("usage.model_call totals are inconsistent")
            usage_call_ids.add(call_id)
            aggregate["attempted_calls"] += 1
            aggregate["recorded_calls"] += 1
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                aggregate[field] += int(data[field])
            aggregate["cost_usd"] = round(
                float(
                    Decimal(str(aggregate["cost_usd"])) + Decimal(str(data.get("cost_usd") or 0))
                ),
                12,
            )
            last_usage_event_seq = cursor
            usage_events.append(
                {
                    "seq": cursor,
                    "call_id_digest": _digest(call_id.encode("utf-8")),
                    "call_index": call_index,
                    "input_tokens": int(data["input_tokens"]),
                    "output_tokens": int(data["output_tokens"]),
                    "total_tokens": int(data["total_tokens"]),
                    "cost_usd": (None if data.get("cost_usd") is None else float(data["cost_usd"])),
                }
            )
            if int(aggregate["total_tokens"]) > MAX_SESSION_TOKENS:
                raise EnterpriseExperimentError("public per-session token cap was exceeded")
        elif event_name == "budget.usage_unknown":
            if budget_event_seq is not None:
                post_cap_usage_event_count += 1
            raise EnterpriseExperimentError("public assignment contains an unknown provider call")
        elif event_name == "budget.exceeded" and "budget_tokens" in data:
            if budget_event_seq is not None:
                raise EnterpriseExperimentError(
                    "public assignment emitted more than one token budget event"
                )
            allowed_reasons = {
                "session token limit reached before model call": set(),
                "next model call could cross the session token limit": {"reserved_tokens"},
                "model call crossed the session token limit": {"call_index"},
            }
            reason = data.get("reason")
            extras = {
                "session_id",
                "turn_id",
                "budget_tokens",
                "recorded_tokens",
                "reason",
            } | allowed_reasons.get(str(reason), set())
            if (
                reason not in allowed_reasons
                or set(data) != extras
                or data.get("session_id") != session_id
                or not isinstance(data.get("turn_id"), str)
                or not data.get("turn_id")
                or data.get("budget_tokens") != MAX_SESSION_TOKENS
                or not _standalone_nonnegative_integer(data.get("recorded_tokens"))
                or int(data["recorded_tokens"]) != int(aggregate["total_tokens"])
                or last_usage_event_seq is None
                or last_usage_event_seq >= cursor
            ):
                raise EnterpriseExperimentError("budget.exceeded token schema is invalid")
            if int(data["recorded_tokens"]) > MAX_SESSION_TOKENS:
                raise EnterpriseExperimentError("public per-session token cap was exceeded")
            if (
                reason == "session token limit reached before model call"
                and int(data["recorded_tokens"]) != MAX_SESSION_TOKENS
            ):
                raise EnterpriseExperimentError("budget.exceeded token fence is inconsistent")
            if reason == "next model call could cross the session token limit":
                reserved = data.get("reserved_tokens")
                if (
                    not _standalone_nonnegative_integer(reserved)
                    or int(reserved) <= 0
                    or int(data["recorded_tokens"]) + int(reserved) <= MAX_SESSION_TOKENS
                ):
                    raise EnterpriseExperimentError("budget.exceeded reservation is invalid")
            if reason == "model call crossed the session token limit":
                call_index = data.get("call_index")
                if (
                    isinstance(call_index, bool)
                    or not isinstance(call_index, int)
                    or call_index < 1
                ):
                    raise EnterpriseExperimentError("budget.exceeded call index is invalid")
            budget_event_seq = cursor
            budget_event_created_at = payload.get("created_at")
            if not isinstance(budget_event_created_at, str):
                raise EnterpriseExperimentError("budget.exceeded timestamp is invalid")
            budget_event_aggregate = dict(aggregate)
            budget_reason = str(reason)
            preceding_usage_event_seq = last_usage_event_seq
        elif budget_event_seq is not None:
            post_event_count += 1
        event_name = ""
        event_id = ""
        data_lines = []

    try:
        with _HTTP_OPENER.open(request, timeout=10.0) as response:
            for raw_line in response:
                byte_count += len(raw_line)
                if byte_count > STANDALONE_EVENT_STREAM_MAX_BYTES:
                    raise EnterpriseExperimentError(
                        "public assignment event replay exceeds its byte limit"
                    )
                line = raw_line.decode("utf-8", errors="strict").rstrip("\r\n")
                if not line:
                    consume()
                    if highest_cursor >= relay_cursor:
                        break
                elif line.startswith("id: "):
                    event_id = line[4:]
                elif line.startswith("event: "):
                    event_name = line[7:]
                elif line.startswith("data: "):
                    data_lines.append(line[6:])
    except HTTPError as error:
        raise EnterpriseExperimentError("public assignment event replay request failed") from error
    except (UnicodeDecodeError, URLError, OSError, TimeoutError) as error:
        raise EnterpriseExperimentError("public assignment event replay failed") from error
    if highest_cursor != relay_cursor:
        raise EnterpriseExperimentError(
            "public assignment event replay did not reach its durable cursor"
        )
    return {
        "status": "complete",
        "scanned_through_cursor": highest_cursor,
        "budget_exceeded": budget_event_seq is not None,
        "event_seq": budget_event_seq,
        "event_created_at": budget_event_created_at,
        "event_aggregate": budget_event_aggregate,
        "event_reason": budget_reason,
        "preceding_usage_event_seq": preceding_usage_event_seq,
        "usage_event_count": len(usage_events),
        "usage_event_digest": _digest(_canonical_json(usage_events)),
        "final_usage_aggregate": aggregate,
        "post_cap_usage_event_count": post_cap_usage_event_count,
        "post_event_count": post_event_count,
    }


def _token_monitor_observability_projection(
    snapshot: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Project the cap-extended public receipt into the monitor's frozen v1 schema."""
    if snapshot is None:
        return None
    projected = json.loads(json.dumps(snapshot))
    if not isinstance(projected, dict):
        raise EnterpriseExperimentError("standalone observability projection is invalid")
    for boundary in ("before", "after"):
        receipt = projected.get(boundary)
        if isinstance(receipt, dict):
            receipt.pop("max_session_tokens", None)
    return projected


def _record_token_monitor_snapshot(
    state_root: Path,
    *,
    previous: dict[str, Any] | None,
    previous_at: float,
    observed_at: float,
    platform_url: str | None = None,
    platform_token: str | None = None,
    connection_id: str | None = None,
    attempt_id: str | None = None,
    session_budget_sequence: _SessionBudgetSequence | None = None,
    budget_event: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], float]:
    standalone_observability = _standalone_observability_snapshot(
        platform_url=platform_url,
        platform_token=platform_token,
        connection_id=connection_id,
    )
    if session_budget_sequence is not None:
        session_budget_sequence.observe(
            standalone_observability,
            budget_event=budget_event,
        )
    snapshot = collect_token_monitor_snapshot(
        platform_db=state_root / "platform-data" / "agent_platform.db",
        bridge_db=state_root / "platform-data" / "local-lilies-bridge.db",
        development_db=(state_root / "platform-data" / "collaborative-development.db"),
        standalone_observability_snapshot=(
            _token_monitor_observability_projection(standalone_observability)
        ),
        required_sources=(
            "platform",
            "standalone_lilies",
            "bridge",
            "collaborative_development",
        ),
        model_egress_enabled=True,
    )
    delta = (
        snapshot_delta(
            previous,
            snapshot,
            elapsed_seconds=max(0.001, observed_at - previous_at),
        )
        if previous is not None
        else None
    )
    projection = {
        "schema_version": "v0.4.13-t01h-token-monitor-1",
        "attempt_id": attempt_id,
        "observed_at": snapshot["generated_at"],
        "safety": snapshot["safety"],
        "totals": snapshot["usage"]["totals"],
        "by_stage": snapshot["usage"]["by_stage"],
        "by_model": snapshot["usage"]["by_model"],
        "delta": delta,
        "source_status": {
            "standalone_lilies": {
                key: snapshot["sources"]["standalone_lilies"].get(key)
                for key in (
                    "available",
                    "schema_valid",
                    "unavailable_reason",
                    "boundary",
                    "active_work_evidence_complete",
                )
            }
        },
        "active": {
            "processes": snapshot["processes"],
            "platform_tasks": snapshot["sources"]["platform"]["active_tasks"],
            "local_sessions": snapshot["sources"]["standalone_lilies"]["active_sessions"],
            "recoverable_assignments": snapshot["sources"]["bridge"]["recoverable_assignments"],
            "development_assignments": snapshot["sources"]["collaborative_development"][
                "active_assignments"
            ],
        },
    }
    monitor_root = (
        state_root / "monitoring"
        if attempt_id is None
        else _token_monitor_root(state_root, attempt_id)
    )
    _atomic_private_json(monitor_root / "token-monitor.latest.json", projection)
    line = _canonical_json(projection) + b"\n"
    history_path = monitor_root / "token-monitor.jsonl"
    descriptor = os.open(
        history_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    totals = projection["totals"]
    delta_tokens = int(delta["tokens"]) if delta is not None else 0
    standalone_coverage = (
        "complete"
        if projection["source_status"]["standalone_lilies"]["available"] is True
        else "unknown"
    )
    print(
        "[token-monitor] "
        f"observed_tokens={int(totals['tokens']):,} "
        f"delta={delta_tokens:+,} "
        f"calls={int(totals['model_calls']):,} "
        f"cost_usd={float(totals['cost_usd']):.6f} "
        f"standalone_coverage={standalone_coverage}",
        file=sys.stderr,
        flush=True,
    )
    return snapshot, observed_at


def _safe_error_projection(value: object | None) -> dict[str, str] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        category = "upstream_unavailable"
        try:
            raw_for_digest = _canonical_json(dict(value))
        except (TypeError, ValueError):
            raw_for_digest = repr(type(value)).encode("utf-8", errors="replace")
    elif isinstance(value, EnterpriseExperimentError):
        category = "enterprise_experiment_error"
        raw_for_digest = repr(value).encode("utf-8", errors="replace")
    elif isinstance(value, (KeyboardInterrupt, SystemExit)):
        category = "interrupted"
        raw_for_digest = repr(value).encode("utf-8", errors="replace")
    elif isinstance(value, subprocess.SubprocessError):
        category = "subprocess_error"
        raw_for_digest = repr(value).encode("utf-8", errors="replace")
    elif isinstance(value, (HTTPError, URLError, TimeoutError)):
        category = "upstream_unavailable"
        raw_for_digest = repr(value).encode("utf-8", errors="replace")
    elif isinstance(value, OSError):
        category = "local_io_error"
        raw_for_digest = repr(value).encode("utf-8", errors="replace")
    else:
        category = "runner_error"
        raw_for_digest = repr(value).encode("utf-8", errors="replace")
    return {
        "code": category,
        "summary": ERROR_PROJECTIONS[category],
        "digest": _digest(raw_for_digest),
    }


def _print_safe_error(value: object) -> None:
    projection = _safe_error_projection(value) or {
        "code": "runner_error",
        "summary": ERROR_PROJECTIONS["runner_error"],
        "digest": _digest(b"runner_error"),
    }
    print(
        json.dumps(projection, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


def _safe_assignment_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "assignment_id",
        "application_id",
        "build_id",
        "session_id",
        "connection_id",
        "phase",
        "status",
        "desired_state",
        "daemon_status",
        "relay_cursor",
        "ack_cursor",
        "created_at",
        "updated_at",
        "runner_timeout",
        "runner_terminal",
    }
    projection = {key: value[key] for key in sorted(allowed) if key in value}
    if value.get("last_error") is not None:
        projection["last_error"] = _safe_error_projection(value.get("last_error"))
    if value.get("runner_terminal_detail") is not None:
        projection["runner_terminal_detail"] = _safe_error_projection(
            value.get("runner_terminal_detail")
        )
    permissions = value.get("runner_auto_permissions")
    if isinstance(permissions, list):
        projection["runner_auto_permissions"] = [
            {
                key: item.get(key)
                for key in ("request_id", "tool_name", "input_digest", "path", "status")
            }
            for item in permissions
            if isinstance(item, Mapping)
        ]
    return projection


def _builder_lifecycle_outcome(value: Mapping[str, Any]) -> str:
    runner_terminal = value.get("runner_terminal")
    if runner_terminal in {
        "builder_ready_without_completion_claim",
        "relay_security_boundary_rejected",
        "unattended_permission_rejected",
    }:
        return str(runner_terminal)
    if value.get("runner_timeout") is True:
        return "timeout"
    phase = value.get("phase")
    if phase in {"completed", "failed", "cancelled", "waiting", "running"}:
        return str(phase)
    return "unknown"


def _assert_assignment_identity(
    value: Mapping[str, Any],
    *,
    assignment_id: str | None = None,
    application_id: str,
    connection_id: str,
    session_id: str | None = None,
    build_id: str | None = None,
) -> dict[str, str]:
    expected = {
        "application_id": application_id,
        "connection_id": connection_id,
    }
    if assignment_id is not None:
        expected["assignment_id"] = assignment_id
    if session_id is not None:
        expected["session_id"] = session_id
    if build_id is not None:
        expected["build_id"] = build_id
    identity: dict[str, str] = {}
    for field in ("assignment_id", "application_id", "build_id", "session_id", "connection_id"):
        observed = value.get(field)
        try:
            normalized = str(UUID(str(observed)))
        except (AttributeError, TypeError, ValueError) as error:
            raise EnterpriseExperimentError(f"formal assignment {field} is invalid") from error
        if normalized != observed:
            raise EnterpriseExperimentError(f"formal assignment {field} is not canonical")
        identity[field] = normalized
        if field in expected and not secrets.compare_digest(normalized, expected[field]):
            raise EnterpriseExperimentError(
                f"formal assignment {field} changed its persisted identity"
            )
    return identity


def _platform_verification_passed(value: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("verdict") == "independently_verified"
        and value.get("claim_status") == "independently_verified"
    )


def _enterprise_run_status(
    assignment: Mapping[str, Any],
    *,
    host_verification: Mapping[str, Any] | None,
    platform_verification: Mapping[str, Any] | None,
    session_budget: Mapping[str, Any] | None,
) -> str:
    terminal = assignment.get("runner_terminal")
    if terminal == "builder_ready_without_completion_claim":
        return "assignment_builder_incomplete"
    if terminal == "unattended_permission_rejected":
        return "assignment_unattended_permission_rejected"
    if terminal == "relay_security_boundary_rejected":
        return "assignment_relay_security_rejected"
    if terminal is not None:
        return "assignment_failed"
    if assignment.get("runner_timeout") is True:
        return "assignment_timeout"
    phase = assignment.get("phase")
    if phase == "waiting":
        return "assignment_waiting_user"
    if phase in {"failed", "cancelled"}:
        return "assignment_failed"
    if phase != "completed":
        return "assignment_timeout"
    if assignment.get("status") != "completed" or assignment.get("daemon_status") != "completed":
        return "assignment_terminal_status_inconsistent"
    if (
        not isinstance(session_budget, Mapping)
        or session_budget.get("status") != "within_cap_complete_usage"
        or session_budget.get("runtime_cap_attested") is not True
        or not isinstance(session_budget.get("sequence"), Mapping)
        or session_budget["sequence"].get("status") != "complete"
    ):
        return "assignment_session_budget_unverified"
    if (
        not isinstance(host_verification, Mapping)
        or host_verification.get("verdict") != "independently_verified"
        or not _platform_verification_passed(platform_verification)
    ):
        return "assignment_completed_verification_failed"
    return "enterprise_run_passed"


def _snapshot_summary(path: Path) -> dict[str, Any]:
    value = _read_private_json(path)
    return {
        "phase": value.get("phase"),
        "seed": value.get("seed"),
        "record_count": value.get("record_count"),
        "request_log_count": value.get("request_log_count"),
        "digest": _digest(path.read_bytes()),
    }


def _host_snapshot_verifier_environment() -> dict[str, str]:
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
    }


def _run_host_snapshot_verification(
    state_root: Path,
    *,
    seed: str,
) -> dict[str, Any]:
    snapshot_path = state_root / "environment" / f"host-snapshot-{seed}-final.json"
    snapshot_digest = _digest(snapshot_path.read_bytes())
    oracle_path = TASK_ROOT / "protected" / "oracle" / "host-oracle.json"
    oracle_digest = _digest(oracle_path.read_bytes())
    verification_identity = _digest(
        _canonical_json(
            {
                "snapshot_digest": snapshot_digest,
                "oracle_digest": oracle_digest,
                "verifier_digest": _digest(HOST_SNAPSHOT_VERIFIER.read_bytes()),
            }
        )
    )
    output_path = (
        state_root
        / "environment"
        / (f"host-verification-{seed}-{verification_identity.removeprefix('sha256:')}.json")
    )
    if not output_path.exists():
        completed = subprocess.run(
            (
                sys.executable,
                str(HOST_SNAPSHOT_VERIFIER),
                "--snapshot",
                str(snapshot_path),
                "--oracle",
                str(oracle_path),
                "--output",
                str(output_path),
            ),
            cwd=ROOT,
            env=_host_snapshot_verifier_environment(),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if completed.returncode not in {0, 3}:
            raise EnterpriseExperimentError(
                "independent host snapshot verifier rejected its input boundary"
            )
    result = _read_private_json(output_path)
    if (
        result.get("task_id") != TASK_ID
        or result.get("revision") != REVISION
        or str(result.get("seed")) != seed
        or result.get("snapshot_digest") != snapshot_digest
        or result.get("oracle_digest") != oracle_digest
        or result.get("verdict") not in {"independently_verified", "verification_failed"}
    ):
        raise EnterpriseExperimentError(
            "independent host snapshot result changed its frozen binding"
        )
    return {
        "verdict": result["verdict"],
        "check_count": result.get("check_count"),
        "passed_check_count": result.get("passed_check_count"),
        "difference_count": len(result.get("differences") or []),
        "oracle_digest": result.get("oracle_digest"),
        "snapshot_digest": snapshot_digest,
        "result_digest": _digest(output_path.read_bytes()),
    }


def _run_platform_independent_verification(
    platform_url: str,
    platform_token: str,
    *,
    assignment_id: str,
) -> dict[str, Any]:
    result = _request_json(
        platform_url,
        (f"/api/v1/local-lilies/assignments/{assignment_id}/independent-verification"),
        method="POST",
        token=platform_token,
        timeout=300.0,
    )
    if not isinstance(result, dict):
        raise EnterpriseExperimentError(
            "platform independent verification returned an invalid result"
        )
    verification = result.get("verification")
    if not isinstance(verification, dict):
        raise EnterpriseExperimentError(
            "platform independent verification omitted its persisted result"
        )
    try:
        canonical_assignment_id = str(UUID(assignment_id))
        claim_id = str(UUID(str(result.get("claim_id"))))
        verification_id = str(UUID(str(verification.get("verification_id"))))
    except (TypeError, ValueError) as error:
        raise EnterpriseExperimentError(
            "platform independent verification identity is invalid"
        ) from error
    verdict = verification.get("verdict")
    claim_status = result.get("claim_status")
    differences = verification.get("differences")
    digest_fields = (
        "task_package_digest",
        "environment_ready_digest",
        "archive_manifest_digest",
        "frozen_context_digest",
        "verification_process_digest",
    )
    if (
        assignment_id != canonical_assignment_id
        or result.get("schema_version") != "1.0"
        or result.get("assignment_id") != assignment_id
        or verification.get("schema_version") != "1.1"
        or verification.get("claim_id") != claim_id
        or verdict != "independently_verified"
        or claim_status != "independently_verified"
        or not isinstance(differences, list)
        or differences
        or verification.get("validation_mode") != "real_host"
        or any(
            not isinstance(verification.get(field), str)
            or DAEMON_FINGERPRINT_PATTERN.fullmatch(str(verification[field])) is None
            for field in (*digest_fields, "oracle_digest")
        )
    ):
        raise EnterpriseExperimentError(
            "platform independent verification is not a successful schema-1.1 real-host result"
        )
    stable_progress = result.get("stable_progress")
    if not isinstance(stable_progress, dict):
        raise EnterpriseExperimentError(
            "platform independent verification omitted stable-seed progress"
        )
    if (
        isinstance(stable_progress.get("stable_hidden_runs"), bool)
        or not isinstance(stable_progress.get("stable_hidden_runs"), int)
        or int(stable_progress["stable_hidden_runs"]) < 1
        or isinstance(stable_progress.get("consecutive_passes"), bool)
        or not isinstance(stable_progress.get("consecutive_passes"), int)
        or int(stable_progress["consecutive_passes"]) < 0
        or not isinstance(stable_progress.get("progress_digest"), str)
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(str(stable_progress["progress_digest"])) is None
    ):
        raise EnterpriseExperimentError("platform stable-seed qualification is invalid")
    stable_verdict = stable_progress.get("stable_verdict")
    if stable_verdict is not None and not isinstance(stable_verdict, dict):
        raise EnterpriseExperimentError("platform stable-seed verdict has an invalid projection")
    if stable_verdict is not None and (
        stable_verdict.get("verdict") != "stably_independently_verified"
        or stable_verdict.get("task_id") != TASK_ID
        or stable_verdict.get("revision") != REVISION
        or stable_verdict.get("verification_process_digest")
        != verification["verification_process_digest"]
        or any(
            not isinstance(stable_verdict.get(field), str)
            or DAEMON_FINGERPRINT_PATTERN.fullmatch(str(stable_verdict[field])) is None
            for field in ("qualification_digest", "verdict_digest")
        )
    ):
        raise EnterpriseExperimentError(
            "platform stable-seed verdict changed its verification-process binding"
        )
    binding = {
        "assignment_id": assignment_id,
        "claim_id": claim_id,
        **{field: verification[field] for field in digest_fields},
        "validation_mode": "real_host",
    }
    unsigned = {
        "schema_version": "1.1",
        "response_schema_version": "1.0",
        "assignment_id": assignment_id,
        "claim_id": claim_id,
        "claim_status": claim_status,
        "verification_id": verification_id,
        "verdict": verdict,
        "oracle_digest": verification["oracle_digest"],
        "difference_count": 0,
        **{field: verification[field] for field in digest_fields},
        "validation_mode": "real_host",
        "frozen_verification_binding_digest": _digest(_canonical_json(binding)),
        "stable_hidden_runs": stable_progress.get("stable_hidden_runs"),
        "consecutive_passes": stable_progress.get("consecutive_passes"),
        "stable_progress_digest": stable_progress.get("progress_digest"),
        "stable_verdict": (
            None
            if stable_verdict is None
            else {
                "verdict": stable_verdict.get("verdict"),
                "verification_process_digest": stable_verdict.get("verification_process_digest"),
                "qualification_digest": stable_verdict.get("qualification_digest"),
                "verdict_digest": stable_verdict.get("verdict_digest"),
            }
        ),
    }
    return {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}


def _environment_generation_receipt(
    state_root: Path,
    *,
    seed: str,
    attempt_id: str,
    environment_instance_id: str,
    baseline_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        baseline_snapshot.get("phase") != "baseline"
        or str(baseline_snapshot.get("seed")) != seed
        or baseline_snapshot.get("attempt_id") != attempt_id
        or not isinstance(baseline_snapshot.get("digest"), str)
    ):
        raise EnterpriseExperimentError("fresh environment generation lacks its baseline snapshot")
    unsigned = {
        "schema_version": "v0.4.13-t01h-environment-generation-1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "seed": seed,
        "attempt_id": attempt_id,
        "environment_instance_id": environment_instance_id,
        "generation_id": _digest(
            _canonical_json(
                {
                    "attempt_id": attempt_id,
                    "environment_instance_id": environment_instance_id,
                    "baseline_snapshot_digest": baseline_snapshot["digest"],
                }
            )
        ),
        "baseline_snapshot_digest": baseline_snapshot["digest"],
        "status": "fresh_reset_initialized_seeded",
        "created_at": _now(),
    }
    receipt = {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}
    path = _attempt_state_root(state_root, seed, attempt_id) / "environment-generation.json"
    if path.exists():
        existing = _read_private_json(path)
        stable_fields = {key for key in unsigned if key != "created_at"}
        if any(existing.get(key) != receipt.get(key) for key in stable_fields):
            raise EnterpriseExperimentError(
                "environment generation conflicts with its durable attempt receipt"
            )
        return existing
    _atomic_private_json(path, receipt)
    return receipt


def _identity_ledger_path(state_root: Path) -> Path:
    return state_root / "historical-run-identities.jsonl"


def _identity_ledger_entries(state_root: Path) -> list[dict[str, Any]]:
    path = _identity_ledger_path(state_root)
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise EnterpriseExperimentError("historical run identity ledger is unsafe")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise EnterpriseExperimentError("historical run identity ledger exceeds its limit")
    entries: list[dict[str, Any]] = []
    for line in path.read_bytes().splitlines():
        if not line:
            continue
        if len(entries) >= 10_000:
            raise EnterpriseExperimentError("historical run identity ledger exceeds its limit")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EnterpriseExperimentError("historical run identity ledger is invalid") from error
        if not isinstance(value, dict):
            raise EnterpriseExperimentError("historical run identity ledger is invalid")
        unsigned = {key: item for key, item in value.items() if key != "receipt_digest"}
        digest = value.get("receipt_digest")
        if (
            value.get("schema_version") != "v0.4.13-t01h-historical-identity-1"
            or not isinstance(digest, str)
            or not secrets.compare_digest(digest, _digest(_canonical_json(unsigned)))
        ):
            raise EnterpriseExperimentError("historical run identity ledger is invalid")
        entries.append(value)
    return entries


def _record_historical_identity(
    state_root: Path,
    *,
    seed: str,
    attempt_id: str,
    application: Mapping[str, Any],
    assignment: Mapping[str, Any],
    environment_generation: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
) -> dict[str, Any]:
    empty_draft = application.get("runner_empty_draft_receipt")
    provider_identity = runtime_identity.get("provider_identity")
    fields = {
        "environment_instance_id": environment_generation.get("environment_instance_id"),
        "environment_generation_id": environment_generation.get("generation_id"),
        "application_id": application.get("id"),
        "assignment_id": assignment.get("assignment_id"),
        "build_id": assignment.get("build_id"),
        "session_id": assignment.get("session_id"),
    }
    if (
        not isinstance(empty_draft, Mapping)
        or not isinstance(empty_draft.get("receipt_digest"), str)
        or runtime_identity.get("builder_actor") != "lilies"
        or not isinstance(runtime_identity.get("source_tree_digest"), str)
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(str(runtime_identity.get("source_tree_digest")))
        is None
        or not isinstance(provider_identity, Mapping)
        or not isinstance(provider_identity.get("receipt_digest"), str)
        or any(not isinstance(value, str) or not value for value in fields.values())
    ):
        raise EnterpriseExperimentError("fresh run identity receipt is incomplete")
    unsigned = {
        "schema_version": "v0.4.13-t01h-historical-identity-1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "seed": seed,
        "attempt_id": attempt_id,
        **fields,
        "empty_draft_receipt_digest": empty_draft["receipt_digest"],
        "builder_actor": "lilies",
        "sibling_commit": runtime_identity.get("sibling_commit"),
        "sibling_package_digest": runtime_identity.get("package_digest"),
        "sibling_source_tree_digest": runtime_identity.get("source_tree_digest"),
        "sibling_dirty": runtime_identity.get("sibling_dirty"),
        "sibling_dirty_status_digest": runtime_identity.get("sibling_dirty_status_digest"),
        "provider_identity_digest": provider_identity.get("receipt_digest"),
        "recorded_at": _now(),
    }
    entry = {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))}
    entries = _identity_ledger_entries(state_root)
    same_attempt = [item for item in entries if item.get("attempt_id") == attempt_id]
    if same_attempt:
        existing = same_attempt[-1]
        stable = set(unsigned) - {"recorded_at"}
        if any(existing.get(key) != entry.get(key) for key in stable):
            raise EnterpriseExperimentError("resume changed the original attempt identity")
        return existing
    unique_fields = tuple(fields)
    for existing in entries:
        for field in unique_fields:
            if secrets.compare_digest(str(existing.get(field) or ""), str(fields[field])):
                raise EnterpriseExperimentError(f"fresh run reused historical identity: {field}")
    path = _identity_ledger_path(state_root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, _canonical_json(entry) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return entry


def _active_run_path(state_root: Path, seed: str) -> Path:
    return state_root / f"active-run-{seed}.json"


def _validated_active_run(state_root: Path, seed: str) -> dict[str, Any]:
    active = _read_private_json(_active_run_path(state_root, seed))
    expected_fields = {
        "schema_version",
        "task_id",
        "revision",
        "seed",
        "collaboration_policy",
        "operational_permission_policy",
        "platform_port",
        "daemon_port",
        "application_id",
        "connection_id",
        "daemon_fingerprint",
        "assignment_id",
        "build_id",
        "session_id",
        "run_attempt_id",
        "attempt_started_at",
        "environment_instance_id",
        "environment_generation",
        "empty_draft_receipt",
        "historical_identity",
        "global_usage_baseline",
        "runtime_identity",
        "updated_at",
        "receipt_digest",
    }
    if (
        set(active) != expected_fields
        or active.get("schema_version") != "1.3"
        or active.get("task_id") != TASK_ID
        or active.get("revision") != REVISION
        or active.get("seed") != seed
    ):
        raise EnterpriseExperimentError("active run does not bind the requested seed")
    receipt_digest = active.pop("receipt_digest")
    expected_digest = _digest(_canonical_json(active))
    active["receipt_digest"] = receipt_digest
    if not isinstance(receipt_digest, str) or not secrets.compare_digest(
        receipt_digest, expected_digest
    ):
        raise EnterpriseExperimentError("active run receipt digest is invalid")
    for field in ("application_id", "connection_id", "assignment_id", "build_id", "session_id"):
        value = active.get(field)
        try:
            if str(UUID(str(value))) != value:
                raise ValueError("non-canonical UUID")
        except (TypeError, ValueError) as error:
            raise EnterpriseExperimentError(f"active run {field} is invalid") from error
    if (
        not isinstance(active.get("daemon_fingerprint"), str)
        or DAEMON_FINGERPRINT_PATTERN.fullmatch(str(active["daemon_fingerprint"])) is None
        or not isinstance(active.get("run_attempt_id"), str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", str(active["run_attempt_id"])) is None
        or not isinstance(active.get("environment_instance_id"), str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,511}",
            str(active["environment_instance_id"]),
        )
        is None
        or not isinstance(active.get("attempt_started_at"), str)
        or _run_attempt_id(seed, str(active.get("attempt_started_at")))
        != active.get("run_attempt_id")
        or not isinstance(active.get("environment_generation"), Mapping)
        or active["environment_generation"].get("attempt_id") != active.get("run_attempt_id")
        or active["environment_generation"].get("environment_instance_id")
        != active.get("environment_instance_id")
        or not isinstance(active.get("empty_draft_receipt"), Mapping)
        or active["empty_draft_receipt"].get("application_id") != active.get("application_id")
        or not isinstance(active.get("historical_identity"), Mapping)
        or active["historical_identity"].get("attempt_id") != active.get("run_attempt_id")
        or not isinstance(active.get("runtime_identity"), Mapping)
        or active["runtime_identity"].get("builder_actor") != "lilies"
        or not isinstance(active.get("global_usage_baseline"), Mapping)
    ):
        raise EnterpriseExperimentError("active run persisted identity is invalid")
    _validated_receipt(
        active["environment_generation"],
        schema_version="v0.4.13-t01h-environment-generation-1",
        label="environment generation",
    )
    _validated_receipt(
        active["empty_draft_receipt"],
        schema_version="v0.4.13-t01h-empty-draft-1",
        label="empty draft",
    )
    _validated_receipt(
        active["historical_identity"],
        schema_version="v0.4.13-t01h-historical-identity-1",
        label="historical identity",
    )
    _validated_global_usage_baseline(active["global_usage_baseline"])
    ledger_entries = _identity_ledger_entries(state_root)
    matches = [
        entry for entry in ledger_entries if entry.get("attempt_id") == active.get("run_attempt_id")
    ]
    if len(matches) != 1 or matches[0] != active["historical_identity"]:
        raise EnterpriseExperimentError(
            "active run is not bound to one durable historical identity"
        )
    return active


def _write_active_run(
    state_root: Path,
    *,
    seed: str,
    collaboration_policy: str,
    operational_permission_policy: str,
    platform_port: int,
    daemon_port: int,
    application: Mapping[str, Any],
    connection: Mapping[str, Any],
    assignment: Mapping[str, Any],
    run_attempt_id: str,
    attempt_started_at: str,
    environment_instance_id: str,
    environment_generation: Mapping[str, Any],
    empty_draft_receipt: Mapping[str, Any],
    historical_identity: Mapping[str, Any],
    global_usage_baseline: Mapping[str, Any],
    runtime_identity: Mapping[str, Any],
) -> None:
    payload = {
        "schema_version": "1.3",
        "task_id": TASK_ID,
        "revision": REVISION,
        "seed": seed,
        "collaboration_policy": collaboration_policy,
        "operational_permission_policy": operational_permission_policy,
        "platform_port": platform_port,
        "daemon_port": daemon_port,
        "application_id": application.get("id"),
        "connection_id": connection.get("connection_id"),
        "daemon_fingerprint": connection.get("daemon_fingerprint"),
        "assignment_id": assignment.get("assignment_id"),
        "build_id": assignment.get("build_id"),
        "session_id": assignment.get("session_id"),
        "run_attempt_id": run_attempt_id,
        "attempt_started_at": attempt_started_at,
        "environment_instance_id": environment_instance_id,
        "environment_generation": dict(environment_generation),
        "empty_draft_receipt": dict(empty_draft_receipt),
        "historical_identity": dict(historical_identity),
        "global_usage_baseline": dict(global_usage_baseline),
        "runtime_identity": dict(runtime_identity),
        "updated_at": _now(),
    }
    _atomic_private_json(
        _active_run_path(state_root, seed),
        {**payload, "receipt_digest": _digest(_canonical_json(payload))},
    )


def _run_attempt_id(
    seed: str,
    started_at: str,
    *,
    revision: int | None = None,
) -> str:
    effective_revision = REVISION if revision is None else revision
    if (
        isinstance(effective_revision, bool)
        or not isinstance(effective_revision, int)
        or effective_revision < 1
    ):
        raise EnterpriseExperimentError("run attempt revision is invalid")
    return _digest(
        _canonical_json(
            {
                "task_id": TASK_ID,
                "revision": effective_revision,
                "seed": seed,
                "started_at": started_at,
            }
        )
    )


def _run_attempt_path(evidence_root: Path, seed: str, attempt_id: str) -> Path:
    digest = _attempt_storage_key(attempt_id)
    return evidence_root / "attempts" / f"seed-{seed}" / f"{digest}.json"


def _attempt_state_root(state_root: Path, seed: str, attempt_id: str) -> Path:
    return state_root / "attempts" / f"seed-{seed}" / _attempt_storage_key(attempt_id)


def _attempt_log_path(
    state_root: Path,
    *,
    seed: str,
    attempt_id: str,
    service: str,
) -> Path:
    if re.fullmatch(r"[a-z][a-z0-9-]{0,39}", service) is None:
        raise EnterpriseExperimentError("attempt log service label is invalid")
    return _attempt_state_root(state_root, seed, attempt_id) / "logs" / f"{service}.log"


def _archive_host_snapshot(
    state_root: Path,
    *,
    seed: str,
    phase: str,
    attempt_id: str,
) -> dict[str, Any]:
    if phase not in {"baseline", "final"}:
        raise EnterpriseExperimentError("host snapshot phase is invalid")
    source = state_root / "environment" / f"host-snapshot-{seed}-{phase}.json"
    payload = source.read_bytes()
    target = _attempt_state_root(state_root, seed, attempt_id) / "host-snapshots" / f"{phase}.json"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        if target.read_bytes() != payload:
            raise EnterpriseExperimentError(
                "host snapshot conflicts with its immutable attempt archive"
            )
    else:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass
    summary = _snapshot_summary(target)
    summary["attempt_id"] = attempt_id
    summary["archive_path"] = str(target)
    return summary


def _archive_latest_run_evidence(
    evidence_root: Path,
    *,
    seed: str,
    latest_path: Path,
    incoming_attempt_id: str,
) -> tuple[str | None, int]:
    if not latest_path.exists():
        return None, 0
    raw = latest_path.read_bytes()
    try:
        existing = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnterpriseExperimentError(
            f"existing seed evidence is invalid: {latest_path}"
        ) from error
    existing_revision = existing.get("revision") if isinstance(existing, dict) else None
    if (
        not isinstance(existing, dict)
        or existing.get("experiment_task_id") != TASK_ID
        or isinstance(existing_revision, bool)
        or not isinstance(existing_revision, int)
        or existing_revision < 1
        or existing_revision > REVISION
        or existing.get("seed") != seed
        or not isinstance(existing.get("started_at"), str)
    ):
        raise EnterpriseExperimentError(
            f"existing seed evidence identity is invalid: {latest_path}"
        )
    attempt_id = existing.get("attempt_id")
    if not isinstance(attempt_id, str):
        attempt_id = _run_attempt_id(
            seed,
            existing["started_at"],
            revision=existing_revision,
        )
    expected_attempt_id = _run_attempt_id(
        seed,
        existing["started_at"],
        revision=existing_revision,
    )
    if not secrets.compare_digest(attempt_id, expected_attempt_id):
        raise EnterpriseExperimentError(
            f"existing seed evidence attempt identity is invalid: {latest_path}"
        )
    archive_path = _run_attempt_path(evidence_root, seed, attempt_id)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        if archive_path.read_bytes() != raw:
            raise EnterpriseExperimentError(
                f"run attempt evidence conflicts with its immutable archive: {archive_path}"
            )
    else:
        _atomic_evidence_bytes(archive_path, raw)
    if secrets.compare_digest(attempt_id, incoming_attempt_id):
        continuation_root = (
            evidence_root
            / "attempt-continuations"
            / f"seed-{seed}"
            / _attempt_storage_key(attempt_id)
        )
        continuation_root.mkdir(parents=True, exist_ok=True)
        continuation_path = continuation_root / f"{_digest(raw).removeprefix('sha256:')}.json"
        if continuation_path.exists():
            if continuation_path.read_bytes() != raw:
                raise EnterpriseExperimentError(
                    "attempt continuation archive changed its immutable payload"
                )
        else:
            _atomic_evidence_bytes(continuation_path, raw)
        previous = existing.get("previous_attempt_id")
        if previous is not None and not isinstance(previous, str):
            raise EnterpriseExperimentError("existing attempt continuation linkage is invalid")
        continuation_index = int(existing.get("continuation_index") or 0) + 1
        return previous, continuation_index
    return attempt_id, 0


def _write_run_evidence(
    evidence_root: Path,
    *,
    seed: str,
    started_at: str,
    status: str,
    package: Mapping[str, Any] | None,
    application: Mapping[str, Any] | None,
    connection: Mapping[str, Any] | None,
    assignment: Mapping[str, Any] | None,
    secret_receipts: Sequence[Mapping[str, Any]],
    host_snapshots: Sequence[Mapping[str, Any]],
    platform_verification: Mapping[str, Any] | None,
    host_verification: Mapping[str, Any] | None,
    error: object | None,
    finished_at: str | None = None,
    model_egress_authorized: bool = False,
    token_monitor: Mapping[str, Any] | None = None,
    lifecycle: Mapping[str, Any] | None = None,
    discovery: Mapping[str, Any] | None = None,
    session_budget: Mapping[str, Any] | None = None,
    freshness: Mapping[str, Any] | None = None,
    builder_provenance: Mapping[str, Any] | None = None,
    report_transaction: Mapping[str, Any] | None = None,
    attempt_id: str | None = None,
    continuation_started_at: str | None = None,
) -> Path:
    evidence_root.mkdir(parents=True, exist_ok=True)
    path = evidence_root / f"seed-{seed}.json"
    expected_attempt_id = _run_attempt_id(seed, started_at)
    if attempt_id is None:
        attempt_id = expected_attempt_id
    elif not secrets.compare_digest(attempt_id, expected_attempt_id):
        raise EnterpriseExperimentError(
            "run evidence attempt identity changed its original start binding"
        )
    previous_attempt_id, continuation_index = _archive_latest_run_evidence(
        evidence_root,
        seed=seed,
        latest_path=path,
        incoming_attempt_id=attempt_id,
    )
    value = {
        "schema_version": "v0.4.13-t01h-run-2",
        "stage_task_id": "V04-13-T01H",
        "experiment_task_id": TASK_ID,
        "revision": REVISION,
        "seed": seed,
        "attempt_id": attempt_id,
        "previous_attempt_id": previous_attempt_id,
        "continuation_index": continuation_index,
        "continuation_started_at": continuation_started_at,
        "status": status,
        "started_at": started_at,
        "finished_at": (
            finished_at
            or (
                str(lifecycle["finished_at"])
                if isinstance(lifecycle, Mapping) and isinstance(lifecycle.get("finished_at"), str)
                else _now()
            )
        ),
        "package": package,
        "application_id": None if application is None else application.get("id"),
        "connection": (
            None
            if connection is None
            else {
                "connection_id": connection.get("connection_id"),
                "base_url": connection.get("base_url"),
                "daemon_fingerprint": connection.get("daemon_fingerprint"),
                "status": connection.get("status"),
            }
        ),
        "assignment": (None if assignment is None else _safe_assignment_projection(assignment)),
        "secret_receipts": list(secret_receipts),
        "host_snapshots": list(host_snapshots),
        "platform_verification": platform_verification,
        "host_verification": host_verification,
        "model_egress_authorized": model_egress_authorized,
        "token_monitor": token_monitor,
        "session_budget": session_budget,
        "freshness": freshness,
        "builder_provenance": builder_provenance,
        "report_transaction": report_transaction,
        "lifecycle": lifecycle,
        "discovery": _safe_discovery_projection(discovery),
        "error": _safe_error_projection(error),
        "claim_ceiling": (
            "Real-host run metadata only. A completed assignment is not an "
            "enterprise pass until its frozen archive and independent oracle "
            "verdict are present and pass."
        ),
    }
    encoded = _canonical_json(value) + b"\n"
    attempt_path = _run_attempt_path(evidence_root, seed, attempt_id)
    attempt_path.parent.mkdir(parents=True, exist_ok=True)
    if attempt_path.exists():
        if continuation_index == 0 and attempt_path.read_bytes() != encoded:
            raise EnterpriseExperimentError(
                f"run attempt evidence conflicts with its immutable archive: {attempt_path}"
            )
        if continuation_index > 0:
            _atomic_evidence_bytes(attempt_path, encoded)
    else:
        _atomic_evidence_bytes(attempt_path, encoded)
    _atomic_evidence_bytes(path, encoded)
    return path


def _write_report_failure_evidence(
    evidence_root: Path,
    *,
    seed: str,
    attempt_id: str,
    lifecycle: Mapping[str, Any] | None,
    error: BaseException,
    prepared: Mapping[str, Any] | None,
) -> Path:
    path = (
        evidence_root
        / "report-failures"
        / f"seed-{seed}"
        / f"{_attempt_storage_key(attempt_id)}.json"
    )
    unsigned = {
        "schema_version": "v0.4.13-t01h-report-failure-1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "seed": seed,
        "attempt_id": attempt_id,
        "status": "report_commit_failed",
        "error": _safe_error_projection(error),
        "prepared": prepared,
        "lifecycle": lifecycle,
        "recorded_at": _now(),
    }
    _atomic_private_json(
        path,
        {**unsigned, "receipt_digest": _digest(_canonical_json(unsigned))},
    )
    return path


def _finalize_run_evidence(
    lifecycle: _LifecycleRecorder,
    resources: _RunResourceScope,
    evidence_root: Path,
    **kwargs: Any,
) -> tuple[Path, bool, BaseException | None]:
    seed = str(kwargs["seed"])
    started_at = str(kwargs["started_at"])
    attempt_id = str(kwargs.get("attempt_id") or _run_attempt_id(seed, started_at))
    try:
        resources.finish_reporting()
        lifecycle_evidence = lifecycle.snapshot()
    except BaseException as lifecycle_error:
        path = _write_report_failure_evidence(
            evidence_root,
            seed=seed,
            attempt_id=attempt_id,
            lifecycle=None,
            error=lifecycle_error,
            prepared=None,
        )
        return path, False, lifecycle_error
    report_transaction_unsigned = {
        "schema_version": "v0.4.13-t01h-report-transaction-2",
        "attempt_id": attempt_id,
        "status": "derived_after_lifecycle_seal",
        "lifecycle_journal_tail_digest": lifecycle_evidence["execution_journal"]["tail_digest"],
        "phase_denominator_excludes_report_derivation": True,
        "derived_at": _now(),
    }
    report_transaction = {
        **report_transaction_unsigned,
        "receipt_digest": _digest(_canonical_json(report_transaction_unsigned)),
    }
    committed = dict(kwargs)
    committed.update(
        lifecycle=lifecycle_evidence,
        report_transaction=report_transaction,
    )
    try:
        path = _write_run_evidence(evidence_root, **committed)
    except BaseException as commit_error:
        path = _write_report_failure_evidence(
            evidence_root,
            seed=seed,
            attempt_id=attempt_id,
            lifecycle=lifecycle_evidence,
            error=commit_error,
            prepared=report_transaction,
        )
        return path, False, commit_error
    return path, True, None


def _token_monitor_evidence(
    state_root: Path,
    *,
    interval_seconds: float,
    attempt_id: str,
) -> dict[str, Any]:
    latest = _token_monitor_root(state_root, attempt_id) / "token-monitor.latest.json"
    if not latest.is_file():
        return {
            "status": "missing",
            "interval_seconds": interval_seconds,
            "latest_digest": None,
        }
    payload = latest.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "invalid",
            "interval_seconds": interval_seconds,
            "latest_digest": _digest(payload),
        }
    if not isinstance(value, dict):
        return {
            "status": "invalid",
            "interval_seconds": interval_seconds,
            "latest_digest": _digest(payload),
        }
    if value.get("attempt_id") != attempt_id:
        return {
            "status": "identity_mismatch",
            "interval_seconds": interval_seconds,
            "latest_digest": _digest(payload),
        }
    return {
        "status": "recorded",
        "attempt_id": attempt_id,
        "interval_seconds": interval_seconds,
        "latest_digest": _digest(payload),
        "observed_at": value.get("observed_at"),
        "totals": value.get("totals"),
        "safety": value.get("safety"),
    }


def run_seed(args: argparse.Namespace) -> int:
    state_root = args.state_root.resolve()
    evidence_root = args.evidence_root.resolve()
    started_at = _now()
    attempt_id = _run_attempt_id(args.seed, started_at)
    lifecycle = _LifecycleRecorder(
        mode="run",
        started_at=started_at,
        journal_path=(
            _attempt_state_root(state_root, args.seed, attempt_id) / "execution-timing.jsonl"
        ),
    )
    environment_instance_id = (
        f"{TASK_ID.lower()}:r{REVISION}:seed-{args.seed}:attempt-{_attempt_storage_key(attempt_id)}"
    )
    operational_permission_policy = str(
        getattr(args, "operational_permission_policy", "task_local_workspace")
    )
    if operational_permission_policy not in OPERATIONAL_PERMISSION_POLICIES:
        raise EnterpriseExperimentError("operational permission policy is invalid")
    host_environment = _scrub_provider_environment(os.environ)
    resources = _RunResourceScope(
        lifecycle,
        state_root=state_root,
        environment=host_environment,
    )
    package: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    assignment: dict[str, Any] | None = None
    secret_receipts: list[dict[str, Any]] = []
    host_snapshots: list[dict[str, Any]] = []
    platform_verification: dict[str, Any] | None = None
    host_verification: dict[str, Any] | None = None
    discovery: dict[str, Any] | None = None
    session_budget: dict[str, Any] | None = None
    runtime_identity: dict[str, Any] | None = None
    environment_generation: dict[str, Any] | None = None
    historical_identity: dict[str, Any] | None = None
    global_usage_baseline: dict[str, Any] | None = None
    builder_provenance: dict[str, Any] | None = None
    try:
        lifecycle.start("environment")
        with resources as stack:
            state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            runner_secrets = _runner_secrets(state_root, create=True)
            provider_configuration = _provider_launch_configuration(args)
            provider_identity = dict(provider_configuration["identity"])
            if args.token_monitor_interval <= 0:
                raise EnterpriseExperimentError(
                    "a formal model run requires --token-monitor-interval greater than zero"
                )
            runtime_identity = _verify_standalone_lilies_runtime()
            runtime_identity["provider_identity"] = provider_identity
            standalone_python = Path(str(runtime_identity["python"]))
            _environment_command(state_root, "config", environment=host_environment)
            _environment_command(
                state_root,
                "reset",
                "--confirm-task-id",
                TASK_ID,
                environment=host_environment,
            )
            resources.mark_environment_up_attempted()
            _environment_command(state_root, "up", environment=host_environment)
            _environment_command(state_root, "initialize", environment=host_environment)
            _environment_command(
                state_root,
                "seed",
                "--seed",
                args.seed,
                environment=host_environment,
            )
            _environment_command(
                state_root,
                "snapshot",
                "--seed",
                args.seed,
                "--phase",
                "baseline",
                environment=host_environment,
            )
            baseline_snapshot = _archive_host_snapshot(
                state_root,
                seed=args.seed,
                phase="baseline",
                attempt_id=attempt_id,
            )
            host_snapshots.append(baseline_snapshot)
            environment_generation = _environment_generation_receipt(
                state_root,
                seed=args.seed,
                attempt_id=attempt_id,
                environment_instance_id=environment_instance_id,
                baseline_snapshot=baseline_snapshot,
            )
            platform_environment = _platform_environment(
                state_root,
                runner_secrets,
                port=args.platform_port,
                collaboration_policy=args.collaboration_policy,
            )
            daemon_environment = _daemon_environment(
                state_root,
                port=args.daemon_port,
                provider_configuration=provider_configuration,
            )
            platform_url = f"http://127.0.0.1:{args.platform_port}"
            daemon_url = f"http://127.0.0.1:{args.daemon_port}"
            package = _freeze_package(Path(platform_environment["DATA_DIR"]))
            lifecycle.finish(outcome="completed")
            lifecycle.start("discovery")
            _managed_process(
                stack,
                (
                    sys.executable,
                    str(ENVIRONMENT_CONTROL),
                    "--state-root",
                    str(state_root / "environment"),
                    "--package-root",
                    str(TASK_ROOT),
                    "serve",
                ),
                environment=host_environment,
                log_path=_attempt_log_path(
                    state_root,
                    seed=args.seed,
                    attempt_id=attempt_id,
                    service="boundary",
                ),
            )
            _managed_process(
                stack,
                (sys.executable, "-m", "agent_platform.cli"),
                environment=platform_environment,
                log_path=_attempt_log_path(
                    state_root,
                    seed=args.seed,
                    attempt_id=attempt_id,
                    service="platform",
                ),
            )
            managed_ollama = _start_managed_ollama(
                stack,
                state_root=state_root,
                attempt_id=attempt_id,
                provider_configuration=provider_configuration,
                log_path=_attempt_log_path(
                    state_root,
                    seed=args.seed,
                    attempt_id=attempt_id,
                    service="ollama",
                ),
            )
            daemon_process = _managed_process(
                stack,
                _standalone_daemon_command(
                    standalone_python,
                    state_root=state_root,
                    port=args.daemon_port,
                ),
                environment=daemon_environment,
                log_path=_attempt_log_path(
                    state_root,
                    seed=args.seed,
                    attempt_id=attempt_id,
                    service="lilies",
                ),
            )
            _wait_json(platform_url, "/health", timeout_seconds=60)
            daemon_health = _wait_json(
                daemon_url,
                "/local/v1/health",
                timeout_seconds=60,
            )
            if not isinstance(daemon_health, dict):
                raise EnterpriseExperimentError(
                    "standalone Lilies health returned an invalid response"
                )
            discovery = _assert_platform_discovered_daemon(
                platform_url,
                runner_secrets["platform_api_token"],
                daemon_url=daemon_url,
                daemon_pid=daemon_process.pid,
                daemon_health=daemon_health,
                expected_model_egress_enabled=(provider_identity["provider"] == "deepseek"),
            )
            discovery["provider_identity"] = provider_identity
            discovery["managed_ollama"] = managed_ollama
            _wait_tcp("127.0.0.1", 18002, timeout_seconds=120)
            lifecycle.finish(outcome="completed")
            lifecycle.start("pairing")
            secret_receipts = _install_environment_secrets(
                platform_url,
                runner_secrets["platform_api_token"],
                _host_secrets(state_root),
            )
            if provider_identity["provider"] == "ollama-local":
                discovery["local_model_authorization"] = _authorize_local_model_access(
                    stack,
                    state_root=state_root,
                    standalone_python=standalone_python,
                    daemon_environment=daemon_environment,
                    daemon_url=daemon_url,
                    expected_daemon_fingerprint=str(discovery["daemon_fingerprint"]),
                    provider_identity=provider_identity,
                )
                authorized_health = _request_json(daemon_url, "/local/v1/health")
                if (
                    not isinstance(authorized_health, Mapping)
                    or authorized_health.get("model_egress_enabled") is not True
                ):
                    raise EnterpriseExperimentError(
                        "local model authorization did not reach public health"
                    )
            connection = _pair_daemon(
                state_root=state_root,
                daemon_port=args.daemon_port,
                platform_url=platform_url,
                platform_token=runner_secrets["platform_api_token"],
                standalone_python=standalone_python,
                daemon_environment=daemon_environment,
                expected_daemon_fingerprint=str(discovery["daemon_fingerprint"]),
            )
            if connection.get("daemon_fingerprint") != discovery["daemon_fingerprint"]:
                raise EnterpriseExperimentError(
                    "paired connection changed the discovered daemon fingerprint"
                )
            lifecycle.finish(outcome="completed")
            lifecycle.start("assignment")
            global_usage_baseline = _global_usage_baseline(
                _standalone_observability_snapshot(
                    platform_url=platform_url,
                    platform_token=runner_secrets["platform_api_token"],
                    connection_id=str(connection["connection_id"]),
                ),
                require_fresh=True,
            )
            application = _create_application(
                platform_url,
                runner_secrets["platform_api_token"],
                seed=args.seed,
            )
            assignment = _start_formal_build(
                platform_url,
                runner_secrets["platform_api_token"],
                application_id=str(application["id"]),
                connection_id=str(connection["connection_id"]),
                seed=args.seed,
                environment_instance_id=environment_instance_id,
            )
            _assert_assignment_identity(
                assignment,
                application_id=str(application["id"]),
                connection_id=str(connection["connection_id"]),
            )
            expected_assignment_id = str(assignment["assignment_id"])
            expected_session_id = str(assignment["session_id"])
            expected_build_id = str(assignment["build_id"])
            historical_identity = _record_historical_identity(
                state_root,
                seed=args.seed,
                attempt_id=attempt_id,
                application=application,
                assignment=assignment,
                environment_generation=environment_generation,
                runtime_identity=runtime_identity,
            )
            _write_active_run(
                state_root,
                seed=args.seed,
                collaboration_policy=args.collaboration_policy,
                operational_permission_policy=operational_permission_policy,
                platform_port=args.platform_port,
                daemon_port=args.daemon_port,
                application=application,
                connection=connection,
                assignment=assignment,
                run_attempt_id=attempt_id,
                attempt_started_at=started_at,
                environment_instance_id=environment_instance_id,
                environment_generation=environment_generation,
                empty_draft_receipt=application["runner_empty_draft_receipt"],
                historical_identity=historical_identity,
                global_usage_baseline=global_usage_baseline,
                runtime_identity=runtime_identity,
            )
            if args.collaboration_policy == "auto_forward":
                _set_auto_forward(
                    platform_url,
                    runner_secrets["platform_api_token"],
                    assignment_id=str(assignment["assignment_id"]),
                )
            lifecycle.finish(outcome="completed")
            lifecycle.start("builder")
            assignment = _poll_assignment(
                platform_url,
                runner_secrets["platform_api_token"],
                assignment_id=str(assignment["assignment_id"]),
                deadline_seconds=args.deadline_seconds,
                operational_permission_policy=operational_permission_policy,
                token_state_root=state_root,
                token_monitor_interval=args.token_monitor_interval,
                connection_id=str(connection["connection_id"]),
                token_attempt_id=attempt_id,
                expected_session_id=expected_session_id,
                global_usage_baseline=global_usage_baseline,
            )
            _assert_assignment_identity(
                assignment,
                assignment_id=expected_assignment_id,
                application_id=str(application["id"]),
                connection_id=str(connection["connection_id"]),
                session_id=expected_session_id,
                build_id=expected_build_id,
            )
            session_budget = _session_budget_receipt(
                _standalone_observability_snapshot(
                    platform_url=platform_url,
                    platform_token=runner_secrets["platform_api_token"],
                    connection_id=str(connection["connection_id"]),
                ),
                assignment=assignment,
                discovery=discovery,
                sequence=assignment.get("runner_session_budget_sequence"),
                global_baseline=global_usage_baseline,
            )
            _write_active_run(
                state_root,
                seed=args.seed,
                collaboration_policy=args.collaboration_policy,
                operational_permission_policy=operational_permission_policy,
                platform_port=args.platform_port,
                daemon_port=args.daemon_port,
                application=application,
                connection=connection,
                assignment=assignment,
                run_attempt_id=attempt_id,
                attempt_started_at=started_at,
                environment_instance_id=environment_instance_id,
                environment_generation=environment_generation,
                empty_draft_receipt=application["runner_empty_draft_receipt"],
                historical_identity=historical_identity,
                global_usage_baseline=global_usage_baseline,
                runtime_identity=runtime_identity,
            )
            lifecycle.finish(outcome=_builder_lifecycle_outcome(assignment))
            lifecycle.start("host_verification")
            _environment_command(
                state_root,
                "snapshot",
                "--seed",
                args.seed,
                "--phase",
                "final",
                environment=host_environment,
            )
            host_snapshots.append(
                _archive_host_snapshot(
                    state_root,
                    seed=args.seed,
                    phase="final",
                    attempt_id=attempt_id,
                )
            )
            if str(assignment.get("phase")) == "completed":
                host_verification = _run_host_snapshot_verification(
                    state_root,
                    seed=args.seed,
                )
                if host_verification.get("verdict") == "independently_verified":
                    lifecycle.finish(outcome="independently_verified")
                else:
                    lifecycle.finish(outcome="verification_failed")
            else:
                lifecycle.finish(outcome="skipped")
            lifecycle.start("platform_verification")
            if (
                host_verification is not None
                and host_verification.get("verdict") == "independently_verified"
            ):
                platform_verification = _run_platform_independent_verification(
                    platform_url,
                    runner_secrets["platform_api_token"],
                    assignment_id=str(assignment["assignment_id"]),
                )
                builder_provenance = _builder_provenance_receipt(
                    platform_url,
                    runner_secrets["platform_api_token"],
                    assignment=assignment,
                    discovery=discovery,
                    runtime_identity=runtime_identity,
                    qualified_verification=platform_verification,
                )
                lifecycle.finish(
                    outcome=str(platform_verification.get("claim_status") or "completed")
                )
            else:
                lifecycle.finish(outcome="skipped")
        status = _enterprise_run_status(
            assignment,
            host_verification=host_verification,
            platform_verification=platform_verification,
            session_budget=session_budget,
        )
        token_monitor = _token_monitor_evidence(
            state_root,
            interval_seconds=float(getattr(args, "token_monitor_interval", 5.0)),
            attempt_id=attempt_id,
        )
        path, report_committed, report_error = _finalize_run_evidence(
            lifecycle,
            resources,
            evidence_root,
            seed=args.seed,
            started_at=started_at,
            status=status,
            package=package,
            application=application,
            connection=connection,
            assignment=assignment,
            secret_receipts=secret_receipts,
            host_snapshots=host_snapshots,
            platform_verification=platform_verification,
            host_verification=host_verification,
            error=None,
            model_egress_authorized=bool(getattr(args, "enable_model_egress", False)),
            token_monitor=token_monitor,
            discovery=discovery,
            session_budget=session_budget,
            freshness={
                "environment_generation": environment_generation,
                "empty_draft": (
                    None if application is None else application.get("runner_empty_draft_receipt")
                ),
                "historical_identity": historical_identity,
            },
            builder_provenance=builder_provenance,
        )
        print(path)
        if not report_committed:
            _print_safe_error(report_error or EnterpriseExperimentError("report failed"))
            return 2
        return 0 if status == "enterprise_run_passed" else 3
    except BaseException as error:
        was_interruption = not isinstance(error, Exception)
        try:
            resources.ensure_closed(error)
        except BaseException as cleanup_error:
            error = cleanup_error
        token_monitor = _token_monitor_evidence(
            state_root,
            interval_seconds=float(getattr(args, "token_monitor_interval", 5.0)),
            attempt_id=attempt_id,
        )
        path, _, report_error = _finalize_run_evidence(
            lifecycle,
            resources,
            evidence_root,
            seed=args.seed,
            started_at=started_at,
            status="run_failed",
            package=package,
            application=application,
            connection=connection,
            assignment=assignment,
            secret_receipts=secret_receipts,
            host_snapshots=host_snapshots,
            platform_verification=platform_verification,
            host_verification=host_verification,
            error=error,
            model_egress_authorized=bool(getattr(args, "enable_model_egress", False)),
            token_monitor=token_monitor,
            discovery=discovery,
            session_budget=session_budget,
            freshness={
                "environment_generation": environment_generation,
                "empty_draft": (
                    None if application is None else application.get("runner_empty_draft_receipt")
                ),
                "historical_identity": historical_identity,
            },
            builder_provenance=builder_provenance,
        )
        print(path, file=sys.stderr)
        _print_safe_error(report_error or error)
        if was_interruption:
            raise
        return 2


def resume_seed(args: argparse.Namespace) -> int:
    state_root = args.state_root.resolve()
    evidence_root = args.evidence_root.resolve()
    resume_started_at = _now()
    attempt_id = _run_attempt_id(args.seed, resume_started_at)
    lifecycle = _LifecycleRecorder(
        mode="resume",
        started_at=resume_started_at,
        journal_path=(
            state_root
            / "resume-attempts"
            / f"seed-{args.seed}"
            / _attempt_storage_key(attempt_id)
            / "execution-timing.jsonl"
        ),
    )
    attempt_started_at = resume_started_at
    host_environment = _scrub_provider_environment(os.environ)
    resources = _RunResourceScope(
        lifecycle,
        state_root=state_root,
        environment=host_environment,
    )
    package: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    assignment: dict[str, Any] | None = None
    host_snapshots: list[dict[str, Any]] = []
    platform_verification: dict[str, Any] | None = None
    host_verification: dict[str, Any] | None = None
    discovery: dict[str, Any] | None = None
    session_budget: dict[str, Any] | None = None
    platform_port: int | None = None
    daemon_port: int | None = None
    collaboration_policy: str | None = None
    operational_permission_policy: str | None = None
    assignment_id: str | None = None
    application_id: str | None = None
    connection_id: str | None = None
    platform_url: str | None = None
    daemon_url: str | None = None
    build_id: str | None = None
    session_id: str | None = None
    daemon_fingerprint: str | None = None
    run_attempt_id: str | None = None
    environment_instance_id: str | None = None
    runtime_identity: dict[str, Any] | None = None
    environment_generation: dict[str, Any] | None = None
    empty_draft_receipt: dict[str, Any] | None = None
    historical_identity: dict[str, Any] | None = None
    global_usage_baseline: dict[str, Any] | None = None
    builder_provenance: dict[str, Any] | None = None
    try:
        lifecycle.start("environment")
        with resources as stack:
            active = _validated_active_run(state_root, args.seed)
            try:
                platform_port = int(active["platform_port"])
                daemon_port = int(active["daemon_port"])
            except (KeyError, TypeError, ValueError) as error:
                raise EnterpriseExperimentError("active run ports are invalid") from error
            collaboration_policy = str(active.get("collaboration_policy") or "")
            operational_permission_policy = str(active.get("operational_permission_policy") or "")
            assignment_id = active.get("assignment_id")
            application_id = active.get("application_id")
            connection_id = active.get("connection_id")
            build_id = active.get("build_id")
            session_id = active.get("session_id")
            daemon_fingerprint = active.get("daemon_fingerprint")
            run_attempt_id = active.get("run_attempt_id")
            attempt_started_at = str(active.get("attempt_started_at") or "")
            environment_instance_id = active.get("environment_instance_id")
            environment_generation = dict(active["environment_generation"])
            empty_draft_receipt = dict(active["empty_draft_receipt"])
            historical_identity = dict(active["historical_identity"])
            global_usage_baseline = dict(active["global_usage_baseline"])
            persisted_runtime_identity = dict(active["runtime_identity"])
            if (
                not 1 <= platform_port <= 65_535
                or not 1 <= daemon_port <= 65_535
                or not isinstance(assignment_id, str)
                or not assignment_id
                or not isinstance(application_id, str)
                or not application_id
                or not isinstance(connection_id, str)
                or not connection_id
                or not isinstance(build_id, str)
                or not isinstance(session_id, str)
                or not isinstance(daemon_fingerprint, str)
                or not isinstance(run_attempt_id, str)
                or not isinstance(environment_instance_id, str)
                or not attempt_started_at
            ):
                raise EnterpriseExperimentError("active run identity is invalid")
            if collaboration_policy not in {"manual", "auto_forward"}:
                raise EnterpriseExperimentError("active run has an invalid collaboration policy")
            if operational_permission_policy not in OPERATIONAL_PERMISSION_POLICIES:
                raise EnterpriseExperimentError(
                    "active run has an invalid operational permission policy"
                )
            attempt_id = run_attempt_id
            application = {
                "id": application_id,
                "runner_empty_draft_receipt": empty_draft_receipt,
            }
            provider_configuration = _provider_launch_configuration(args)
            provider_identity = dict(provider_configuration["identity"])
            if args.token_monitor_interval <= 0:
                raise EnterpriseExperimentError(
                    "a formal model resume requires --token-monitor-interval greater than zero"
                )
            runtime_identity = _verify_standalone_lilies_runtime()
            runtime_identity["provider_identity"] = provider_identity
            if runtime_identity != persisted_runtime_identity:
                raise EnterpriseExperimentError(
                    "resume changed the original sibling package provenance"
                )
            standalone_python = Path(str(runtime_identity["python"]))
            runner_secrets = _runner_secrets(state_root, create=False)
            resources.mark_environment_up_attempted()
            _environment_command(
                state_root,
                "up",
                environment=host_environment,
            )
            platform_environment = _platform_environment(
                state_root,
                runner_secrets,
                port=platform_port,
                collaboration_policy=collaboration_policy,
            )
            daemon_environment = _daemon_environment(
                state_root,
                port=daemon_port,
                provider_configuration=provider_configuration,
            )
            package = _freeze_package(Path(platform_environment["DATA_DIR"]))
            platform_url = f"http://127.0.0.1:{platform_port}"
            daemon_url = f"http://127.0.0.1:{daemon_port}"
            connection = {
                "connection_id": connection_id,
                "base_url": daemon_url,
                "status": "existing",
            }
            baseline_path = state_root / "environment" / f"host-snapshot-{args.seed}-baseline.json"
            if baseline_path.exists():
                host_snapshots.append(
                    _archive_host_snapshot(
                        state_root,
                        seed=args.seed,
                        phase="baseline",
                        attempt_id=attempt_id,
                    )
                )
            lifecycle.finish(outcome="completed")
            lifecycle.start("discovery")
            _managed_process(
                stack,
                (
                    sys.executable,
                    str(ENVIRONMENT_CONTROL),
                    "--state-root",
                    str(state_root / "environment"),
                    "--package-root",
                    str(TASK_ROOT),
                    "serve",
                ),
                environment=host_environment,
                log_path=_attempt_log_path(
                    state_root,
                    seed=args.seed,
                    attempt_id=attempt_id,
                    service="boundary",
                ),
            )
            _managed_process(
                stack,
                (sys.executable, "-m", "agent_platform.cli"),
                environment=platform_environment,
                log_path=_attempt_log_path(
                    state_root,
                    seed=args.seed,
                    attempt_id=attempt_id,
                    service="platform",
                ),
            )
            managed_ollama = _start_managed_ollama(
                stack,
                state_root=state_root,
                attempt_id=attempt_id,
                provider_configuration=provider_configuration,
                log_path=_attempt_log_path(
                    state_root,
                    seed=args.seed,
                    attempt_id=attempt_id,
                    service="ollama",
                ),
            )
            daemon_process = _managed_process(
                stack,
                _standalone_daemon_command(
                    standalone_python,
                    state_root=state_root,
                    port=daemon_port,
                ),
                environment=daemon_environment,
                log_path=_attempt_log_path(
                    state_root,
                    seed=args.seed,
                    attempt_id=attempt_id,
                    service="lilies",
                ),
            )
            _wait_json(platform_url, "/health", timeout_seconds=60)
            daemon_health = _wait_json(
                daemon_url,
                "/local/v1/health",
                timeout_seconds=60,
            )
            if not isinstance(daemon_health, dict):
                raise EnterpriseExperimentError(
                    "standalone Lilies health returned an invalid response"
                )
            discovery = _assert_platform_discovered_daemon(
                platform_url,
                runner_secrets["platform_api_token"],
                daemon_url=daemon_url,
                daemon_pid=daemon_process.pid,
                daemon_health=daemon_health,
                expected_model_egress_enabled=(provider_identity["provider"] == "deepseek"),
            )
            discovery["provider_identity"] = provider_identity
            discovery["managed_ollama"] = managed_ollama
            if not secrets.compare_digest(
                str(discovery["daemon_fingerprint"]),
                daemon_fingerprint,
            ):
                raise EnterpriseExperimentError("resume discovered another daemon identity")
            connection["daemon_fingerprint"] = discovery["daemon_fingerprint"]
            _wait_tcp("127.0.0.1", 18002, timeout_seconds=120)
            lifecycle.finish(outcome="completed")
            lifecycle.start("pairing")
            if provider_identity["provider"] == "ollama-local":
                discovery["local_model_authorization"] = _authorize_local_model_access(
                    stack,
                    state_root=state_root,
                    standalone_python=standalone_python,
                    daemon_environment=daemon_environment,
                    daemon_url=daemon_url,
                    expected_daemon_fingerprint=str(discovery["daemon_fingerprint"]),
                    provider_identity=provider_identity,
                )
            refreshed_connection = _request_json(
                platform_url,
                f"/api/v1/local-lilies/connections/{connection_id}/refresh",
                method="POST",
                token=runner_secrets["platform_api_token"],
            )
            if (
                not isinstance(refreshed_connection, dict)
                or refreshed_connection.get("connection_id") != connection_id
                or refreshed_connection.get("base_url") != daemon_url
                or refreshed_connection.get("status") != "connected"
                or refreshed_connection.get("daemon_fingerprint") != daemon_fingerprint
            ):
                raise EnterpriseExperimentError(
                    "refreshed connection changed its persisted daemon identity"
                )
            lifecycle.finish(outcome="connection_refreshed")
            lifecycle.start("assignment")
            assignment = _request_json(
                platform_url,
                f"/api/v1/local-lilies/assignments/{assignment_id}/resume",
                method="POST",
                token=runner_secrets["platform_api_token"],
                timeout=180.0,
            )
            if not isinstance(assignment, dict):
                raise EnterpriseExperimentError("resume returned an invalid assignment")
            _assert_assignment_identity(
                assignment,
                assignment_id=assignment_id,
                application_id=application_id,
                connection_id=connection_id,
                session_id=session_id,
                build_id=build_id,
            )
            if collaboration_policy == "auto_forward":
                _set_auto_forward(
                    platform_url,
                    runner_secrets["platform_api_token"],
                    assignment_id=assignment_id,
                )
            lifecycle.finish(outcome="completed")
            lifecycle.start("builder")
            assignment = _poll_assignment(
                platform_url,
                runner_secrets["platform_api_token"],
                assignment_id=assignment_id,
                deadline_seconds=args.deadline_seconds,
                operational_permission_policy=operational_permission_policy,
                token_state_root=state_root,
                token_monitor_interval=args.token_monitor_interval,
                connection_id=connection_id,
                token_attempt_id=attempt_id,
                expected_session_id=session_id,
                global_usage_baseline=global_usage_baseline,
            )
            _assert_assignment_identity(
                assignment,
                assignment_id=assignment_id,
                application_id=application_id,
                connection_id=connection_id,
                session_id=session_id,
                build_id=build_id,
            )
            session_budget = _session_budget_receipt(
                _standalone_observability_snapshot(
                    platform_url=platform_url,
                    platform_token=runner_secrets["platform_api_token"],
                    connection_id=connection_id,
                ),
                assignment=assignment,
                discovery=discovery,
                sequence=assignment.get("runner_session_budget_sequence"),
                global_baseline=global_usage_baseline,
            )
            lifecycle.finish(outcome=_builder_lifecycle_outcome(assignment))
            lifecycle.start("host_verification")
            _environment_command(
                state_root,
                "snapshot",
                "--seed",
                args.seed,
                "--phase",
                "final",
                environment=host_environment,
            )
            host_snapshots.append(
                _archive_host_snapshot(
                    state_root,
                    seed=args.seed,
                    phase="final",
                    attempt_id=attempt_id,
                )
            )
            if str(assignment.get("phase")) == "completed":
                host_verification = _run_host_snapshot_verification(
                    state_root,
                    seed=args.seed,
                )
                if host_verification.get("verdict") == "independently_verified":
                    lifecycle.finish(outcome="independently_verified")
                else:
                    lifecycle.finish(outcome="verification_failed")
            else:
                lifecycle.finish(outcome="skipped")
            lifecycle.start("platform_verification")
            if (
                host_verification is not None
                and host_verification.get("verdict") == "independently_verified"
            ):
                platform_verification = _run_platform_independent_verification(
                    platform_url,
                    runner_secrets["platform_api_token"],
                    assignment_id=assignment_id,
                )
                builder_provenance = _builder_provenance_receipt(
                    platform_url,
                    runner_secrets["platform_api_token"],
                    assignment=assignment,
                    discovery=discovery,
                    runtime_identity=runtime_identity,
                    qualified_verification=platform_verification,
                )
                lifecycle.finish(
                    outcome=str(platform_verification.get("claim_status") or "completed")
                )
            else:
                lifecycle.finish(outcome="skipped")
        if (
            application is None
            or connection is None
            or assignment is None
            or platform_port is None
            or daemon_port is None
            or collaboration_policy is None
            or operational_permission_policy is None
            or run_attempt_id is None
            or environment_instance_id is None
            or runtime_identity is None
            or environment_generation is None
            or empty_draft_receipt is None
            or historical_identity is None
            or global_usage_baseline is None
        ):
            raise EnterpriseExperimentError("resume completed without its safe persisted identity")
        connection["status"] = "connected"
        _write_active_run(
            state_root,
            seed=args.seed,
            collaboration_policy=collaboration_policy,
            operational_permission_policy=operational_permission_policy,
            platform_port=platform_port,
            daemon_port=daemon_port,
            application=application,
            connection=connection,
            assignment=assignment,
            run_attempt_id=run_attempt_id,
            attempt_started_at=attempt_started_at,
            environment_instance_id=environment_instance_id,
            environment_generation=environment_generation,
            empty_draft_receipt=empty_draft_receipt,
            historical_identity=historical_identity,
            global_usage_baseline=global_usage_baseline,
            runtime_identity=runtime_identity,
        )
        status = _enterprise_run_status(
            assignment,
            host_verification=host_verification,
            platform_verification=platform_verification,
            session_budget=session_budget,
        )
        token_monitor = _token_monitor_evidence(
            state_root,
            interval_seconds=float(getattr(args, "token_monitor_interval", 5.0)),
            attempt_id=attempt_id,
        )
        path, report_committed, report_error = _finalize_run_evidence(
            lifecycle,
            resources,
            evidence_root,
            seed=args.seed,
            started_at=attempt_started_at,
            status=status,
            package=package,
            application=application,
            connection=connection,
            assignment=assignment,
            secret_receipts=[],
            host_snapshots=host_snapshots,
            platform_verification=platform_verification,
            host_verification=host_verification,
            error=None,
            model_egress_authorized=bool(getattr(args, "enable_model_egress", False)),
            token_monitor=token_monitor,
            discovery=discovery,
            session_budget=session_budget,
            freshness={
                "environment_generation": environment_generation,
                "empty_draft": empty_draft_receipt,
                "historical_identity": historical_identity,
            },
            builder_provenance=builder_provenance,
            attempt_id=attempt_id,
            continuation_started_at=resume_started_at,
        )
        print(path)
        if not report_committed:
            _print_safe_error(report_error or EnterpriseExperimentError("report failed"))
            return 2
        return 0 if status == "enterprise_run_passed" else 3
    except BaseException as error:
        was_interruption = not isinstance(error, Exception)
        try:
            resources.ensure_closed(error)
        except BaseException as cleanup_error:
            error = cleanup_error
        token_monitor = _token_monitor_evidence(
            state_root,
            interval_seconds=float(getattr(args, "token_monitor_interval", 5.0)),
            attempt_id=attempt_id,
        )
        path, _, report_error = _finalize_run_evidence(
            lifecycle,
            resources,
            evidence_root,
            seed=args.seed,
            started_at=attempt_started_at,
            status="resume_failed",
            package=package,
            application=application,
            connection=connection,
            assignment=assignment,
            secret_receipts=[],
            host_snapshots=host_snapshots,
            platform_verification=platform_verification,
            host_verification=host_verification,
            error=error,
            model_egress_authorized=bool(getattr(args, "enable_model_egress", False)),
            token_monitor=token_monitor,
            discovery=discovery,
            session_budget=session_budget,
            freshness={
                "environment_generation": environment_generation,
                "empty_draft": empty_draft_receipt,
                "historical_identity": historical_identity,
            },
            builder_provenance=builder_provenance,
            attempt_id=attempt_id,
            continuation_started_at=resume_started_at,
        )
        print(path, file=sys.stderr)
        _print_safe_error(report_error or error)
        if was_interruption:
            raise
        return 2


def prepare(args: argparse.Namespace) -> int:
    state_root = args.state_root.resolve()
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _runner_secrets(state_root, create=True)
    package = _freeze_package(state_root / "platform-data")
    result = {
        "schema_version": "v0.4.13-t01h-runner-preparation-1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "state_root": str(state_root),
        "package_public_summary_digest": package["public_summary_digest"],
        "package_sealed_digest": package["sealed_package_digest"],
        "runner_source_digest": _digest(Path(__file__).read_bytes()),
        "status": "prepared_environment_not_started",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _add_provider_launch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-provider",
        choices=("deepseek", "ollama-local"),
        default="deepseek",
    )
    parser.add_argument("--model")
    parser.add_argument("--provider-max-output-tokens", type=int, default=16_384)
    parser.add_argument("--ollama-base-url")
    parser.add_argument("--ollama-model-manifest-digest")
    parser.add_argument("--ollama-template-digest")
    parser.add_argument("--ollama-context-window-tokens", type=int)
    parser.add_argument("--ollama-binary", type=Path)
    parser.add_argument("--ollama-models-dir", type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the frozen EXP-LILIES-001 task only through the platform formal-build boundary."
        )
    )
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument(
        "--evidence-root",
        type=Path,
        default=ROOT / "docs" / "evidence" / "v0.4.13" / "t01h" / "runs",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("prepare")
    run = subparsers.add_parser("run")
    run.add_argument("--seed", choices=("debug", "101", "202", "303"), required=True)
    run.add_argument(
        "--collaboration-policy",
        choices=("manual", "auto_forward"),
        default="manual",
    )
    run.add_argument(
        "--operational-permission-policy",
        choices=OPERATIONAL_PERMISSION_POLICIES,
        default="task_local_workspace",
        help=(
            "Resolve exact task-local workspace writes without human supervision; "
            "all other permission classes remain fail-closed."
        ),
    )
    run.add_argument("--platform-port", type=int, default=DEFAULT_PLATFORM_PORT)
    run.add_argument("--daemon-port", type=int, default=DEFAULT_DAEMON_PORT)
    run.add_argument("--deadline-seconds", type=float, default=10_800)
    run.add_argument(
        "--token-monitor-interval",
        type=float,
        default=5.0,
        help="Persist read-only token/cost snapshots every N seconds; zero disables.",
    )
    run.add_argument(
        "--enable-model-egress",
        action="store_true",
        help="Allow real provider HTTP only for this explicit experiment invocation.",
    )
    _add_provider_launch_arguments(run)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--seed", choices=("debug", "101", "202", "303"), required=True)
    resume.add_argument("--deadline-seconds", type=float, default=10_800)
    resume.add_argument(
        "--token-monitor-interval",
        type=float,
        default=5.0,
        help="Persist read-only token/cost snapshots every N seconds; zero disables.",
    )
    resume.add_argument(
        "--enable-model-egress",
        action="store_true",
        help="Allow real provider HTTP only for this explicit resume invocation.",
    )
    _add_provider_launch_arguments(resume)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "prepare":
        return prepare(args)
    if args.command == "run":
        return run_seed(args)
    if args.command == "resume":
        return resume_seed(args)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
