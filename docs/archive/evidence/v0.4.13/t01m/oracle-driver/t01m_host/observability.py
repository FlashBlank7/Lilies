"""Actively paired, fail-closed daemon observability snapshots for A07."""

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .commands import run
from .constants import APPLICATION_ID
from .util import (
    OracleError,
    canonical_json_bytes,
    require_hex_digest,
    sha256_file,
    write_new_or_replace,
)

SNAPSHOT_KIND = "t01m-daemon-observability-snapshot"
LEDGER_FIELDS = {
    "model_calls",
    "input_tokens",
    "output_tokens",
    "unknown_token_events",
    "tool_calls",
    "cost_microunits",
}
CLOCK_TOLERANCE_NS = 5_000_000_000


def _is_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value)
    ):
        raise OracleError(f"invalid observability {label}")
    return value


def validate_snapshot(
    snapshot: dict[str, Any],
    *,
    phase: str,
    run_nonce: str,
    producer_sha256: str,
    invocation_started_ns: int,
    invocation_completed_ns: int,
) -> None:
    if (
        not _is_nonnegative_int(invocation_started_ns)
        or not _is_nonnegative_int(invocation_completed_ns)
        or invocation_started_ns > invocation_completed_ns
    ):
        raise OracleError("observability invocation timestamps are invalid")
    if set(snapshot) != {
        "schema_version",
        "kind",
        "phase",
        "run_nonce",
        "capture_nonce",
        "capture_id",
        "captured_at_unix_ns",
        "coverage_epoch",
        "daemon",
        "ledger",
        "attestation",
    }:
        raise OracleError("observability snapshot field set is not canonical")
    if (
        type(snapshot["schema_version"]) is not int
        or snapshot["schema_version"] != 1
        or snapshot["kind"] != SNAPSHOT_KIND
        or snapshot["phase"] != phase
        or snapshot["run_nonce"] != run_nonce
    ):
        raise OracleError("observability snapshot identity mismatch")
    _identifier(snapshot["run_nonce"], "run_nonce")
    _identifier(snapshot["capture_id"], "capture_id")
    _identifier(snapshot["capture_nonce"], "capture_nonce")
    captured = snapshot["captured_at_unix_ns"]
    if (
        not _is_nonnegative_int(captured)
        or captured
        < invocation_started_ns - CLOCK_TOLERANCE_NS
        or captured
        > invocation_completed_ns + CLOCK_TOLERANCE_NS
    ):
        raise OracleError("observability captured_at is outside active invocation")
    epoch = snapshot["coverage_epoch"]
    if (
        not isinstance(epoch, dict)
        or set(epoch) != {"id", "started_at_unix_ns"}
        or not _is_nonnegative_int(epoch["started_at_unix_ns"])
        or epoch["started_at_unix_ns"] > captured
    ):
        raise OracleError("observability coverage epoch is invalid")
    _identifier(epoch["id"], "coverage_epoch.id")
    daemon = snapshot["daemon"]
    if (
        not isinstance(daemon, dict)
        or set(daemon) != {"fingerprint_sha256", "instance_id"}
    ):
        raise OracleError("observability daemon identity is incomplete")
    require_hex_digest(daemon["fingerprint_sha256"], "daemon fingerprint")
    _identifier(daemon["instance_id"], "daemon.instance_id")
    ledger = snapshot["ledger"]
    if (
        not isinstance(ledger, dict)
        or set(ledger) != LEDGER_FIELDS
        or any(
            not _is_nonnegative_int(ledger[field])
            for field in LEDGER_FIELDS
        )
    ):
        raise OracleError("observability ledger counter schema is incomplete")
    attestation = snapshot["attestation"]
    if (
        not isinstance(attestation, dict)
        or set(attestation) != {"complete", "producer_sha256", "read_only"}
        or attestation["complete"] is not True
        or attestation["producer_sha256"] != producer_sha256
        or attestation["read_only"] is not True
    ):
        raise OracleError("observability client attestation mismatch")


class DaemonObservabilityClient:
    def __init__(self, executable: Path, artifact_root: Path):
        if (
            not executable.is_absolute()
            or executable.is_symlink()
            or not executable.is_file()
            or not os.access(executable, os.X_OK)
        ):
            raise OracleError(
                "observability client must be an absolute executable regular file"
            )
        self.executable = executable
        self.producer_sha256 = sha256_file(executable)
        self.artifact_root = artifact_root
        self.run_nonce = secrets.token_hex(32)
        self._captured_phases: list[str] = []

    def _require_unchanged_executable(self) -> None:
        if (
            self.executable.is_symlink()
            or not self.executable.is_file()
            or not os.access(self.executable, os.X_OK)
            or sha256_file(self.executable) != self.producer_sha256
        ):
            raise OracleError("observability client identity changed during A07")

    def capture(self, phase: str) -> dict[str, Any]:
        if phase not in {"before", "after"}:
            raise OracleError("observability phase must be before or after")
        expected_history = [] if phase == "before" else ["before"]
        if self._captured_phases != expected_history:
            raise OracleError("observability captures must run exactly before then after")
        self._require_unchanged_executable()
        capture_nonce = secrets.token_hex(32)
        invocation_started = time.time_ns()
        result = run(
            [
                self.executable,
                "capture",
                "--phase",
                phase,
                "--run-nonce",
                self.run_nonce,
                "--capture-nonce",
                capture_nonce,
                "--application-id",
                APPLICATION_ID,
            ],
            timeout=60.0,
        )
        invocation_completed = time.time_ns()
        self._require_unchanged_executable()
        if result.stderr:
            raise OracleError("observability client wrote unexpected stderr")
        try:
            snapshot = json.loads(result.stdout.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OracleError("observability client returned invalid JSON") from error
        if not isinstance(snapshot, dict):
            raise OracleError("observability snapshot must be a JSON object")
        if snapshot.get("capture_nonce") != capture_nonce:
            raise OracleError("observability capture nonce mismatch")
        if result.stdout != canonical_json_bytes(snapshot):
            raise OracleError("observability snapshot bytes are not canonical JSON")
        validate_snapshot(
            snapshot,
            phase=phase,
            run_nonce=self.run_nonce,
            producer_sha256=self.producer_sha256,
            invocation_started_ns=invocation_started,
            invocation_completed_ns=invocation_completed,
        )
        raw_digest = hashlib.sha256(result.stdout).hexdigest()
        artifact_path_identity = f"artifacts/daemon-observability-{phase}.json"
        document = {
            "schema_version": 1,
            "artifact_path_identity": artifact_path_identity,
            "client": {
                "path_identity": "explicit --observability-client",
                "sha256": self.producer_sha256,
            },
            "host_invocation_started_unix_ns": invocation_started,
            "host_invocation_completed_unix_ns": invocation_completed,
            "raw_snapshot_sha256": raw_digest,
            "snapshot": snapshot,
        }
        write_new_or_replace(
            self.artifact_root / f"daemon-observability-{phase}.json",
            canonical_json_bytes(document),
        )
        self._captured_phases.append(phase)
        return document


def validate_observability_pair(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    workload_started_ns: int,
    workload_completed_ns: int,
) -> dict[str, Any]:
    if (
        not _is_nonnegative_int(workload_started_ns)
        or not _is_nonnegative_int(workload_completed_ns)
        or workload_started_ns > workload_completed_ns
    ):
        raise OracleError("observability workload timestamps are invalid")
    expected_wrapper_fields = {
        "schema_version",
        "artifact_path_identity",
        "client",
        "host_invocation_started_unix_ns",
        "host_invocation_completed_unix_ns",
        "raw_snapshot_sha256",
        "snapshot",
    }
    for document, phase in ((before, "before"), (after, "after")):
        if (
            not isinstance(document, dict)
            or set(document) != expected_wrapper_fields
            or type(document.get("schema_version")) is not int
            or document.get("schema_version") != 1
            or document.get("artifact_path_identity")
            != f"artifacts/daemon-observability-{phase}.json"
            or not isinstance(document.get("client"), dict)
            or set(document["client"]) != {"path_identity", "sha256"}
            or document["client"].get("path_identity")
            != "explicit --observability-client"
            or not isinstance(document.get("snapshot"), dict)
            or not _is_nonnegative_int(
                document.get("host_invocation_started_unix_ns")
            )
            or not _is_nonnegative_int(
                document.get("host_invocation_completed_unix_ns")
            )
            or document["host_invocation_started_unix_ns"]
            > document["host_invocation_completed_unix_ns"]
        ):
            raise OracleError("observability capture wrapper is not canonical")
        require_hex_digest(document["client"].get("sha256"), "observability client")
        require_hex_digest(
            document.get("raw_snapshot_sha256"), "raw observability snapshot"
        )
        if document["raw_snapshot_sha256"] != hashlib.sha256(
            canonical_json_bytes(document["snapshot"])
        ).hexdigest():
            raise OracleError("observability raw snapshot digest mismatch")
        validate_snapshot(
            document["snapshot"],
            phase=phase,
            run_nonce=document["snapshot"].get("run_nonce"),
            producer_sha256=document["client"]["sha256"],
            invocation_started_ns=document["host_invocation_started_unix_ns"],
            invocation_completed_ns=document["host_invocation_completed_unix_ns"],
        )
    left = before["snapshot"]
    right = after["snapshot"]
    if (
        left["phase"] != "before"
        or right["phase"] != "after"
        or left["run_nonce"] != right["run_nonce"]
        or before["client"] != after["client"]
    ):
        raise OracleError("observability snapshots are not one actively paired run")
    if (
        left["capture_id"] == right["capture_id"]
        or left["capture_nonce"] == right["capture_nonce"]
        or before["raw_snapshot_sha256"] == after["raw_snapshot_sha256"]
        or before["artifact_path_identity"] == after["artifact_path_identity"]
    ):
        raise OracleError("observability before/after must be distinct captures")
    if left["coverage_epoch"] != right["coverage_epoch"]:
        raise OracleError("observability coverage epoch changed across workload")
    if left["daemon"] != right["daemon"]:
        raise OracleError("observed daemon instance/fingerprint changed")
    if not (
        left["captured_at_unix_ns"] < workload_started_ns
        <= workload_completed_ns
        < right["captured_at_unix_ns"]
    ):
        raise OracleError("observability snapshots do not strictly bracket workload")
    if left["ledger"] != right["ledger"]:
        raise OracleError("daemon model/token/tool/cost ledger counters changed")
    return {
        "schema_version": 1,
        "run_nonce": left["run_nonce"],
        "coverage_epoch": left["coverage_epoch"],
        "daemon": left["daemon"],
        "ledger_counters_unchanged": True,
        "before_artifact_path_identity": before["artifact_path_identity"],
        "after_artifact_path_identity": after["artifact_path_identity"],
        "before_raw_snapshot_sha256": before["raw_snapshot_sha256"],
        "after_raw_snapshot_sha256": after["raw_snapshot_sha256"],
        "before_capture_nonce": left["capture_nonce"],
        "after_capture_nonce": right["capture_nonce"],
        "before_capture_id": left["capture_id"],
        "after_capture_id": right["capture_id"],
        "result": "pass",
    }
