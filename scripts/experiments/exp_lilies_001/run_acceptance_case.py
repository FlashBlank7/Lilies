#!/usr/bin/env python3
"""Run one EXP-LILIES-001 acceptance case through public platform APIs.

This is a task-author adapter, not a Builder tool.  It deliberately treats the
environment controller and host verifier as opaque subprocesses and publishes
only a signed, aggregate receipt.  Protected records, verifier differences,
host credentials, and bearer tokens never enter the receipt or subprocess
arguments.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parents[3]
EXPERIMENTS_ROOT = Path(__file__).resolve().parents[1]
if str(EXPERIMENTS_ROOT) not in sys.path:
    sys.path.insert(0, str(EXPERIMENTS_ROOT))

import real_project_testkit as testkit  # noqa: E402


TASK_ID = "EXP-LILIES-001"
REVISION = 28
SEEDS = ("debug", "101", "202", "303")
PACKAGE_ROOT = (
    ROOT
    / "docs"
    / "experiments"
    / "lilies-collaboration"
    / TASK_ID
    / str(REVISION)
)
ENVIRONMENT_CONTROL = Path(__file__).with_name("environment_control.py")
HOST_VERIFIER = Path(__file__).with_name("verify_host_snapshot.py")
HOST_ORACLE = PACKAGE_ROOT / "protected" / "oracle" / "host-oracle.json"
SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
RAW_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:[\]-]{1,240}$")
MAX_PRIVATE_FILE_BYTES = 64 * 1024
MAX_TRACE_EVENTS = 100_000
MAX_TRACE_IDENTITIES = 10_000
MAX_RESUMES = 1_000
REQUIRED_ARTIFACTS = {
    "enterprise-result.json": "application/json",
    "reconciliation.xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}
WORKFLOW_RECORD_FIELDS = (
    "record_id",
    "source_id",
    "supplier",
    "purchase_order",
    "part_number",
    "lot_number",
    "quantity",
    "document_date",
    "certificate_type",
    "ocr_confidence",
)
HOST_SECRET_NAMES = {
    "exp-lilies-001-environment-attestation": (
        "secrets.json",
        "attestation_secret",
    ),
    "exp-lilies-001-paperless-builder-token": (
        "credentials.json",
        "paperless_builder_token",
    ),
    "exp-lilies-001-inventree-builder-token": (
        "credentials.json",
        "inventree_builder_token",
    ),
}


class AcceptanceCaseError(RuntimeError):
    """A fail-closed acceptance rejection with a fixed public reason code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HostSecretInstallError(AcceptanceCaseError):
    def __init__(self, installed_count: int) -> None:
        super().__init__("host_secret_rotation_failed")
        self.installed_count = installed_count


class WorkflowExecutionError(AcceptanceCaseError):
    def __init__(self, reason: str, run_receipt: dict[str, Any]) -> None:
        super().__init__(reason)
        self.run_receipt = run_receipt


@dataclass(frozen=True)
class CaseConfig:
    platform_url: str
    state_root: Path
    application_id: str
    assignment_id: str
    session_id: str
    version: int
    content_hash: str
    seed: str
    timeout_seconds: float = 900.0
    max_resume_count: int = MAX_RESUMES


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


def _normalize_sha256(value: Any) -> str:
    """Normalize one public raw/prefixed SHA-256 value or reject it."""

    if not isinstance(value, str):
        raise ValueError("SHA-256 value is not a string")
    raw = value.removeprefix("sha256:")
    if RAW_SHA256_PATTERN.fullmatch(raw) is None:
        raise ValueError("SHA-256 value is not canonical")
    return f"sha256:{raw}"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _read_private_bytes(path: Path, *, max_bytes: int = MAX_PRIVATE_FILE_BYTES) -> bytes:
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AcceptanceCaseError("private_input_rejected") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or not 0 < metadata.st_size <= max_bytes
        ):
            raise AcceptanceCaseError("private_input_rejected")
        payload = os.read(descriptor, metadata.st_size + 1)
        if len(payload) != metadata.st_size:
            raise AcceptanceCaseError("private_input_rejected")
        return payload
    finally:
        os.close(descriptor)


def _secret_from_file(path: Path, *, json_field: str | None = None) -> bytes:
    payload = _read_private_bytes(path)
    if json_field is not None:
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AcceptanceCaseError("private_input_rejected") from error
        if not isinstance(value, Mapping) or not isinstance(value.get(json_field), str):
            raise AcceptanceCaseError("private_input_rejected")
        payload = value[json_field].encode("utf-8")
    else:
        payload = payload.strip()
    if not 16 <= len(payload) <= 8_192 or b"\x00" in payload:
        raise AcceptanceCaseError("private_input_rejected")
    return payload


def _receipt_signing_key(path: Path) -> bytes:
    payload = _read_private_bytes(path)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if isinstance(value, Mapping):
        candidate = value.get("acceptance_receipt_signing_key")
        if not isinstance(candidate, str):
            candidate = value.get("collaborative_development_signing_key")
        if not isinstance(candidate, str):
            raise AcceptanceCaseError("private_input_rejected")
        payload = candidate.encode("utf-8")
    else:
        payload = payload.strip()
    if not 32 <= len(payload) <= 8_192 or b"\x00" in payload:
        raise AcceptanceCaseError("private_input_rejected")
    return payload


def _platform_token(path: Path) -> str:
    payload = _read_private_bytes(path)
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        value = None
    if isinstance(value, Mapping) and isinstance(value.get("platform_api_token"), str):
        token = value["platform_api_token"]
    else:
        try:
            token = payload.decode("utf-8").strip()
        except UnicodeDecodeError as error:
            raise AcceptanceCaseError("private_input_rejected") from error
    if not 16 <= len(token.encode("utf-8")) <= 8_192 or "\x00" in token:
        raise AcceptanceCaseError("private_input_rejected")
    return token


def _write_private_json(path: Path, value: Mapping[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() or path.is_symlink():
        raise AcceptanceCaseError("receipt_write_rejected")
    payload = _canonical_json(value) + b"\n"
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
        os.chmod(path, 0o600, follow_symlinks=False)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def sign_receipt(unsigned: Mapping[str, Any], signing_key: bytes) -> dict[str, Any]:
    if len(signing_key) < 32:
        raise AcceptanceCaseError("private_input_rejected")
    payload = _canonical_json(unsigned)
    signature = hmac.new(signing_key, payload, hashlib.sha256).digest()
    return {
        **dict(unsigned),
        "receipt_digest": _digest(payload),
        "signature": {
            "algorithm": "hmac-sha256",
            "assurance": "task_author_tamper_evidence_only",
            "key_id": _digest(signing_key),
            "signed_digest": _digest(payload),
            "value": base64.urlsafe_b64encode(signature).decode("ascii").rstrip("="),
        },
    }


def verify_receipt(receipt: Mapping[str, Any], signing_key: bytes) -> bool:
    signature = receipt.get("signature")
    receipt_digest = receipt.get("receipt_digest")
    if not isinstance(signature, Mapping) or not isinstance(receipt_digest, str):
        return False
    unsigned = {
        key: value
        for key, value in receipt.items()
        if key not in {"receipt_digest", "signature"}
    }
    payload = _canonical_json(unsigned)
    encoded = base64.urlsafe_b64encode(
        hmac.new(signing_key, payload, hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return (
        signature.get("algorithm") == "hmac-sha256"
        and signature.get("assurance") == "task_author_tamper_evidence_only"
        and signature.get("key_id") == _digest(signing_key)
        and signature.get("signed_digest") == _digest(payload)
        and receipt_digest == _digest(payload)
        and isinstance(signature.get("value"), str)
        and secrets.compare_digest(signature["value"], encoded)
    )


def _validate_config(config: CaseConfig) -> None:
    try:
        normalized_application_id = str(UUID(config.application_id))
        normalized_assignment_id = str(UUID(config.assignment_id))
        normalized_session_id = str(UUID(config.session_id))
        normalized_platform_url = testkit.normalize_loopback_base_url(
            config.platform_url
        )
    except (ValueError, AttributeError) as error:
        raise AcceptanceCaseError("case_binding_rejected") from error
    if (
        normalized_application_id != config.application_id
        or normalized_assignment_id != config.assignment_id
        or normalized_session_id != config.session_id
        or normalized_platform_url != config.platform_url.rstrip("/")
        or isinstance(config.version, bool)
        or config.version < 1
        or config.seed not in SEEDS
        or SHA256_PATTERN.fullmatch(config.content_hash) is None
        or config.timeout_seconds <= 0
        or isinstance(config.max_resume_count, bool)
        or not 0 <= config.max_resume_count <= MAX_RESUMES
    ):
        raise AcceptanceCaseError("case_binding_rejected")


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise AcceptanceCaseError("private_state_rejected") from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise AcceptanceCaseError("private_state_rejected")


def _platform_json(
    method: str,
    config: CaseConfig,
    token: str,
    path: str,
    body: Any | None = None,
) -> Any:
    try:
        return testkit.platform_json(
            method,
            config.platform_url,
            token,
            path,
            body,
            expose_error_detail=False,
        )
    except Exception as error:
        raise AcceptanceCaseError("platform_request_failed") from error


def _application_guard(config: CaseConfig, token: str) -> dict[str, Any]:
    application = _platform_json(
        "GET", config, token, f"/api/v1/applications/{config.application_id}"
    )
    versions = _platform_json(
        "GET",
        config,
        token,
        f"/api/v1/applications/{config.application_id}/versions",
    )
    runtime = _platform_json(
        "GET",
        config,
        token,
        f"/api/v1/applications/{config.application_id}/runtime-definition",
    )
    if (
        not isinstance(application, Mapping)
        or application.get("id") != config.application_id
        or application.get("active_version") != config.version
        or not isinstance(application.get("draft_revision"), int)
        or not isinstance(versions, list)
        or not isinstance(runtime, Mapping)
    ):
        raise AcceptanceCaseError("published_version_guard_failed")
    try:
        application_hash = _normalize_sha256(application.get("content_hash"))
        runtime_hash = _normalize_sha256(runtime.get("content_hash"))
    except ValueError as error:
        raise AcceptanceCaseError("published_version_guard_failed") from error
    normalized_versions: list[dict[str, Any]] = []
    matching: list[dict[str, Any]] = []
    for item in versions:
        if (
            not isinstance(item, Mapping)
            or isinstance(item.get("version"), bool)
            or not isinstance(item.get("version"), int)
        ):
            raise AcceptanceCaseError("published_version_guard_failed")
        try:
            version_hash = _normalize_sha256(item.get("content_hash"))
        except ValueError as error:
            raise AcceptanceCaseError("published_version_guard_failed") from error
        projection = {
            "version": item["version"],
            "content_hash": version_hash,
        }
        normalized_versions.append(projection)
        if item["version"] == config.version:
            matching.append({**dict(item), "content_hash": version_hash})
    if (
        len(matching) != 1
        or matching[0].get("content_hash") != config.content_hash
        or runtime.get("application_id") != config.application_id
        or runtime.get("source") != "published"
        or runtime.get("version") != config.version
        or runtime_hash != config.content_hash
    ):
        raise AcceptanceCaseError("published_version_guard_failed")
    return {
        "application_id": config.application_id,
        "active_version": config.version,
        "published_content_hash": config.content_hash,
        "version_content_hash": matching[0]["content_hash"],
        "runtime_content_hash": runtime_hash,
        "draft_revision": application["draft_revision"],
        "draft_content_hash": application_hash,
        "version_inventory_digest": _digest(_canonical_json(normalized_versions)),
        "version_count": len(normalized_versions),
    }


def _error_has_http_status(error: BaseException, status_code: int) -> bool:
    pending: list[BaseException] = [error]
    visited: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in visited:
            continue
        visited.add(id(current))
        if (
            isinstance(current, urllib.error.HTTPError)
            and current.code == status_code
        ):
            return True
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)
    return False


def _formal_execution_policy_guard(
    config: CaseConfig,
    token: str,
) -> dict[str, Any]:
    versions = _platform_json(
        "GET",
        config,
        token,
        f"/api/v1/applications/{config.application_id}/versions",
    )
    if not isinstance(versions, list):
        raise AcceptanceCaseError("builder_handoff_guard_failed")
    matching = [
        item
        for item in versions
        if isinstance(item, Mapping) and item.get("version") == config.version
    ]
    if len(matching) != 1:
        raise AcceptanceCaseError("builder_handoff_guard_failed")
    version = matching[0]
    decision = version.get("publication_decision")
    policy = (
        decision.get("execution_policy_snapshot")
        if isinstance(decision, Mapping)
        else None
    )
    try:
        version_hash = _normalize_sha256(version.get("content_hash"))
        policy_digest = _normalize_sha256(
            policy.get("policy_digest") if isinstance(policy, Mapping) else None
        )
    except ValueError as error:
        raise AcceptanceCaseError("builder_handoff_guard_failed") from error
    if (
        version.get("application_id") != config.application_id
        or version_hash != config.content_hash
        or not isinstance(decision, Mapping)
        or decision.get("application_id") != config.application_id
        or not isinstance(policy, Mapping)
        or policy.get("assignment_id") != config.assignment_id
        or policy.get("session_id") != config.session_id
        or not isinstance(policy.get("allowed_nested_application_ids"), list)
        or config.application_id not in policy["allowed_nested_application_ids"]
    ):
        raise AcceptanceCaseError("builder_handoff_guard_failed")
    return {
        "application_id": config.application_id,
        "version": config.version,
        "content_hash": version_hash,
        "policy_digest": policy_digest,
    }


def _formal_builder_handoff_guard(
    config: CaseConfig,
    token: str,
) -> dict[str, Any]:
    inventory = _platform_json(
        "GET",
        config,
        token,
        "/api/v1/studio/collaboration/channels?limit=500",
    )
    channels = inventory.get("channels") if isinstance(inventory, Mapping) else None
    if not isinstance(channels, list):
        raise AcceptanceCaseError("builder_handoff_guard_failed")
    matching = [
        channel
        for channel in channels
        if (
            isinstance(channel, Mapping)
            and channel.get("assignment_id") == config.assignment_id
            and channel.get("lilies_session_id") == config.session_id
            and channel.get("task_id") == TASK_ID
            and channel.get("task_revision") == REVISION
            and isinstance(channel.get("application_ids"), list)
            and config.application_id in channel["application_ids"]
        )
    ]
    if len(matching) != 1:
        raise AcceptanceCaseError("builder_handoff_guard_failed")
    channel = matching[0]
    raw_channel_id = channel.get("channel_id")
    try:
        channel_id = str(UUID(raw_channel_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise AcceptanceCaseError("builder_handoff_guard_failed") from error
    if raw_channel_id != channel_id or channel.get("status") != "closed":
        raise AcceptanceCaseError("builder_handoff_guard_failed")
    policy = _formal_execution_policy_guard(config, token)
    return {
        "assignment_id": config.assignment_id,
        "session_id": config.session_id,
        "phase": "closed",
        "status": "closed",
        "channel_id": channel_id,
        "channel_status": "closed",
        "handoff_route": "formal_collaboration",
        "execution_policy_digest": policy["policy_digest"],
        "lilies_discovery_status": "unavailable",
    }


def _builder_handoff_guard(config: CaseConfig, token: str) -> dict[str, Any]:
    assignment: Any | None = None
    formal_assignment = False
    try:
        assignment = _platform_json(
            "GET",
            config,
            token,
            f"/api/v1/local-lilies/assignments/{config.assignment_id}",
        )
    except AcceptanceCaseError as error:
        if not _error_has_http_status(error, 404):
            raise
        formal_assignment = True
    status = _platform_json("GET", config, token, "/api/v1/local-lilies/status")
    discovery = status.get("discovery") if isinstance(status, Mapping) else None
    if not isinstance(discovery, Mapping) or discovery.get("status") != "unavailable":
        raise AcceptanceCaseError("builder_handoff_guard_failed")
    if formal_assignment:
        return _formal_builder_handoff_guard(config, token)
    if (
        not isinstance(assignment, Mapping)
        or assignment.get("assignment_id") != config.assignment_id
        or assignment.get("application_id") != config.application_id
        or assignment.get("session_id") != config.session_id
        or assignment.get("phase") != "completed"
        or assignment.get("status") != "completed"
        or assignment.get("daemon_status") != "completed"
    ):
        raise AcceptanceCaseError("builder_handoff_guard_failed")
    return {
        "assignment_id": config.assignment_id,
        "session_id": config.session_id,
        "phase": "completed",
        "status": "completed",
        "daemon_status": "completed",
        "lilies_discovery_status": "unavailable",
    }


def _protected_environment() -> dict[str, str]:
    allowed = ("PATH", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR")
    environment = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    environment.update({"LANG": "C", "LC_ALL": "C"})
    return environment


def _protected_run(
    argv: tuple[str, ...],
    *,
    private_root: Path,
    allowed_returncodes: frozenset[int] = frozenset({0}),
    timeout_seconds: float = 900,
) -> tuple[int, float]:
    stdout_path = private_root / f"subprocess-{uuid4().hex}.stdout"
    stderr_path = private_root / f"subprocess-{uuid4().hex}.stderr"
    started = time.monotonic()
    try:
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            os.chmod(stdout_path, 0o600)
            os.chmod(stderr_path, 0o600)
            completed = subprocess.run(
                argv,
                cwd=ROOT,
                env=_protected_environment(),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=timeout_seconds,
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise AcceptanceCaseError("protected_subprocess_failed") from error
    duration = max(0.0, time.monotonic() - started)
    if completed.returncode not in allowed_returncodes:
        raise AcceptanceCaseError("protected_subprocess_failed")
    return completed.returncode, duration


def _environment_command(
    config: CaseConfig,
    private_root: Path,
    command: str,
    *extra: str,
) -> dict[str, Any]:
    _, duration = _protected_run(
        (
            sys.executable,
            "-I",
            str(ENVIRONMENT_CONTROL),
            "--state-root",
            str(config.state_root),
            "--package-root",
            str(PACKAGE_ROOT),
            command,
            *extra,
        ),
        private_root=private_root,
        timeout_seconds=max(config.timeout_seconds, 900),
    )
    return {"command": command, "duration_seconds": round(duration, 6), "status": "passed"}


def _workflow_inputs_command(
    config: CaseConfig,
    private_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    output = private_root / f"workflow-input-{uuid4().hex}.json"
    _, duration = _protected_run(
        (
            sys.executable,
            "-I",
            str(ENVIRONMENT_CONTROL),
            "--state-root",
            str(config.state_root),
            "--package-root",
            str(PACKAGE_ROOT),
            "workflow-input",
            "--seed",
            config.seed,
            "--output",
            str(output),
        ),
        private_root=private_root,
        timeout_seconds=max(config.timeout_seconds, 900),
    )
    try:
        value = json.loads(_read_private_bytes(output))
    except (AcceptanceCaseError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcceptanceCaseError("workflow_input_rejected") from error
    records = value.get("records") if isinstance(value, Mapping) else None
    if (
        not isinstance(value, Mapping)
        or set(value) != {"records", "run_label"}
        or value.get("run_label") != "formal"
        or not isinstance(records, list)
        or not 0 < len(records) <= MAX_TRACE_IDENTITIES
    ):
        raise AcceptanceCaseError("workflow_input_rejected")
    projected: list[dict[str, Any]] = []
    record_ids: set[str] = set()
    required_fields = set(WORKFLOW_RECORD_FIELDS)
    for record in records:
        if not isinstance(record, Mapping) or set(record) != required_fields:
            raise AcceptanceCaseError("workflow_input_rejected")
        required_strings = (
            record.get(field)
            for field in (
                "record_id",
                "source_id",
                "supplier",
                "part_number",
            )
        )
        if any(
            not isinstance(item, str)
            or not item
            or len(item) > 4_000
            or "\x00" in item
            for item in required_strings
        ):
            raise AcceptanceCaseError("workflow_input_rejected")
        for field in ("lot_number", "document_date", "certificate_type"):
            item = record.get(field)
            if item is not None and (
                not isinstance(item, str)
                or not item
                or len(item) > 4_000
                or "\x00" in item
            ):
                raise AcceptanceCaseError("workflow_input_rejected")
        purchase_order = record.get("purchase_order")
        if purchase_order is not None and (
            not isinstance(purchase_order, str)
            or not purchase_order
            or len(purchase_order) > 4_000
            or "\x00" in purchase_order
        ):
            raise AcceptanceCaseError("workflow_input_rejected")
        quantity = record.get("quantity")
        if quantity is not None and (
            isinstance(quantity, bool)
            or not isinstance(quantity, (int, float))
            or not math.isfinite(quantity)
        ):
            raise AcceptanceCaseError("workflow_input_rejected")
        confidence = record.get("ocr_confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            raise AcceptanceCaseError("workflow_input_rejected")
        record_id = str(record["record_id"])
        if record_id in record_ids:
            raise AcceptanceCaseError("workflow_input_rejected")
        record_ids.add(record_id)
        projected.append({field: record[field] for field in WORKFLOW_RECORD_FIELDS})
    return (
        {"records": projected, "run_label": "formal"},
        {
            "command": "workflow-input",
            "duration_seconds": round(duration, 6),
            "status": "passed",
        },
    )


def _read_host_secret_documents(state_root: Path) -> dict[str, Mapping[str, Any]]:
    documents: dict[str, Mapping[str, Any]] = {}
    for filename in {item[0] for item in HOST_SECRET_NAMES.values()}:
        payload = _read_private_bytes(state_root / filename)
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AcceptanceCaseError("host_secret_rotation_failed") from error
        if not isinstance(value, Mapping):
            raise AcceptanceCaseError("host_secret_rotation_failed")
        documents[filename] = value
    return documents


def _install_host_secrets(config: CaseConfig, token: str) -> int:
    installed = 0
    try:
        documents = _read_host_secret_documents(config.state_root)
        for public_name, (filename, field) in sorted(HOST_SECRET_NAMES.items()):
            value = documents[filename].get(field)
            if not isinstance(value, str) or not value:
                raise AcceptanceCaseError("host_secret_rotation_failed")
            receipt = _platform_json(
                "POST",
                config,
                token,
                "/api/v1/platform/secrets",
                {
                    "owner_id": "formal-environment",
                    "name": public_name,
                    "value": value,
                    "description": f"{TASK_ID} revision {REVISION} controlled secret",
                },
            )
            if (
                not isinstance(receipt, Mapping)
                or receipt.get("owner_id") != "formal-environment"
                or receipt.get("name") != public_name
                or receipt.get("encrypted") is not True
            ):
                raise AcceptanceCaseError("host_secret_rotation_failed")
            installed += 1
    except BaseException as error:
        raise HostSecretInstallError(installed) from error
    return installed


def conservative_resume_values(run: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return testkit.conservative_human_resume_values(run)
    except (RuntimeError, ValueError, TypeError) as error:
        raise AcceptanceCaseError("human_input_schema_rejected") from error


def _opaque_value_digest(value: Any) -> str:
    try:
        payload = _canonical_json({"value": value})
    except (TypeError, ValueError):
        payload = _canonical_json({"type": type(value).__name__})
    return _digest(payload)


def _public_run_status(value: Any) -> tuple[str, str]:
    digest = _opaque_value_digest(value)
    if isinstance(value, str) and value in {
        "queued",
        "running",
        "paused",
        "succeeded",
        "failed",
        "cancelled",
    }:
        return str(value), digest
    return "unknown", digest


def _execute_workflow(
    config: CaseConfig,
    token: str,
    inputs: Mapping[str, Any],
) -> dict[str, Any]:
    created = _platform_json(
        "POST",
        config,
        token,
        f"/api/v1/applications/{config.application_id}/runs",
        {"inputs": dict(inputs), "version": config.version, "workspace_path": "."},
    )
    if not isinstance(created, Mapping):
        missing_digest = _opaque_value_digest({"state": "unavailable"})
        raise WorkflowExecutionError(
            "workflow_start_rejected",
            {
                "created_response_type": testkit._public_json_type(created),
                "created_response_digest": _opaque_value_digest(created),
                "run_id": None,
                "run_id_digest": missing_digest,
                "observed_version": None,
                "observed_version_digest": missing_digest,
                "created_status": "unknown",
                "created_status_digest": missing_digest,
                "last_status": "unknown",
                "terminal_status": None,
                "resume_count": 0,
                "version": config.version,
                "cancel_attempted": False,
                "cancel_result": "unsafe_run_identity",
            },
        )
    raw_run_id = created.get("run_id")
    run_id_digest = _opaque_value_digest(raw_run_id)
    canonical_run_id: str | None = None
    if isinstance(raw_run_id, str):
        try:
            canonical_run_id = str(UUID(raw_run_id))
        except ValueError:
            pass
    observed_version = created.get("version")
    public_version = (
        observed_version
        if isinstance(observed_version, int) and not isinstance(observed_version, bool)
        else None
    )
    created_status, created_status_digest = _public_run_status(
        created.get("status")
    )
    run_receipt = {
        "run_id": canonical_run_id,
        "run_id_digest": run_id_digest,
        "observed_version": public_version,
        "observed_version_digest": _opaque_value_digest(observed_version),
        "created_status": created_status,
        "created_status_digest": created_status_digest,
        "last_status": created_status,
        "terminal_status": None,
        "resume_count": 0,
        "version": config.version,
        "cancel_attempted": False,
        "cancel_result": "not_required",
    }
    try:
        if canonical_run_id is None or raw_run_id != canonical_run_id:
            raise AcceptanceCaseError("workflow_start_rejected")
        if public_version != config.version:
            raise AcceptanceCaseError("workflow_version_mismatch")
        if created_status not in {"queued", "running"}:
            raise AcceptanceCaseError("workflow_status_rejected")
        deadline = time.monotonic() + config.timeout_seconds
        while time.monotonic() < deadline:
            run = _platform_json(
                "GET", config, token, f"/api/v1/runs/{canonical_run_id}"
            )
            if (
                not isinstance(run, Mapping)
                or run.get("id") not in {None, canonical_run_id}
                or run.get("run_id") not in {None, canonical_run_id}
                or run.get("application_id") != config.application_id
                or run.get("version") != config.version
            ):
                raise AcceptanceCaseError("workflow_identity_rejected")
            status = run.get("status")
            if not isinstance(status, str):
                raise AcceptanceCaseError("workflow_status_rejected")
            public_status, _status_digest = _public_run_status(status)
            run_receipt["last_status"] = public_status
            if status == "paused":
                if run_receipt["resume_count"] >= config.max_resume_count:
                    raise AcceptanceCaseError("human_input_limit_exceeded")
                values = conservative_resume_values(run)
                _platform_json(
                    "POST",
                    config,
                    token,
                    f"/api/v1/runs/{canonical_run_id}/resume",
                    {"values": values},
                )
                run_receipt["resume_count"] += 1
            elif status in {"queued", "running"}:
                time.sleep(0.05)
            elif status in {"succeeded", "failed", "cancelled"}:
                run_receipt["terminal_status"] = status
                return {
                    "run": run,
                    "receipt": run_receipt,
                }
            else:
                raise AcceptanceCaseError("workflow_status_rejected")
        raise AcceptanceCaseError("workflow_timeout")
    except BaseException as error:
        if canonical_run_id is None:
            run_receipt["cancel_result"] = "unsafe_run_identity"
        else:
            run_receipt["cancel_attempted"] = True
            run_receipt["cancel_result"] = "failed"
            try:
                cancelled = _platform_json(
                    "POST",
                    config,
                    token,
                    f"/api/v1/runs/{canonical_run_id}/cancel",
                )
                if (
                    isinstance(cancelled, Mapping)
                    and cancelled.get("run_id") == canonical_run_id
                    and cancelled.get("status") in {"cancelling", "cancelled"}
                ):
                    run_receipt["cancel_result"] = str(cancelled["status"])
            except BaseException:
                pass
        reason = (
            error.reason
            if isinstance(error, AcceptanceCaseError)
            else "acceptance_case_failed"
        )
        raise WorkflowExecutionError(
            reason,
            run_receipt,
        ) from error


def _artifact_binding(
    value: Mapping[str, Any],
    *,
    run_id: str,
    require_filename: bool,
) -> tuple[str, str, str, int, str, bool]:
    relative_path = value.get("relative_path")
    path = PurePosixPath(relative_path) if isinstance(relative_path, str) else None
    if (
        path is None
        or path.is_absolute()
        or path.parts[:3] != (".workflow-run-artifacts", run_id, "artifacts")
        or len(path.parts) != 4
    ):
        raise AcceptanceCaseError("artifact_projection_rejected")
    filename = path.name
    if require_filename and value.get("filename") != filename:
        raise AcceptanceCaseError("artifact_projection_rejected")
    media_type = value.get("media_type")
    size_bytes = value.get("size_bytes")
    digest = value.get("sha256")
    replayed = value.get("replayed")
    if (
        filename not in REQUIRED_ARTIFACTS
        or media_type != REQUIRED_ARTIFACTS[filename]
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or not 0 < size_bytes <= 128 * 1024 * 1024
        or not isinstance(digest, str)
        or SHA256_PATTERN.fullmatch(digest) is None
        or not isinstance(replayed, bool)
    ):
        raise AcceptanceCaseError("artifact_projection_rejected")
    return filename, relative_path, media_type, size_bytes, digest, replayed


def _output_artifact_bindings(
    outputs: Any,
    *,
    run_id: str,
) -> set[tuple[str, str, str, int, str, bool]]:
    bindings: set[tuple[str, str, str, int, str, bool]] = set()
    pending: list[tuple[Any, int]] = [(outputs, 0)]
    visited = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if visited > 100_000 or depth > 100:
            raise AcceptanceCaseError("artifact_projection_rejected")
        if isinstance(current, Mapping):
            artifact_keys = {
                "relative_path",
                "filename",
                "media_type",
                "size_bytes",
                "sha256",
                "replayed",
            }
            if artifact_keys <= set(current):
                bindings.add(
                    _artifact_binding(
                        current,
                        run_id=run_id,
                        require_filename=True,
                    )
                )
            pending.extend((value, depth + 1) for value in current.values())
        elif isinstance(current, list):
            pending.extend((value, depth + 1) for value in current)
    return bindings


def _trace_projection(
    config: CaseConfig,
    token: str,
    run_id: str,
    outputs: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    events = _platform_json("GET", config, token, f"/v1/streams/{run_id}")
    if not isinstance(events, list) or len(events) > MAX_TRACE_EVENTS:
        raise AcceptanceCaseError("trace_projection_rejected")
    identities: Counter[tuple[str, str | None]] = Counter()
    trace_artifacts: dict[
        tuple[str, str, str, int, str, bool], dict[str, Any]
    ] = {}
    for event in events:
        if not isinstance(event, Mapping):
            raise AcceptanceCaseError("trace_projection_rejected")
        event_type = event.get("type")
        data = event.get("data")
        node_id = data.get("node_id") if isinstance(data, Mapping) else None
        node_id_digest = None
        if (
            not isinstance(event_type, str)
            or SAFE_ID_PATTERN.fullmatch(event_type) is None
            or (
                node_id is not None
                and (
                    not isinstance(node_id, str)
                    or not node_id
                    or len(node_id.encode("utf-8")) > 1_024
                )
            )
        ):
            raise AcceptanceCaseError("trace_projection_rejected")
        if isinstance(node_id, str):
            node_id_digest = _digest(node_id.encode("utf-8"))
        identities[(event_type, node_id_digest)] += 1
        if event_type == "artifact.created":
            if not isinstance(data, Mapping) or node_id_digest is None:
                raise AcceptanceCaseError("artifact_projection_rejected")
            binding = _artifact_binding(
                data,
                run_id=run_id,
                require_filename=False,
            )
            if binding in trace_artifacts:
                raise AcceptanceCaseError("artifact_projection_rejected")
            trace_artifacts[binding] = {
                "filename": binding[0],
                "media_type": binding[2],
                "size_bytes": binding[3],
                "sha256": binding[4],
                "replayed": binding[5],
                "node_id_digest": node_id_digest,
            }
    projection = [
        {
            "event_type": event_type,
            "node_id_digest": node_id_digest,
            "count": count,
        }
        for (event_type, node_id_digest), count in sorted(
            identities.items(), key=lambda item: (item[0][0], item[0][1] or "")
        )
    ]
    if len(projection) > MAX_TRACE_IDENTITIES:
        raise AcceptanceCaseError("trace_projection_rejected")
    output_artifacts = _output_artifact_bindings(outputs, run_id=run_id)
    if (
        set(trace_artifacts) != output_artifacts
        or {item[0] for item in trace_artifacts} != set(REQUIRED_ARTIFACTS)
    ):
        raise AcceptanceCaseError("artifact_trace_binding_failed")
    trace_receipt = {
        "event_count": len(events),
        "identity_count": len(projection),
        "identities": projection,
        "identity_digest": _digest(_canonical_json(projection)),
    }
    artifacts = [trace_artifacts[key] for key in sorted(trace_artifacts)]
    return trace_receipt, artifacts


def _run_host_verifier(config: CaseConfig, private_root: Path) -> dict[str, Any]:
    snapshot_path = config.state_root / f"host-snapshot-{config.seed}-final.json"
    output_path = private_root / "host-verification-result.json"
    returncode, duration = _protected_run(
        (
            sys.executable,
            "-I",
            str(HOST_VERIFIER),
            "--snapshot",
            str(snapshot_path),
            "--oracle",
            str(HOST_ORACLE),
            "--output",
            str(output_path),
        ),
        private_root=private_root,
        allowed_returncodes=frozenset({0, 3}),
        timeout_seconds=config.timeout_seconds,
    )
    payload = _read_private_bytes(output_path, max_bytes=32 * 1024 * 1024)
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AcceptanceCaseError("host_verifier_output_rejected") from error
    differences = result.get("differences") if isinstance(result, Mapping) else None
    integer_fields = (
        "check_count",
        "passed_check_count",
        "record_binding_gate_count",
        "fault_gate_count",
    )
    if (
        not isinstance(result, Mapping)
        or result.get("task_id") != TASK_ID
        or result.get("revision") != REVISION
        or str(result.get("seed")) != config.seed
        or result.get("verdict") not in {"independently_verified", "verification_failed"}
        or returncode != (0 if result.get("verdict") == "independently_verified" else 3)
        or not isinstance(differences, list)
        or any(
            isinstance(result.get(field), bool)
            or not isinstance(result.get(field), int)
            or result[field] < 0
            for field in integer_fields
        )
        or not isinstance(result.get("snapshot_digest"), str)
        or SHA256_PATTERN.fullmatch(result["snapshot_digest"]) is None
        or not isinstance(result.get("oracle_digest"), str)
        or SHA256_PATTERN.fullmatch(result["oracle_digest"]) is None
    ):
        raise AcceptanceCaseError("host_verifier_output_rejected")
    check_count = result["check_count"]
    passed_check_count = result["passed_check_count"]
    difference_count = len(differences)
    passed = result["verdict"] == "independently_verified"
    if (
        check_count <= 0
        or passed_check_count > check_count
        or (
            passed
            and (
                passed_check_count != check_count
                or difference_count != 0
                or returncode != 0
                or result["record_binding_gate_count"] <= 0
                or result["fault_gate_count"] <= 0
            )
        )
        or (
            not passed
            and (
                passed_check_count >= check_count
                or difference_count == 0
                or returncode != 3
            )
        )
    ):
        raise AcceptanceCaseError("host_verifier_output_rejected")
    return {
        "verdict": result["verdict"],
        "check_count": check_count,
        "passed_check_count": passed_check_count,
        "difference_count": difference_count,
        "record_binding_gate_count": result["record_binding_gate_count"],
        "fault_gate_count": result["fault_gate_count"],
        "snapshot_digest": result["snapshot_digest"],
        "oracle_digest": result["oracle_digest"],
        "result_digest": _digest(payload),
        "duration_seconds": round(duration, 6),
    }


def _failure_reason(error: BaseException) -> str:
    if isinstance(error, AcceptanceCaseError):
        return error.reason
    return "acceptance_case_failed"


def execute_case(
    config: CaseConfig,
    *,
    platform_token: str,
    signing_key: bytes,
) -> dict[str, Any]:
    """Execute one case and return a signed receipt without protected values."""

    _validate_config(config)
    _ensure_private_directory(config.state_root)
    case_id = str(uuid4())
    started_at = _utc_now()
    before_guard: dict[str, Any] | None = None
    after_guard: dict[str, Any] | None = None
    before_handoff: dict[str, Any] | None = None
    after_handoff: dict[str, Any] | None = None
    run_receipt: dict[str, Any] | None = None
    trace_receipt: dict[str, Any] | None = None
    artifacts: list[dict[str, Any]] = []
    host_verification: dict[str, Any] | None = None
    command_receipts: list[dict[str, Any]] = []
    installed_secret_count = 0
    failure: BaseException | None = None
    host_lifecycle_started = False
    cleanup_status = "not_required"
    with tempfile.TemporaryDirectory(
        prefix=".acceptance-private-", dir=config.state_root
    ) as private_name:
        private_root = Path(private_name)
        try:
            before_guard = _application_guard(config, platform_token)
            before_handoff = _builder_handoff_guard(config, platform_token)
            host_lifecycle_started = True
            command_receipts.append(
                _environment_command(
                    config,
                    private_root,
                    "reset",
                    "--confirm-task-id",
                    TASK_ID,
                )
            )
            command_receipts.append(
                _environment_command(config, private_root, "up")
            )
            command_receipts.append(
                _environment_command(config, private_root, "initialize")
            )
            command_receipts.append(
                _environment_command(
                    config, private_root, "seed", "--seed", config.seed
                )
            )
            workflow_inputs, workflow_input_receipt = _workflow_inputs_command(
                config,
                private_root,
            )
            command_receipts.append(workflow_input_receipt)
            command_receipts.append(
                _environment_command(
                    config,
                    private_root,
                    "snapshot",
                    "--seed",
                    config.seed,
                    "--phase",
                    "baseline",
                )
            )
            try:
                installed_secret_count = _install_host_secrets(
                    config, platform_token
                )
            except HostSecretInstallError as error:
                installed_secret_count = error.installed_count
                raise
            try:
                outcome = _execute_workflow(
                    config,
                    platform_token,
                    workflow_inputs,
                )
            except WorkflowExecutionError as error:
                run_receipt = error.run_receipt
                raise
            run = outcome["run"]
            run_receipt = outcome["receipt"]
            evidence_failure: BaseException | None = None
            try:
                trace_receipt, artifacts = _trace_projection(
                    config,
                    platform_token,
                    run_receipt["run_id"],
                    run.get("outputs"),
                )
            except BaseException as error:
                evidence_failure = error
            command_receipts.append(
                _environment_command(
                    config,
                    private_root,
                    "snapshot",
                    "--seed",
                    config.seed,
                    "--phase",
                    "final",
                )
            )
            host_verification = _run_host_verifier(config, private_root)
            after_guard = _application_guard(config, platform_token)
            after_handoff = _builder_handoff_guard(config, platform_token)
            if after_guard != before_guard:
                raise AcceptanceCaseError("published_version_changed")
            if after_handoff != before_handoff:
                raise AcceptanceCaseError("builder_handoff_changed")
            if run_receipt["terminal_status"] != "succeeded":
                raise AcceptanceCaseError("workflow_run_failed")
            if host_verification["verdict"] != "independently_verified":
                raise AcceptanceCaseError("host_verification_failed")
            if evidence_failure is not None:
                raise evidence_failure
        except BaseException as error:
            failure = error
            if host_lifecycle_started and after_guard is None:
                try:
                    after_guard = _application_guard(config, platform_token)
                    if before_guard is not None and after_guard != before_guard:
                        failure = AcceptanceCaseError("published_version_changed")
                except BaseException:
                    pass
            if host_lifecycle_started and after_handoff is None:
                try:
                    after_handoff = _builder_handoff_guard(
                        config, platform_token
                    )
                    if (
                        before_handoff is not None
                        and after_handoff != before_handoff
                    ):
                        failure = AcceptanceCaseError("builder_handoff_changed")
                except BaseException:
                    pass
        finally:
            if host_lifecycle_started:
                try:
                    command_receipts.append(
                        _environment_command(config, private_root, "down")
                    )
                    cleanup_status = "passed"
                except BaseException:
                    cleanup_status = "failed"
                    if failure is None:
                        failure = AcceptanceCaseError("environment_cleanup_failed")

    status = "passed" if failure is None and cleanup_status == "passed" else "failed"
    unsigned = {
        "schema_version": "v0.4.13-t01h-exp001-acceptance-case-2",
        "task_id": TASK_ID,
        "revision": REVISION,
        "case_id": case_id,
        "seed": config.seed,
        "status": status,
        "reason": "acceptance_case_passed" if status == "passed" else _failure_reason(failure or RuntimeError()),
        "application_id": config.application_id,
        "builder_assignment_id": config.assignment_id,
        "builder_session_id": config.session_id,
        "version": config.version,
        "published_content_hash": config.content_hash,
        "before_guard": before_guard,
        "after_guard": after_guard,
        "before_builder_handoff": before_handoff,
        "after_builder_handoff": after_handoff,
        "run": run_receipt,
        "trace": trace_receipt,
        "artifacts": artifacts,
        "host_verification": host_verification,
        "host_secret_install_count": installed_secret_count,
        "environment_commands": command_receipts,
        "cleanup_status": cleanup_status,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "claim": "task_author_receipt_tamper_evidence_only",
    }
    return sign_receipt(unsigned, signing_key)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one opaque EXP-LILIES-001 acceptance case."
    )
    parser.add_argument("--platform-url", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--receipt-key-file", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--builder-assignment-id", required=True)
    parser.add_argument("--builder-session-id", required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--content-hash", required=True)
    parser.add_argument("--seed", choices=SEEDS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--max-resume-count", type=int, default=MAX_RESUMES)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    signing_key: bytes | None = None
    try:
        signing_key = _receipt_signing_key(args.receipt_key_file)
        token = _platform_token(args.token_file)
        config = CaseConfig(
            platform_url=args.platform_url,
            state_root=args.state_root.resolve(),
            application_id=args.application_id,
            assignment_id=args.builder_assignment_id,
            session_id=args.builder_session_id,
            version=args.version,
            content_hash=args.content_hash,
            seed=args.seed,
            timeout_seconds=args.timeout_seconds,
            max_resume_count=args.max_resume_count,
        )
        receipt = execute_case(
            config,
            platform_token=token,
            signing_key=signing_key,
        )
        _write_private_json(args.output, receipt)
        summary = {
            "status": receipt["status"],
            "reason": receipt["reason"],
            "receipt_digest": receipt["receipt_digest"],
        }
        print(json.dumps(summary, separators=(",", ":"), sort_keys=True))
        return 0 if receipt["status"] == "passed" else 3
    except BaseException as error:
        if signing_key is not None:
            try:
                unsigned = {
                    "schema_version": "v0.4.13-t01h-exp001-acceptance-case-2",
                    "task_id": TASK_ID,
                    "revision": REVISION,
                    "case_id": str(uuid4()),
                    "seed": args.seed,
                    "status": "failed",
                    "reason": _failure_reason(error),
                    "application_id": None,
                    "builder_assignment_id": None,
                    "builder_session_id": None,
                    "version": None,
                    "published_content_hash": None,
                    "before_guard": None,
                    "after_guard": None,
                    "before_builder_handoff": None,
                    "after_builder_handoff": None,
                    "run": None,
                    "trace": None,
                    "artifacts": [],
                    "host_verification": None,
                    "host_secret_install_count": 0,
                    "environment_commands": [],
                    "cleanup_status": "not_required",
                    "started_at": _utc_now(),
                    "finished_at": _utc_now(),
                    "claim": "task_author_receipt_tamper_evidence_only",
                }
                receipt = sign_receipt(unsigned, signing_key)
                _write_private_json(args.output, receipt)
                print(
                    json.dumps(
                        {
                            "status": "failed",
                            "reason": receipt["reason"],
                            "receipt_digest": receipt["receipt_digest"],
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                return 3
            except BaseException:
                pass
        print(
            json.dumps(
                {"status": "failed", "reason": "acceptance_adapter_rejected"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
