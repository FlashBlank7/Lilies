from __future__ import annotations

# ruff: noqa: E402

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
from uuid import NAMESPACE_URL, UUID, uuid5

import uvicorn


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
for import_root in (ROOT, BACKEND_SRC):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from agent_platform.config import Settings
from agent_platform.external_builder_bootstrap import (
    ExternalBuilderBootstrapError,
    ExternalBuilderBootstrapRequest,
    bootstrap_external_builder_async,
)
from agent_platform import formal_source_provenance
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition, Usage
from agent_platform.providers.base import (
    ModelProvider,
    ProviderCapabilities,
    ProviderError,
)
from scripts import run_v04_13_enterprise_experiment as enterprise_runner


TASK_ID = enterprise_runner.TASK_ID
REVISION = enterprise_runner.REVISION
ENVIRONMENT_CONTROL = enterprise_runner.ENVIRONMENT_CONTROL
TASK_ROOT = enterprise_runner.TASK_ROOT
DEFAULT_PLATFORM_PORT = enterprise_runner.DEFAULT_PLATFORM_PORT
DEFAULT_OWNER_UI_URL = "http://127.0.0.1:3000"
CODEX_CHILD = ROOT / "scripts" / "run_v04_13_codex_builder_child.py"
MAX_CODEX_ROLLOUT_TOKEN_LIMIT = 1_000_000
CODEX_ROLLOUT_TOKEN_LIMIT = 1_000_000
MAX_COLLABORATION_REPLAY_MESSAGES = 50_000
EXTERNAL_CODEX_SPEND_DISABLED_FILENAME = "EXTERNAL_CODEX_SPEND_DISABLED"
BUILDER_BOOTSTRAP_DISABLED_FILENAME = "BUILDER_BOOTSTRAP_DISABLED"
ENVIRONMENT_CONTROL_OWNER_FILENAME = ".codex-builder-control-owner.json"
ENVIRONMENT_COMPOSE_PROJECT = "exp-lilies-001-r7"
ENVIRONMENT_REVISION_HISTORY_DIRNAME = "revision-history"
ENVIRONMENT_ADOPTION_SCHEMA_VERSION = (
    "v0.4.13-environment-revision-adoption-1"
)
AUTHORITY_TRANSITION_SCHEMA_VERSION = (
    "v0.4.13-codex-resume-authority-transition-1"
)
PRE_PROVIDER_RECONCILIATION_SCHEMA_VERSION = (
    "v0.4.13-codex-pre-provider-reconciliation-1"
)
INDETERMINATE_PROVIDER_RETRY_SCHEMA_VERSION = (
    "v0.4.13-codex-indeterminate-provider-retry-1"
)
AUTHORITY_GUARD_PREFLIGHT_ERROR = (
    b"isolated Codex Builder child failed: CodexBuilderChildError: "
    b"resume handoff changed; a new authority cannot reuse the Builder thread\n"
)
PREDECESSOR_RETIREMENT_REASON = (
    "retired by verified immediate successor project revision advance"
)
REPAIR_PHASES = (
    "builder_report",
    "development_enablement",
    "same_project_rerun",
)
RepairPhase = Literal[
    "builder_report",
    "development_enablement",
    "same_project_rerun",
]
SAFE_BOOTSTRAP_FIELDS = frozenset(
    {
        "schema_version",
        "builder_actor",
        "task_id",
        "revision",
        "run_id",
        "assignment_id",
        "application_id",
        "build_id",
        "session_id",
        "connection_id",
        "environment_instance_id",
        "channel_id",
        "task_credential_ref",
        "collaboration_credential_ref",
        "contract_digest",
        "assignment_bundle_digest",
        "workspace_manifest_digest",
        "workspace_policy_digest",
        "expires_at",
        "handoff_path",
        "handoff_digest",
        "formal_archive_supported",
    }
)


class CodexBuilderRunnerError(RuntimeError):
    """The isolated Codex Builder environment could not be prepared safely."""


class CodexBuilderChildExitError(CodexBuilderRunnerError):
    """Child wrapper started but exited without a trusted process receipt."""

    def __init__(self, message: str, *, wrapper_exit_code: int) -> None:
        super().__init__(message)
        self.accounting_result = {
            "accounting_evidence_level": "child_wrapper_attempt_only",
            "child_wrapper_exit_code": wrapper_exit_code,
            "process_execution_status": "failed_before_receipt",
            "transcript_digest": None,
            "usage_accounting": {
                **_usage_accounting(None),
                "model_call_count": 1,
                "model_call_count_support": ("attempted_unknown_from_child_wrapper_process"),
                "unknown_usage_model_calls": 1,
            },
            "formal_archive_supported": True,
        }


def _assert_builder_bootstrap_enabled(state_root: Path) -> None:
    disabled_path = (
        state_root.resolve() / BUILDER_BOOTSTRAP_DISABLED_FILENAME
    )
    if disabled_path.exists() or disabled_path.is_symlink():
        raise CodexBuilderRunnerError(
            "Builder bootstrap is disabled for this state root by "
            f"{disabled_path}; there is no command-line override"
        )


def _assert_external_codex_spend_enabled(state_root: Path) -> None:
    spend_disabled_path = state_root.resolve() / EXTERNAL_CODEX_SPEND_DISABLED_FILENAME
    if spend_disabled_path.exists() or spend_disabled_path.is_symlink():
        raise CodexBuilderRunnerError(
            "external Codex spending is disabled for this state root by "
            f"{spend_disabled_path}; there is no command-line override"
        )


def _environment_state_root(
    state_root: Path,
    configured: Path | None,
) -> Path:
    candidate = (
        state_root / "environment"
        if configured is None
        else configured.expanduser()
    )
    if candidate.is_symlink():
        raise CodexBuilderRunnerError(
            "environment state root must not be a symlink"
        )
    resolved = candidate.resolve()
    if resolved.name != "environment":
        raise CodexBuilderRunnerError(
            "environment state root must identify the controlled environment directory"
        )
    return resolved


def _environment_command(
    environment_state_root: Path,
    *arguments: str,
    inherited_environment: Mapping[str, str],
) -> None:
    enterprise_runner._run_checked(
        (
            sys.executable,
            str(ENVIRONMENT_CONTROL),
            "--state-root",
            str(environment_state_root),
            "--package-root",
            str(TASK_ROOT),
            *arguments,
        ),
        environment=inherited_environment,
    )


def _sha256_digest(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _read_private_regular_bytes(
    path: Path,
    *,
    allowed_modes: frozenset[int],
    label: str,
) -> bytes:
    if path.is_symlink():
        raise CodexBuilderRunnerError(f"{label} must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CodexBuilderRunnerError(f"{label} is unavailable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) not in allowed_modes
            or metadata.st_nlink != 1
        ):
            raise CodexBuilderRunnerError(
                f"{label} must be a private regular file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_json_object(payload: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexBuilderRunnerError(f"{label} is invalid JSON") from error
    if not isinstance(value, dict):
        raise CodexBuilderRunnerError(f"{label} must be a JSON object")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_private_bytes(path: Path, payload: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o400)
    except FileExistsError:
        existing = _read_private_regular_bytes(
            path,
            allowed_modes=frozenset({0o400}),
            label=f"immutable revision history file {path.name}",
        )
        if not hmac.compare_digest(
            hashlib.sha256(existing).digest(),
            hashlib.sha256(payload).digest(),
        ):
            raise CodexBuilderRunnerError(
                f"immutable revision history conflicts: {path.name}"
            )
        return
    try:
        os.fchmod(descriptor, 0o400)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise CodexBuilderRunnerError(
                    f"immutable revision history write failed: {path.name}"
                )
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _atomic_private_bytes(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise CodexBuilderRunnerError(
            f"environment identity must not be a symlink: {path.name}"
        )
    if path.exists():
        _read_private_regular_bytes(
            path,
            allowed_modes=frozenset({0o600}),
            label=f"environment identity {path.name}",
        )
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.monotonic_ns()}"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise CodexBuilderRunnerError(
                    f"environment identity write failed: {path.name}"
                )
            view = view[written:]
        os.fsync(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _public_inputs_tree_digest(package_root: Path) -> str:
    root = package_root / "fixtures" / "public-inputs"
    if (
        root.is_symlink()
        or not root.is_dir()
        or stat.S_IMODE(root.stat().st_mode) & 0o022
    ):
        raise CodexBuilderRunnerError(
            "public input tree root is unsafe"
        )
    entries: list[dict[str, Any]] = []
    for directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(directory)
        current_metadata = current.lstat()
        if (
            stat.S_ISLNK(current_metadata.st_mode)
            or not stat.S_ISDIR(current_metadata.st_mode)
            or stat.S_IMODE(current_metadata.st_mode) & 0o022
        ):
            raise CodexBuilderRunnerError(
                "public input tree contains an unsafe directory"
            )
        directory_names.sort()
        file_names.sort()
        for name in [*directory_names, *file_names]:
            candidate = current / name
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise CodexBuilderRunnerError(
                    "public input tree must not contain symlinks"
                )
            if stat.S_IMODE(metadata.st_mode) & 0o022:
                raise CodexBuilderRunnerError(
                    "public input tree contains a writable entry"
                )
        for name in file_names:
            candidate = current / name
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise CodexBuilderRunnerError(
                    "public input tree contains a non-regular file"
                )
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(candidate, flags)
            try:
                live = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(live.st_mode)
                    or live.st_dev != metadata.st_dev
                    or live.st_ino != metadata.st_ino
                    or live.st_size != metadata.st_size
                    or stat.S_IMODE(live.st_mode)
                    != stat.S_IMODE(metadata.st_mode)
                ):
                    raise CodexBuilderRunnerError(
                        "public input tree changed during digest"
                    )
                digest = hashlib.sha256()
                observed_size = 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    observed_size += len(chunk)
                    digest.update(chunk)
            finally:
                os.close(descriptor)
            if observed_size != metadata.st_size:
                raise CodexBuilderRunnerError(
                    "public input tree changed during digest"
                )
            entries.append(
                {
                    "path": candidate.relative_to(root).as_posix(),
                    "size": observed_size,
                    "sha256": digest.hexdigest(),
                }
            )
    if not entries:
        raise CodexBuilderRunnerError("public input tree is empty")
    return _sha256_digest(enterprise_runner._canonical_json(entries))


def _validate_environment_identity(
    value: Mapping[str, Any],
    *,
    seed: str,
    revision: int,
    phase: str | None,
    label: str,
) -> None:
    records = value.get("records")
    record_count = value.get("record_count")
    phase_invalid = (
        value.get("phase") != phase
        if phase is not None
        else "phase" in value
    )
    if (
        value.get("schema_version") != "1.0"
        or value.get("task_id") != TASK_ID
        or value.get("revision") != revision
        or value.get("seed") != seed
        or phase_invalid
        or type(record_count) is not int
        or record_count < 1
        or not isinstance(records, list)
        or len(records) != record_count
        or any(not isinstance(item, dict) for item in records)
    ):
        raise CodexBuilderRunnerError(f"{label} identity is invalid")


def _project_environment_identity(
    payload: bytes,
    *,
    seed: str,
    predecessor_revision: int,
    phase: str | None,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    predecessor = _decode_json_object(payload, label=label)
    _validate_environment_identity(
        predecessor,
        seed=seed,
        revision=predecessor_revision,
        phase=phase,
        label=label,
    )
    projected = dict(predecessor)
    projected["revision"] = REVISION
    projected_payload = enterprise_runner._canonical_json(projected)
    return predecessor, projected_payload


def _adopt_environment_revision(
    environment_state_root: Path,
    *,
    seed: str,
    current_package_root: Path = TASK_ROOT,
) -> dict[str, Any]:
    predecessor_revision = REVISION - 1
    if predecessor_revision < 1:
        raise CodexBuilderRunnerError(
            "environment revision adoption requires a predecessor"
        )
    if (
        current_package_root.is_symlink()
        or current_package_root.name != str(REVISION)
        or not current_package_root.is_dir()
    ):
        raise CodexBuilderRunnerError(
            "current package revision root is unsafe"
        )
    predecessor_package_root = (
        current_package_root.parent / str(predecessor_revision)
    )
    if (
        predecessor_package_root.is_symlink()
        or not predecessor_package_root.is_dir()
    ):
        raise CodexBuilderRunnerError(
            "predecessor package revision root is unsafe"
        )
    predecessor_tree_digest = _public_inputs_tree_digest(
        predecessor_package_root
    )
    current_tree_digest = _public_inputs_tree_digest(current_package_root)
    if not hmac.compare_digest(
        predecessor_tree_digest,
        current_tree_digest,
    ):
        raise CodexBuilderRunnerError(
            "successor public input tree differs from its predecessor"
        )

    identities = (
        (f"seed-receipts-{seed}.json", None),
        (f"host-snapshot-{seed}-baseline.json", "baseline"),
    )
    current_payloads: dict[str, bytes] = {}
    current_values: dict[str, dict[str, Any]] = {}
    for filename, _phase in identities:
        path = environment_state_root / filename
        payload = _read_private_regular_bytes(
            path,
            allowed_modes=frozenset({0o600}),
            label=f"environment identity {filename}",
        )
        value = _decode_json_object(
            payload,
            label=f"environment identity {filename}",
        )
        if value.get("revision") not in {
            predecessor_revision,
            REVISION,
        }:
            raise CodexBuilderRunnerError(
                f"environment identity {filename} has an invalid revision"
            )
        current_payloads[filename] = payload
        current_values[filename] = value

    transition_root = (
        environment_state_root
        / ENVIRONMENT_REVISION_HISTORY_DIRNAME
        / f"r{predecessor_revision}-to-r{REVISION}-seed-{seed}"
    )
    history_root = transition_root.parent
    history_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        history_root.is_symlink()
        or stat.S_IMODE(history_root.stat().st_mode) != 0o700
    ):
        raise CodexBuilderRunnerError(
            "environment revision history root is unsafe"
        )
    if not transition_root.exists():
        transition_root.mkdir(mode=0o700)
    if transition_root.is_symlink() or not transition_root.is_dir():
        raise CodexBuilderRunnerError(
            "environment revision transition root is unsafe"
        )
    transition_mode = stat.S_IMODE(transition_root.stat().st_mode)
    if transition_mode not in {0o700, 0o500}:
        raise CodexBuilderRunnerError(
            "environment revision transition root has an unsafe mode"
        )
    allowed_transition_files = {
        *(filename for filename, _phase in identities),
        "adoption.json",
    }
    if any(
        child.name not in allowed_transition_files
        or child.is_symlink()
        or not child.is_file()
        for child in transition_root.iterdir()
    ):
        raise CodexBuilderRunnerError(
            "environment revision transition contains an unexpected entry"
        )

    archive_payloads: dict[str, bytes] = {}
    projected_payloads: dict[str, bytes] = {}
    projected_values: dict[str, dict[str, Any]] = {}
    for filename, phase in identities:
        archive_path = transition_root / filename
        if archive_path.exists() or archive_path.is_symlink():
            archived = _read_private_regular_bytes(
                archive_path,
                allowed_modes=frozenset({0o400}),
                label=f"archived environment identity {filename}",
            )
        else:
            if transition_mode != 0o700:
                raise CodexBuilderRunnerError(
                    "immutable environment revision history is incomplete"
                )
            if current_values[filename].get("revision") != predecessor_revision:
                raise CodexBuilderRunnerError(
                    "predecessor environment bytes are unavailable for archive"
                )
            archived = current_payloads[filename]
            _write_immutable_private_bytes(archive_path, archived)
        predecessor, projected = _project_environment_identity(
            archived,
            seed=seed,
            predecessor_revision=predecessor_revision,
            phase=phase,
            label=f"archived environment identity {filename}",
        )
        archive_payloads[filename] = archived
        projected_payloads[filename] = projected
        projected_values[filename] = _decode_json_object(
            projected,
            label=f"projected environment identity {filename}",
        )
        current_revision = current_values[filename].get("revision")
        if current_revision == predecessor_revision:
            if not hmac.compare_digest(
                hashlib.sha256(current_payloads[filename]).digest(),
                hashlib.sha256(archived).digest(),
            ):
                raise CodexBuilderRunnerError(
                    "current predecessor identity differs from immutable history"
                )
        else:
            _validate_environment_identity(
                current_values[filename],
                seed=seed,
                revision=REVISION,
                phase=phase,
                label=f"current environment identity {filename}",
            )
            if current_payloads[filename] != projected:
                raise CodexBuilderRunnerError(
                    "current successor identity differs from adoption receipt"
                )
        if predecessor.get("record_count") != projected_values[filename].get(
            "record_count"
        ):
            raise CodexBuilderRunnerError(
                "environment adoption changed the frozen record count"
            )

    receipt_records = archive_payloads[identities[0][0]]
    baseline_records = archive_payloads[identities[1][0]]
    receipt_value = _decode_json_object(
        receipt_records,
        label="archived seed receipt",
    )
    baseline_value = _decode_json_object(
        baseline_records,
        label="archived baseline snapshot",
    )
    receipt_ids = {
        str(item.get("record_id"))
        for item in receipt_value["records"]
    }
    baseline_ids = {
        str(item.get("record_id"))
        for item in baseline_value["records"]
    }
    if (
        receipt_value["record_count"] != baseline_value["record_count"]
        or len(receipt_ids) != receipt_value["record_count"]
        or receipt_ids != baseline_ids
    ):
        raise CodexBuilderRunnerError(
            "predecessor receipt and baseline record identities differ"
        )

    stable_receipt = {
        "schema_version": ENVIRONMENT_ADOPTION_SCHEMA_VERSION,
        "task_id": TASK_ID,
        "seed": seed,
        "predecessor_revision": predecessor_revision,
        "successor_revision": REVISION,
        "record_count": receipt_value["record_count"],
        "predecessor_public_inputs_tree_digest": predecessor_tree_digest,
        "successor_public_inputs_tree_digest": current_tree_digest,
        "archived_identity_digests": {
            filename: _sha256_digest(payload)
            for filename, payload in archive_payloads.items()
        },
        "projected_identity_digests": {
            filename: _sha256_digest(payload)
            for filename, payload in projected_payloads.items()
        },
        "host_mutation_operations": [],
        "adoption_policy": (
            "metadata_revision_projection_only_no_reset_initialize_seed_or_up"
        ),
    }
    adoption_path = transition_root / "adoption.json"
    if adoption_path.exists() or adoption_path.is_symlink():
        persisted_receipt_payload = _read_private_regular_bytes(
            adoption_path,
            allowed_modes=frozenset({0o400}),
            label="environment revision adoption receipt",
        )
        persisted_receipt = _decode_json_object(
            persisted_receipt_payload,
            label="environment revision adoption receipt",
        )
        comparison = dict(persisted_receipt)
        adopted_at = comparison.pop("adopted_at", None)
        if (
            not isinstance(adopted_at, str)
            or comparison != stable_receipt
        ):
            raise CodexBuilderRunnerError(
                "environment revision adoption receipt conflicts"
            )
    else:
        if transition_mode != 0o700:
            raise CodexBuilderRunnerError(
                "immutable environment revision adoption receipt is missing"
            )
        persisted_receipt = {
            **stable_receipt,
            "adopted_at": enterprise_runner._now(),
        }
        _write_immutable_private_bytes(
            adoption_path,
            enterprise_runner._canonical_json(persisted_receipt),
        )
    os.chmod(transition_root, 0o500)
    _fsync_directory(history_root)

    for filename, _phase in identities:
        if current_values[filename].get("revision") == predecessor_revision:
            _atomic_private_bytes(
                environment_state_root / filename,
                projected_payloads[filename],
            )
    for filename, phase in identities:
        projected_current = _read_private_regular_bytes(
            environment_state_root / filename,
            allowed_modes=frozenset({0o600}),
            label=f"projected environment identity {filename}",
        )
        if projected_current != projected_payloads[filename]:
            raise CodexBuilderRunnerError(
                f"projected environment identity digest differs: {filename}"
            )
        _validate_environment_identity(
            _decode_json_object(
                projected_current,
                label=f"projected environment identity {filename}",
            ),
            seed=seed,
            revision=REVISION,
            phase=phase,
            label=f"projected environment identity {filename}",
        )
    return {
        **persisted_receipt,
        "adoption_receipt_path": str(adoption_path),
        "adoption_receipt_digest": _sha256_digest(
            _read_private_regular_bytes(
                adoption_path,
                allowed_modes=frozenset({0o400}),
                label="environment revision adoption receipt",
            )
        ),
    }


def _validate_resumable_environment(
    environment_state_root: Path,
    *,
    seed: str,
) -> None:
    if (
        environment_state_root.is_symlink()
        or not environment_state_root.is_dir()
        or stat.S_IMODE(environment_state_root.stat().st_mode) != 0o700
    ):
        raise CodexBuilderRunnerError(
            "resumed environment state root must be a private regular directory"
        )
    identities = (
        (
            environment_state_root / f"seed-receipts-{seed}.json",
            None,
        ),
        (
            environment_state_root
            / f"host-snapshot-{seed}-baseline.json",
            "baseline",
        ),
    )
    for path, phase in identities:
        try:
            value = enterprise_runner._read_private_json(path)
        except Exception as error:
            raise CodexBuilderRunnerError(
                f"resumed environment identity is unavailable: {path.name}"
            ) from error
        if (
            value.get("schema_version") != "1.0"
            or value.get("task_id") != TASK_ID
            or value.get("revision") != REVISION
            or value.get("seed") != seed
            or (
                phase is not None
                and value.get("phase") != phase
            )
            or type(value.get("record_count")) is not int
            or value["record_count"] < 1
        ):
            raise CodexBuilderRunnerError(
                f"resumed environment identity is invalid: {path.name}"
            )
    try:
        enterprise_runner._host_secrets(environment_state_root.parent)
    except Exception as error:
        raise CodexBuilderRunnerError(
            "resumed environment credentials are unavailable"
        ) from error


def _claim_environment_control(
    environment_state_root: Path,
    *,
    platform_state_root: Path,
    seed: str,
) -> None:
    environment_state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        environment_state_root.is_symlink()
        or stat.S_IMODE(environment_state_root.stat().st_mode) != 0o700
    ):
        raise CodexBuilderRunnerError(
            "environment state root must remain a private regular directory"
        )
    owner_path = (
        environment_state_root / ENVIRONMENT_CONTROL_OWNER_FILENAME
    )
    owner = {
        "schema_version": "v0.4.13-environment-control-owner-1",
        "task_id": TASK_ID,
        "compose_project": ENVIRONMENT_COMPOSE_PROJECT,
        "seed": seed,
        "platform_state_root": str(platform_state_root),
        "environment_state_root": str(environment_state_root),
    }
    payload = enterprise_runner._canonical_json(owner)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(owner_path, flags, 0o600)
    except FileExistsError:
        try:
            existing = enterprise_runner._read_private_json(owner_path)
        except Exception as error:
            raise CodexBuilderRunnerError(
                "environment control owner state is unsafe"
            ) from error
        if existing != owner:
            raise CodexBuilderRunnerError(
                "environment is already controlled by another platform state root"
            )
        return
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise CodexBuilderRunnerError(
                    "environment control owner state could not be written"
                )
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_runner_secrets_for_bootstrap(
    *,
    skip_environment_prepare: bool,
    configured_environment_state_root: Path | None,
    handoff_exists: bool,
) -> bool:
    if not skip_environment_prepare:
        return True
    return (
        configured_environment_state_root is not None
        and not handoff_exists
    )


class _NoModelProvider(ModelProvider):
    """Fail closed if platform code accidentally asks this runner for a model."""

    name = "disabled"

    def capabilities(self, model: str) -> ProviderCapabilities:
        del model
        return ProviderCapabilities(
            thinking=False,
            tools=False,
            parallel_tools=False,
            prompt_caching=False,
            images=False,
            max_context_tokens=1,
            max_output_tokens=1,
        )

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del (
            model,
            system,
            messages,
            tools,
            max_output_tokens,
            thinking_enabled,
            effort,
            tool_choice,
            user_id,
        )
        raise ProviderError(
            "model execution is disabled in the external Codex Builder host runner",
            retryable=False,
        )
        if False:  # pragma: no cover - keeps this an async generator.
            yield StreamEvent(type="message_stop", data={})


def _private_jsonl_append(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise CodexBuilderRunnerError("private event ledger must not be a symlink")
    if path.exists() and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise CodexBuilderRunnerError("private event ledger must have mode 0600")
    line = enterprise_runner._canonical_json(dict(value)) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _lifecycle_path(state_root: Path, seed: str) -> Path:
    return state_root / "observations" / f"codex-builder-{seed}.jsonl"


def _record_lifecycle(
    state_root: Path,
    seed: str,
    event_type: str,
    **fields: Any,
) -> None:
    safe_fields = {
        key: value
        for key, value in fields.items()
        if key
        in {
            "application_id",
            "assignment_id",
            "build_id",
            "session_id",
            "channel_id",
            "handoff_digest",
            "codex_thread_id",
            "transcript_digest",
            "package_public_summary_digest",
            "status",
            "error_code",
        }
    }
    _private_jsonl_append(
        _lifecycle_path(state_root, seed),
        {
            "schema_version": "v0.4.13-t01h-codex-builder-event-1",
            "task_id": TASK_ID,
            "revision": REVISION,
            "seed": seed,
            "event_type": event_type,
            "observed_at": enterprise_runner._now(),
            **safe_fields,
        },
    )


def _safe_bootstrap_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    projection = {key: value[key] for key in sorted(SAFE_BOOTSTRAP_FIELDS) if key in value}
    forbidden = {
        "access_token",
        "authorization",
        "bearer",
        "credential",
        "secret",
        "token",
    }
    if any(
        any(marker in key.casefold() for marker in forbidden)
        and key not in {"task_credential_ref", "collaboration_credential_ref"}
        for key in projection
    ):
        raise CodexBuilderRunnerError("bootstrap owner projection contains authority")
    return projection


def _owner_observation_urls(
    *,
    platform_url: str,
    owner_ui_url: str,
    application_id: str,
    assignment_id: str,
    channel_id: str,
) -> dict[str, str]:
    encoded_application = quote(application_id, safe="")
    encoded_assignment = quote(assignment_id, safe="")
    encoded_channel = quote(channel_id, safe="")
    platform = platform_url.rstrip("/")
    owner_ui = owner_ui_url.rstrip("/")
    return {
        "application_api_url": (f"{platform}/api/v1/applications/{encoded_application}"),
        "application_studio_url": (
            f"{owner_ui}/applications/{encoded_application}?assignment={encoded_assignment}"
        ),
        "collaboration_detail_api_url": (
            f"{platform}/api/v1/studio/collaboration/channels/{encoded_channel}"
        ),
        "collaboration_export_api_url": (
            f"{platform}/api/v1/studio/collaboration/channels/{encoded_channel}/export"
        ),
        "collaboration_event_stream_api_url": (
            f"{platform}/api/v1/studio/collaboration/channels/{encoded_channel}/events?after=0"
        ),
        "collaboration_information_flow_studio_url": (
            f"{owner_ui}/developer/collaboration?channel={encoded_channel}"
        ),
    }


def _bootstrap_ids(seed: str, application_id: UUID) -> dict[str, UUID]:
    prefix = f"lilies:{TASK_ID}:r{REVISION}:seed-{seed}:codex:{application_id}"
    return {
        "assignment_id": uuid5(NAMESPACE_URL, f"{prefix}:assignment"),
        "build_id": uuid5(NAMESPACE_URL, f"{prefix}:build"),
        "session_id": uuid5(NAMESPACE_URL, f"{prefix}:session"),
        "connection_id": uuid5(
            NAMESPACE_URL,
            f"lilies:{TASK_ID}:r{REVISION}:external-builder:codex",
        ),
    }


def _handoff_path(state_root: Path, seed: str) -> Path:
    return state_root / "handoffs" / f"codex-builder-seed-{seed}.json"


def _owner_setup_path(state_root: Path, seed: str) -> Path:
    return state_root / f"codex-builder-owner-setup-seed-{seed}.json"


def _expected_application_name(seed: str) -> str:
    return f"{TASK_ID} · {seed} · workflow pending"


def _write_owner_setup_state(
    state_root: Path,
    *,
    seed: str,
    application: Mapping[str, Any],
    package: Mapping[str, Any],
) -> Path:
    application_id = application.get("id")
    if (
        not isinstance(application_id, str)
        or application.get("name") != _expected_application_name(seed)
    ):
        raise CodexBuilderRunnerError(
            "owner setup application identity is invalid"
        )
    try:
        UUID(application_id)
    except ValueError as error:
        raise CodexBuilderRunnerError(
            "owner setup application id is invalid"
        ) from error
    path = _owner_setup_path(state_root, seed)
    payload = {
        "schema_version": "v0.4.13-external-builder-owner-setup-1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "seed": seed,
        "application_id": application_id,
        "expected_application_name": _expected_application_name(seed),
        "package_public_summary_digest": package.get("public_summary_digest"),
        "package_sealed_digest": package.get("sealed_package_digest"),
        "created_at": enterprise_runner._now(),
    }
    if path.exists() or path.is_symlink():
        existing = enterprise_runner._read_private_json(path)
        comparison = dict(existing)
        comparison.pop("created_at", None)
        expected = dict(payload)
        expected.pop("created_at", None)
        if comparison != expected:
            raise CodexBuilderRunnerError(
                "existing owner setup state conflicts with the current task"
            )
        return path
    enterprise_runner._atomic_private_json(path, payload)
    return path


def _load_owner_setup_application(
    state_root: Path,
    *,
    seed: str,
    package: Mapping[str, Any],
    platform_url: str,
    platform_token: str,
) -> dict[str, Any] | None:
    path = _owner_setup_path(state_root, seed)
    if not path.exists() and not path.is_symlink():
        return None
    value = enterprise_runner._read_private_json(path)
    if (
        value.get("schema_version")
        != "v0.4.13-external-builder-owner-setup-1"
        or value.get("task_id") != TASK_ID
        or value.get("revision") != REVISION
        or value.get("seed") != seed
        or value.get("expected_application_name")
        != _expected_application_name(seed)
        or value.get("package_public_summary_digest")
        != package.get("public_summary_digest")
        or value.get("package_sealed_digest")
        != package.get("sealed_package_digest")
    ):
        raise CodexBuilderRunnerError(
            "existing owner setup state does not match the frozen task"
        )
    application_id = value.get("application_id")
    if not isinstance(application_id, str):
        raise CodexBuilderRunnerError(
            "existing owner setup application id is unavailable"
        )
    try:
        UUID(application_id)
    except ValueError as error:
        raise CodexBuilderRunnerError(
            "existing owner setup application id is invalid"
        ) from error
    application = enterprise_runner._request_json(
        platform_url,
        f"/api/v1/applications/{application_id}",
        token=platform_token,
    )
    if (
        not isinstance(application, dict)
        or application.get("id") != application_id
        or application.get("name") != _expected_application_name(seed)
    ):
        raise CodexBuilderRunnerError(
            "existing owner setup application changed before assignment"
        )
    return application


def _deterministic_task_token_factory(
    private_key: str,
) -> Callable[[UUID], str]:
    key = private_key.encode("utf-8")
    if len(key) < 32:
        raise CodexBuilderRunnerError(
            "external Builder task-token derivation key is invalid"
        )

    def create(credential_id: UUID) -> str:
        digest = hmac.new(
            key,
            (
                "lilies:external-builder-task-token:v1:"
                f"{credential_id.hex}"
            ).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        suffix = (
            base64.urlsafe_b64encode(digest)
            .decode("ascii")
            .rstrip("=")
        )
        return f"lpt_{credential_id.hex}_{suffix}"

    return create


def _bootstrap_request(
    *,
    state_root: Path,
    seed: str,
    application_id: str,
    handoff_path: Path | None = None,
) -> ExternalBuilderBootstrapRequest:
    try:
        application_uuid = UUID(application_id)
    except ValueError as error:
        raise CodexBuilderRunnerError("platform returned a non-UUID application id") from error
    identifiers = _bootstrap_ids(seed, application_uuid)
    target_handoff = (
        handoff_path
        if handoff_path is not None
        else _handoff_path(state_root, seed)
    )
    if not target_handoff.is_absolute():
        raise CodexBuilderRunnerError(
            "external Builder handoff target must be absolute"
        )
    return ExternalBuilderBootstrapRequest(
        task_id=TASK_ID,
        revision=REVISION,
        application_id=application_uuid,
        environment_instance_id=f"{TASK_ID.lower()}:r{REVISION}:seed-{seed}",
        idempotency_key=(
            f"external.codex.{TASK_ID.lower()}.r{REVISION}.seed-{seed}.{application_uuid.hex}"
        ),
        builder_actor="codex",
        handoff_path=target_handoff,
        **identifiers,
    )


def _revision_handoff_path(state_root: Path, seed: str, revision: int) -> Path:
    if type(revision) is not int or revision < 1:
        raise CodexBuilderRunnerError("external Builder task revision is invalid")
    return (
        state_root
        / "handoffs"
        / f"codex-builder-r{revision}-seed-{seed}.json"
    )


def _validated_handoff_payload(
    path: Path,
    *,
    state: Mapping[str, Any],
    bootstrap: Mapping[str, Any],
) -> dict[str, Any]:
    expected_digest = bootstrap.get("handoff_digest")
    if (
        not isinstance(expected_digest, str)
        or not expected_digest.startswith("sha256:")
        or len(expected_digest) != 71
    ):
        raise CodexBuilderRunnerError(
            "existing external Builder handoff digest is unavailable"
        )
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CodexBuilderRunnerError(
            "existing external Builder handoff cannot be read"
        ) from error
    observed_digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
    if not hmac.compare_digest(expected_digest, observed_digest):
        raise CodexBuilderRunnerError(
            "existing external Builder handoff digest does not match owner state"
        )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexBuilderRunnerError(
            "existing external Builder handoff is invalid"
        ) from error
    if not isinstance(value, dict):
        raise CodexBuilderRunnerError(
            "existing external Builder handoff is invalid"
        )
    task = value.get("task")
    assignment = value.get("assignment")
    workspace = value.get("workspace")
    platform = value.get("platform")
    collaboration = value.get("collaboration")
    if any(
        not isinstance(item, dict)
        for item in (task, assignment, workspace, platform, collaboration)
    ):
        raise CodexBuilderRunnerError(
            "existing external Builder handoff binding is incomplete"
        )
    assert isinstance(task, dict)
    assert isinstance(assignment, dict)
    assert isinstance(workspace, dict)
    assert isinstance(platform, dict)
    assert isinstance(collaboration, dict)
    exact_bindings = (
        (value.get("builder_actor"), state.get("builder_actor")),
        (
            value.get("formal_archive_supported"),
            bootstrap.get("formal_archive_supported"),
        ),
        (task.get("task_id"), state.get("task_id")),
        (task.get("revision"), bootstrap.get("revision")),
        (task.get("run_id"), bootstrap.get("run_id")),
        (assignment.get("assignment_id"), bootstrap.get("assignment_id")),
        (assignment.get("application_id"), bootstrap.get("application_id")),
        (assignment.get("build_id"), bootstrap.get("build_id")),
        (assignment.get("session_id"), bootstrap.get("session_id")),
        (assignment.get("connection_id"), bootstrap.get("connection_id")),
        (
            assignment.get("environment_instance_id"),
            bootstrap.get("environment_instance_id"),
        ),
        (
            assignment.get("bundle_digest"),
            bootstrap.get("assignment_bundle_digest"),
        ),
        (
            workspace.get("manifest_digest"),
            bootstrap.get("workspace_manifest_digest"),
        ),
        (
            workspace.get("policy_digest"),
            bootstrap.get("workspace_policy_digest"),
        ),
        (
            platform.get("contract_digest"),
            bootstrap.get("contract_digest"),
        ),
        (
            platform.get("credential_ref"),
            bootstrap.get("task_credential_ref"),
        ),
        (
            collaboration.get("channel_id"),
            bootstrap.get("channel_id"),
        ),
        (
            collaboration.get("credential_ref"),
            bootstrap.get("collaboration_credential_ref"),
        ),
    )
    if any(actual != expected for actual, expected in exact_bindings):
        raise CodexBuilderRunnerError(
            "existing external Builder handoff binding does not match owner state"
        )
    if _parse_utc(
        platform.get("expires_at"),
        label="external Builder handoff expiry",
    ) != _parse_utc(
        bootstrap.get("expires_at"),
        label="external Builder owner-state expiry",
    ):
        raise CodexBuilderRunnerError(
            "existing external Builder handoff expiry does not match owner state"
        )
    return value


def _existing_bootstrap_context(
    state_root: Path,
    seed: str,
) -> tuple[Path, dict[str, Any], dict[str, Any], Path] | None:
    state_path = state_root / f"codex-builder-seed-{seed}.json"
    if not state_path.exists() and not state_path.is_symlink():
        return None
    state = enterprise_runner._read_private_json(state_path)
    bootstrap = state.get("bootstrap")
    if (
        state.get("task_id") != TASK_ID
        or state.get("seed") != seed
        or not isinstance(bootstrap, dict)
    ):
        raise CodexBuilderRunnerError(
            "existing external Builder state is not resumable"
        )
    raw_handoff_path = bootstrap.get("handoff_path")
    if not isinstance(raw_handoff_path, str) or not raw_handoff_path:
        raise CodexBuilderRunnerError(
            "existing external Builder handoff path is unavailable"
        )
    current_handoff = Path(raw_handoff_path)
    handoff_root = state_root / "handoffs"
    if (
        handoff_root.is_symlink()
        or current_handoff.parent != handoff_root
    ):
        raise CodexBuilderRunnerError(
            "existing external Builder handoff boundary is unsafe"
        )
    try:
        resolved_root = handoff_root.resolve(strict=True)
        resolved_handoff = current_handoff.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise CodexBuilderRunnerError(
            "existing external Builder handoff is unavailable"
        ) from error
    if (
        resolved_root != handoff_root
        or current_handoff.is_symlink()
        or current_handoff != resolved_handoff
        or resolved_handoff.parent != resolved_root
        or not resolved_handoff.is_file()
        or stat.S_IMODE(resolved_handoff.stat().st_mode) != 0o600
    ):
        raise CodexBuilderRunnerError(
            "existing external Builder handoff boundary is unsafe"
        )
    _validated_handoff_payload(
        resolved_handoff,
        state=state,
        bootstrap=bootstrap,
    )
    return state_path, state, bootstrap, resolved_handoff


def _platform_settings(
    state_root: Path,
    secrets_state: Mapping[str, str],
    *,
    platform_port: int,
) -> Settings:
    environment = _model_off_platform_environment(
        state_root,
        secrets_state,
        platform_port=platform_port,
    )
    return Settings(
        api_token=environment["API_TOKEN"],
        host=environment["HOST"],
        port=int(environment["PORT"]),
        data_dir=Path(environment["DATA_DIR"]),
        workspace_root=Path(environment["WORKSPACE_ROOT"]),
        deepseek_api_key=None,
        model_egress_enabled=False,
        platform_harness_secret_envelope_key=environment["PLATFORM_HARNESS_SECRET_ENVELOPE_KEY"],
        lilies_local_agent_enabled=True,
        lilies_collaboration_enabled=True,
        lilies_collaboration_developer_token=environment["LILIES_COLLABORATION_DEVELOPER_TOKEN"],
        lilies_collaboration_verifier_token=environment["LILIES_COLLABORATION_VERIFIER_TOKEN"],
        lilies_formal_hidden_seed_key=environment["LILIES_FORMAL_HIDDEN_SEED_KEY"],
        lilies_collaborative_development_enabled=True,
        lilies_collaborative_development_signing_key=environment[
            "LILIES_COLLABORATIVE_DEVELOPMENT_SIGNING_KEY"
        ],
        lilies_autonomous_collaboration_enabled=True,
        lilies_local_builder_default=False,
        lilies_platform_base_url=environment["LILIES_PLATFORM_BASE_URL"],
        lilies_platform_contract_version=5,
        adaptive_monitoring_refresh_interval_seconds=0,
    )


def _model_off_platform_environment(
    state_root: Path,
    secrets_state: Mapping[str, str],
    *,
    platform_port: int,
) -> dict[str, str]:
    environment = enterprise_runner._platform_environment(
        state_root,
        secrets_state,
        port=platform_port,
        collaboration_policy="auto_forward",
        enable_model_egress=False,
    )
    environment.update(
        {
            "MODEL_EGRESS_ENABLED": "false",
            "DEEPSEEK_API_KEY": "",
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
        }
    )
    return environment


def _wait_for_managed_attestation(
    process: Any,
    *,
    attestation_secret: str,
    timeout_seconds: float,
) -> None:
    secret = attestation_secret.encode("utf-8")
    if len(secret) < 32:
        raise CodexBuilderRunnerError(
            "environment attestation secret is invalid"
        )
    challenge = (
        "sha256:"
        + hashlib.sha256(os.urandom(32)).hexdigest()
    )
    expected_signature = (
        "sha256:"
        + hmac.new(
            secret,
            challenge.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    )
    expected_body = b'{"identity":"exp-lilies-001-r7-real-hosts"}'
    deadline = time.monotonic() + timeout_seconds
    stable_since: float | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        status = process.poll()
        if status is not None:
            raise CodexBuilderRunnerError(
                f"managed environment boundary exited with status {status}"
            )
        try:
            request = Request(
                "http://127.0.0.1:18002/identity",
                method="GET",
                headers={
                    "X-Lilies-Attestation-Challenge": challenge,
                },
            )
            with urlopen(request, timeout=2) as response:
                body = response.read(len(expected_body) + 1)
                supplied = response.headers.get(
                    "X-Lilies-Environment-Attestation",
                    "",
                )
                ready = (
                    int(response.status) == 200
                    and body == expected_body
                    and hmac.compare_digest(
                        supplied,
                        expected_signature,
                    )
                )
        except (HTTPError, URLError, OSError, TimeoutError) as error:
            last_error = error
            ready = False
        if ready:
            now = time.monotonic()
            stable_since = now if stable_since is None else stable_since
            if now - stable_since >= 1.0 and process.poll() is None:
                return
        else:
            stable_since = None
        time.sleep(0.2)
    raise CodexBuilderRunnerError(
        "managed environment attestation did not become ready"
    ) from last_error


async def _retire_predecessor_authority(
    services: Any,
    *,
    bootstrap: Mapping[str, Any],
    allow_active: bool,
) -> dict[str, Any]:
    credential_ref = bootstrap.get("task_credential_ref")
    if not isinstance(credential_ref, str) or not credential_ref:
        raise CodexBuilderRunnerError(
            "predecessor task credential reference is unavailable"
        )
    predecessor_revision = bootstrap.get("revision")
    if predecessor_revision != REVISION - 1:
        raise CodexBuilderRunnerError(
            "predecessor authority revision is not the immediate predecessor"
        )
    exact_strings = {
        name: bootstrap.get(name)
        for name in (
            "assignment_id",
            "session_id",
            "application_id",
            "channel_id",
            "collaboration_credential_ref",
        )
    }
    if any(
        not isinstance(value, str) or not value
        for value in exact_strings.values()
    ):
        raise CodexBuilderRunnerError(
            "predecessor authority binding is incomplete"
        )
    assignment_id = str(exact_strings["assignment_id"])
    session_id = str(exact_strings["session_id"])
    application_id = str(exact_strings["application_id"])
    channel_id = str(exact_strings["channel_id"])
    collaboration_credential_ref = str(
        exact_strings["collaboration_credential_ref"]
    )
    try:
        record = await services.platform_blackbox_auth.get_credential(
            credential_ref
        )
    except Exception as error:
        raise CodexBuilderRunnerError(
            "predecessor task credential cannot be verified"
        ) from error
    expected_expiry = _parse_utc(
        bootstrap.get("expires_at"),
        label="predecessor task credential expiry",
    )
    bindings = (
        (str(record.credential_ref), credential_ref),
        (str(record.assignment_id), assignment_id),
        (str(record.session_id), session_id),
        (record.expires_at, expected_expiry),
    )
    if (
        any(actual != expected for actual, expected in bindings)
        or [str(value) for value in record.application_ids]
        != [application_id]
    ):
        raise CodexBuilderRunnerError(
            "predecessor task credential binding does not match owner state"
        )

    expected_channel_uuid = uuid5(
        NAMESPACE_URL,
        "lilies:collaboration:"
        f"{TASK_ID}:{predecessor_revision}:{assignment_id}",
    )
    if str(expected_channel_uuid) != channel_id:
        raise CodexBuilderRunnerError(
            "predecessor formal channel identity does not match task revision"
        )
    activation_key = (
        f"formal.channel.activate.{UUID(assignment_id).hex}"
    )
    expected_collaboration_credential_id = uuid5(
        NAMESPACE_URL,
        "lilies:collaboration-credential:"
        f"{expected_channel_uuid}:{activation_key}",
    )
    expected_collaboration_ref = (
        f"collaboration_{expected_collaboration_credential_id.hex}"
    )
    if not hmac.compare_digest(
        collaboration_credential_ref,
        expected_collaboration_ref,
    ):
        raise CodexBuilderRunnerError(
            "predecessor collaboration credential reference is invalid"
        )

    collaboration_store = getattr(
        getattr(services, "collaboration", None),
        "store",
        None,
    )
    if (
        collaboration_store is None
        or not callable(getattr(collaboration_store, "get_channel", None))
        or not callable(
            getattr(collaboration_store, "revoke_credential", None)
        )
        or not callable(
            getattr(
                collaboration_store,
                "close_formal_channel_boundary",
                None,
            )
        )
    ):
        raise CodexBuilderRunnerError(
            "formal collaboration retirement store is unavailable"
        )
    try:
        channel = await collaboration_store.get_channel(channel_id)
    except Exception as error:
        raise CodexBuilderRunnerError(
            "predecessor formal collaboration channel cannot be verified"
        ) from error
    if (
        channel.get("task_id") != TASK_ID
        or channel.get("task_revision") != predecessor_revision
        or str(channel.get("assignment_id")) != assignment_id
        or str(channel.get("lilies_session_id")) != session_id
        or [str(item) for item in channel.get("application_ids", [])]
        != [application_id]
    ):
        raise CodexBuilderRunnerError(
            "predecessor formal collaboration binding does not match owner state"
        )

    authority_active = (
        record.revoked_at is None
        and record.expires_at > datetime.now(timezone.utc)
    ) or (
        str(channel.get("status")) != "closed"
        and expected_expiry > datetime.now(timezone.utc)
    )
    if authority_active and not allow_active:
        raise CodexBuilderRunnerError(
            "active predecessor authority requires "
            "--retire-active-predecessor-authority"
        )
    try:
        revoked_task = await services.platform_blackbox_auth.revoke_credential(
            credential_ref,
            reason=PREDECESSOR_RETIREMENT_REASON,
        )
    except Exception as error:
        raise CodexBuilderRunnerError(
            "predecessor task credential could not be revoked"
        ) from error
    if (
        str(revoked_task.credential_ref) != credential_ref
        or str(revoked_task.assignment_id) != assignment_id
        or str(revoked_task.session_id) != session_id
        or [str(value) for value in revoked_task.application_ids]
        != [application_id]
        or revoked_task.revoked_at is None
    ):
        raise CodexBuilderRunnerError(
            "predecessor task credential revocation changed its binding"
        )
    try:
        revoked_collaboration = (
            await collaboration_store.revoke_credential(
                expected_collaboration_credential_id,
                PREDECESSOR_RETIREMENT_REASON,
            )
        )
    except Exception as error:
        raise CodexBuilderRunnerError(
            "predecessor collaboration credential could not be revoked"
        ) from error
    if (
        str(revoked_collaboration.get("credential_id"))
        != str(expected_collaboration_credential_id)
        or str(revoked_collaboration.get("channel_id")) != channel_id
        or str(revoked_collaboration.get("assignment_id")) != assignment_id
        or str(revoked_collaboration.get("lilies_session_id"))
        != session_id
        or revoked_collaboration.get("revoked_at") is None
    ):
        raise CodexBuilderRunnerError(
            "predecessor collaboration credential revocation changed its binding"
        )
    try:
        closed_channel = await (
            collaboration_store.close_formal_channel_boundary(
                channel_id=channel_id,
                task_id=TASK_ID,
                task_revision=predecessor_revision,
                assignment_id=assignment_id,
                lilies_session_id=session_id,
                application_ids=[application_id],
                idempotency_key=(
                    "external-builder.successor-retirement."
                    f"{assignment_id}.r{REVISION}"
                ),
            )
        )
    except Exception as error:
        raise CodexBuilderRunnerError(
            "predecessor formal collaboration channel could not be closed"
        ) from error
    if (
        closed_channel.get("task_id") != TASK_ID
        or closed_channel.get("task_revision") != predecessor_revision
        or str(closed_channel.get("channel_id")) != channel_id
        or str(closed_channel.get("assignment_id")) != assignment_id
        or str(closed_channel.get("lilies_session_id")) != session_id
        or [
            str(item)
            for item in closed_channel.get("application_ids", [])
        ]
        != [application_id]
        or str(closed_channel.get("status")) != "closed"
        or closed_channel.get("closed_at") is None
    ):
        raise CodexBuilderRunnerError(
            "predecessor formal channel close changed its binding"
        )
    return {
        "schema_version": (
            "v0.4.13-predecessor-authority-retirement-1"
        ),
        "task_id": TASK_ID,
        "predecessor_revision": predecessor_revision,
        "successor_revision": REVISION,
        "assignment_id": assignment_id,
        "session_id": session_id,
        "application_id": application_id,
        "task_credential_ref": credential_ref,
        "task_credential_revoked_at": (
            revoked_task.revoked_at.isoformat()
        ),
        "collaboration_channel_id": channel_id,
        "collaboration_credential_ref": collaboration_credential_ref,
        "collaboration_credential_revoked_at": (
            revoked_collaboration["revoked_at"]
        ),
        "collaboration_channel_closed_at": closed_channel["closed_at"],
        "active_predecessor_retirement_authorized": allow_active,
        "retirement_reason": PREDECESSOR_RETIREMENT_REASON,
    }


def _prepare_host_environment(
    environment_state_root: Path,
    *,
    seed: str,
    inherited_environment: Mapping[str, str],
) -> None:
    commands: tuple[tuple[str, ...], ...] = (
        ("config",),
        ("reset", "--confirm-task-id", TASK_ID),
        ("up",),
        ("initialize",),
        ("seed", "--seed", seed),
        ("snapshot", "--seed", seed, "--phase", "baseline"),
    )
    for command in commands:
        _environment_command(
            environment_state_root,
            *command,
            inherited_environment=inherited_environment,
        )


def _resume_host_environment(
    environment_state_root: Path,
    *,
    inherited_environment: Mapping[str, str],
) -> None:
    _environment_command(
        environment_state_root,
        "up",
        inherited_environment=inherited_environment,
    )


async def _wait_for_server(
    server: uvicorn.Server,
    task: asyncio.Task[Any],
    *,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while not server.started and time.monotonic() < deadline:
        if task.done():
            await task
            raise CodexBuilderRunnerError("platform stopped before startup")
        await asyncio.sleep(0.05)
    if not server.started:
        raise CodexBuilderRunnerError("in-process platform did not become ready")


def _write_bootstrap_state(
    state_root: Path,
    *,
    seed: str,
    receipt: Mapping[str, Any],
    owner_urls: Mapping[str, str],
    package: Mapping[str, Any],
) -> Path:
    repair_ledger = _load_repair_ledger(state_root, seed)
    repair_ledger_path = _repair_ledger_path(state_root, seed)
    if not repair_ledger_path.exists():
        enterprise_runner._atomic_private_json(repair_ledger_path, repair_ledger)
    path = state_root / f"codex-builder-seed-{seed}.json"
    enterprise_runner._atomic_private_json(
        path,
        {
            "schema_version": "v0.4.13-t01h-codex-builder-state-1",
            "task_id": TASK_ID,
            "revision": REVISION,
            "seed": seed,
            "builder_actor": "codex",
            "platform_model_egress_enabled": False,
            "formal_archive_supported": True,
            "bootstrap": _safe_bootstrap_projection(receipt),
            "owner_observation_urls": dict(owner_urls),
            "repair_cycle_ledger": str(repair_ledger_path),
            "repair_cycle_required_order": list(REPAIR_PHASES),
            "package_public_summary_digest": package.get("public_summary_digest"),
            "package_sealed_digest": package.get("sealed_package_digest"),
            "updated_at": enterprise_runner._now(),
        },
    )
    return path


def _finalize_revision_owner_state(
    owner_state: Mapping[str, Any],
    *,
    safe_receipt: Mapping[str, Any],
    package: Mapping[str, Any],
    advanced_from: Mapping[str, Any],
) -> tuple[dict[str, Any], bool]:
    finalized = dict(owner_state)
    owner_revision = finalized.get("revision")
    owner_bootstrap = finalized.get("bootstrap")
    if owner_revision == REVISION:
        if (
            not isinstance(owner_bootstrap, dict)
            or owner_bootstrap.get("handoff_digest")
            != safe_receipt.get("handoff_digest")
            or owner_bootstrap.get("assignment_id")
            != safe_receipt.get("assignment_id")
        ):
            raise CodexBuilderRunnerError(
                "finalized project revision conflicts with "
                "the replayed handoff"
            )
        return finalized, False
    if owner_revision != REVISION - 1:
        raise CodexBuilderRunnerError(
            "external Builder owner state cannot finalize "
            "the requested project revision"
        )
    revision_history = finalized.get("project_revision_history", [])
    if not isinstance(revision_history, list):
        raise CodexBuilderRunnerError(
            "external Builder project revision history is invalid"
        )
    predecessor_revision = advanced_from.get("revision")
    if predecessor_revision != REVISION - 1:
        raise CodexBuilderRunnerError(
            "external Builder predecessor snapshot is invalid"
        )
    if any(
        isinstance(item, dict)
        and item.get("revision") == predecessor_revision
        for item in revision_history
    ):
        raise CodexBuilderRunnerError(
            "predecessor project revision is already archived"
        )
    finalized["project_revision_history"] = [
        *revision_history,
        dict(advanced_from),
    ]
    finalized["revision"] = REVISION
    finalized["bootstrap"] = dict(safe_receipt)
    finalized["package_public_summary_digest"] = package.get(
        "public_summary_digest"
    )
    finalized["package_sealed_digest"] = package.get(
        "sealed_package_digest"
    )
    pending = finalized.pop("pending_revision_advance", None)
    if pending is not None and pending != advanced_from:
        raise CodexBuilderRunnerError(
            "pending project revision advance differs from final history"
        )
    if "codex_execution" in finalized:
        finalized["last_execution_revision"] = predecessor_revision
    return finalized, True


def _authority_transition_projection(
    handoff: Mapping[str, Any],
    *,
    handoff_digest: str,
) -> dict[str, Any]:
    task = handoff.get("task")
    assignment = handoff.get("assignment")
    platform = handoff.get("platform")
    collaboration = handoff.get("collaboration")
    workspace = handoff.get("workspace")
    if not all(
        isinstance(value, Mapping)
        for value in (task, assignment, platform, collaboration, workspace)
    ):
        raise CodexBuilderRunnerError(
            "resume authority handoff structure is invalid"
        )
    projection = {
        "task_id": task.get("task_id"),
        "revision": task.get("revision"),
        "application_id": assignment.get("application_id"),
        "assignment_id": assignment.get("assignment_id"),
        "session_id": assignment.get("session_id"),
        "channel_id": collaboration.get("channel_id"),
        "task_credential_ref": platform.get("credential_ref"),
        "collaboration_credential_ref": collaboration.get("credential_ref"),
        "platform_base_url": platform.get("base_url"),
        "platform_contract_url": platform.get("contract_url"),
        "platform_contract_digest": platform.get("contract_digest"),
        "workspace_policy_digest": workspace.get("policy_digest"),
        "handoff_digest": handoff_digest,
    }
    if (
        handoff.get("schema_version") != "1.0"
        or handoff.get("builder_actor") != "codex"
        or not isinstance(projection["task_id"], str)
        or type(projection["revision"]) is not int
        or projection["revision"] < 1
        or any(
            not isinstance(projection[key], str) or not projection[key]
            for key in (
                "application_id",
                "assignment_id",
                "session_id",
                "channel_id",
                "task_credential_ref",
                "collaboration_credential_ref",
                "platform_base_url",
                "platform_contract_url",
                "platform_contract_digest",
                "workspace_policy_digest",
            )
        )
        or not handoff_digest.startswith("sha256:")
        or len(handoff_digest) != len("sha256:") + 64
    ):
        raise CodexBuilderRunnerError(
            "resume authority handoff identity is invalid"
        )
    return projection


def _prepare_resume_authority_transition(
    state_path: Path,
    *,
    state: Mapping[str, Any],
    receipt: Mapping[str, Any],
    previous_invocation: Mapping[str, Any],
) -> Path | None:
    predecessor_revision = previous_invocation.get("project_revision")
    successor_revision = receipt.get("revision")
    if predecessor_revision == successor_revision:
        return None
    if (
        type(predecessor_revision) is not int
        or type(successor_revision) is not int
        or successor_revision != predecessor_revision + 1
    ):
        raise CodexBuilderRunnerError(
            "Codex thread authority can advance only one project revision"
        )
    history = state.get("project_revision_history")
    if not isinstance(history, list):
        raise CodexBuilderRunnerError(
            "Codex thread authority transition omitted revision history"
        )
    matches = [
        item
        for item in history
        if isinstance(item, dict)
        and item.get("revision") == predecessor_revision
    ]
    if len(matches) != 1:
        raise CodexBuilderRunnerError(
            "Codex thread authority transition predecessor is not unique"
        )
    predecessor_history = matches[0]
    predecessor_bootstrap = predecessor_history.get("bootstrap")
    current_bootstrap = state.get("bootstrap")
    retirement = predecessor_history.get("authority_retirement")
    adoption = predecessor_history.get("environment_revision_adoption")
    if (
        predecessor_history.get("status")
        != "retired_revision_history_snapshot"
        or predecessor_history.get("channel_appendability")
        != "closed_and_credentials_revoked"
        or not isinstance(predecessor_bootstrap, dict)
        or not isinstance(current_bootstrap, dict)
        or not isinstance(retirement, dict)
        or not isinstance(adoption, dict)
        or current_bootstrap.get("handoff_digest")
        != receipt.get("handoff_digest")
        or current_bootstrap.get("assignment_id")
        != receipt.get("assignment_id")
    ):
        raise CodexBuilderRunnerError(
            "Codex thread authority transition history is incomplete"
        )
    runtime_root = previous_invocation.get("runtime_root")
    thread_id = previous_invocation.get("thread_id")
    if (
        not isinstance(runtime_root, str)
        or not isinstance(thread_id, str)
        or not thread_id
    ):
        raise CodexBuilderRunnerError(
            "Codex thread authority transition omitted prior runtime identity"
        )
    runtime_handoff_path = (
        Path(runtime_root) / "BUILDER_HANDOFF.json"
    )
    runtime_handoff_payload = _read_private_regular_bytes(
        runtime_handoff_path,
        allowed_modes=frozenset({0o600}),
        label="persisted Codex runtime handoff",
    )
    runtime_handoff_digest = _sha256_digest(runtime_handoff_payload)
    successor_handoff_path = Path(str(receipt.get("handoff_path", "")))
    successor_handoff_payload = _read_private_regular_bytes(
        successor_handoff_path,
        allowed_modes=frozenset({0o600}),
        label="successor Codex handoff",
    )
    successor_handoff_digest = _sha256_digest(successor_handoff_payload)
    if successor_handoff_digest != receipt.get("handoff_digest"):
        raise CodexBuilderRunnerError(
            "successor Codex handoff digest differs from owner state"
        )
    if runtime_handoff_digest == successor_handoff_digest:
        return None
    if runtime_handoff_digest != predecessor_bootstrap.get(
        "handoff_digest"
    ):
        raise CodexBuilderRunnerError(
            "persisted Codex runtime handoff is neither predecessor nor successor"
        )
    predecessor_handoff = _decode_json_object(
        runtime_handoff_payload,
        label="persisted predecessor Codex handoff",
    )
    successor_handoff = _decode_json_object(
        successor_handoff_payload,
        label="successor Codex handoff",
    )
    predecessor_projection = _authority_transition_projection(
        predecessor_handoff,
        handoff_digest=runtime_handoff_digest,
    )
    successor_projection = _authority_transition_projection(
        successor_handoff,
        handoff_digest=successor_handoff_digest,
    )
    adoption_digest = adoption.get("adoption_receipt_digest")
    if (
        predecessor_projection["task_id"] != TASK_ID
        or successor_projection["task_id"] != TASK_ID
        or predecessor_projection["revision"] != predecessor_revision
        or successor_projection["revision"] != successor_revision
        or predecessor_projection["application_id"]
        != successor_projection["application_id"]
        or predecessor_projection["platform_base_url"]
        != successor_projection["platform_base_url"]
        or predecessor_projection["platform_contract_url"]
        != successor_projection["platform_contract_url"]
        or predecessor_projection["platform_contract_digest"]
        != successor_projection["platform_contract_digest"]
        or predecessor_projection["workspace_policy_digest"]
        != successor_projection["workspace_policy_digest"]
        or retirement.get("active_predecessor_retirement_authorized")
        is not True
        or retirement.get("predecessor_revision")
        != predecessor_revision
        or retirement.get("successor_revision") != successor_revision
        or not isinstance(adoption_digest, str)
        or not adoption_digest.startswith("sha256:")
    ):
        raise CodexBuilderRunnerError(
            "Codex thread authority transition invariants are invalid"
        )
    transition = {
        "schema_version": AUTHORITY_TRANSITION_SCHEMA_VERSION,
        "thread_id": thread_id,
        "predecessor": predecessor_projection,
        "successor": successor_projection,
        "authority_retirement": dict(retirement),
        "environment_adoption_receipt_digest": adoption_digest,
    }
    transition_path = (
        state_path.parent
        / "handoffs"
        / (
            "codex-resume-authority-"
            f"r{predecessor_revision}-to-r{successor_revision}-"
            f"seed-{state.get('seed')}.json"
        )
    )
    _write_immutable_private_bytes(
        transition_path,
        enterprise_runner._canonical_json(transition),
    )
    return transition_path


def _reconcile_authority_guard_preflight_failure(
    state_path: Path,
    *,
    invocation: dict[str, Any],
) -> bool:
    existing = invocation.get("pre_provider_failure_reconciliation")
    if isinstance(existing, dict):
        evidence_path = existing.get("evidence_path")
        evidence_digest = existing.get("evidence_digest")
        if not isinstance(evidence_path, str) or not isinstance(
            evidence_digest, str
        ):
            raise CodexBuilderRunnerError(
                "pre-provider reconciliation reference is invalid"
            )
        payload = _read_private_regular_bytes(
            Path(evidence_path),
            allowed_modes=frozenset({0o400}),
            label="pre-provider reconciliation evidence",
        )
        if _sha256_digest(payload) != evidence_digest:
            raise CodexBuilderRunnerError(
                "pre-provider reconciliation evidence digest changed"
            )
        evidence = _decode_json_object(
            payload,
            label="pre-provider reconciliation evidence",
        )
        return (
            evidence.get("schema_version")
            == PRE_PROVIDER_RECONCILIATION_SCHEMA_VERSION
            and evidence.get("invocation_id")
            == invocation.get("invocation_id")
            and evidence.get("provider_process_started") is False
            and evidence.get("retry_eligible") is True
        )
    accounting = invocation.get("usage_accounting")
    if (
        invocation.get("status") != "process_failed"
        or invocation.get("error_code") != "CodexBuilderChildExitError"
        or invocation.get("accounting_evidence_level")
        != "child_wrapper_attempt_only"
        or invocation.get("child_wrapper_exit_code") != 2
        or not isinstance(accounting, dict)
        or accounting.get("receipt_status") != "not_reported"
        or accounting.get("usage_receipt_count") != 0
        or accounting.get("model_call_count_support")
        != "attempted_unknown_from_child_wrapper_process"
    ):
        return False
    invocation_index = invocation.get("invocation_index")
    if type(invocation_index) is not int or invocation_index < 1:
        return False
    seed = str(
        enterprise_runner._read_private_json(state_path).get("seed", "")
    )
    log_path = (
        state_path.parent
        / "logs"
        / (
            f"codex-builder-child-{seed}-"
            f"invocation-{invocation_index:04d}.log"
        )
    )
    log_payload = _read_private_regular_bytes(
        log_path,
        allowed_modes=frozenset({0o600, 0o644}),
        label="Codex child preflight log",
    )
    if not hmac.compare_digest(
        log_payload,
        AUTHORITY_GUARD_PREFLIGHT_ERROR,
    ):
        return False
    for key in ("transcript_path", "stderr_log_path", "result_path"):
        candidate = invocation.get(key)
        if not isinstance(candidate, str):
            return False
        candidate_path = Path(candidate)
        if candidate_path.exists() or candidate_path.is_symlink():
            return False
    evidence = {
        "schema_version": PRE_PROVIDER_RECONCILIATION_SCHEMA_VERSION,
        "invocation_id": invocation.get("invocation_id"),
        "invocation_index": invocation_index,
        "child_wrapper_exit_code": 2,
        "child_log_digest": _sha256_digest(log_payload),
        "failure_guard": "resume_authority_changed_before_provider_process",
        "provider_process_started": False,
        "retry_eligible": True,
    }
    evidence_path = (
        state_path.parent
        / "observations"
        / (
            "codex-builder-pre-provider-reconciliation-"
            f"{seed}-invocation-{invocation_index:04d}.json"
        )
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        evidence_path.parent.is_symlink()
        or stat.S_IMODE(evidence_path.parent.stat().st_mode) != 0o700
    ):
        raise CodexBuilderRunnerError(
            "pre-provider reconciliation directory is unsafe"
        )
    evidence_payload = enterprise_runner._canonical_json(evidence)
    _write_immutable_private_bytes(evidence_path, evidence_payload)
    invocation["pre_provider_failure_reconciliation"] = {
        "evidence_path": str(evidence_path),
        "evidence_digest": _sha256_digest(evidence_payload),
        "provider_process_started": False,
        "retry_eligible": True,
    }
    return True


def _authorize_indeterminate_provider_retry(
    state_path: Path,
    *,
    invocation: dict[str, Any],
    authorized: bool,
) -> bool:
    existing = invocation.get("indeterminate_provider_retry_authorization")
    if isinstance(existing, dict):
        evidence_path = existing.get("evidence_path")
        evidence_digest = existing.get("evidence_digest")
        if not isinstance(evidence_path, str) or not isinstance(
            evidence_digest, str
        ):
            raise CodexBuilderRunnerError(
                "indeterminate provider retry authorization reference is invalid"
            )
        payload = _read_private_regular_bytes(
            Path(evidence_path),
            allowed_modes=frozenset({0o400}),
            label="indeterminate provider retry authorization evidence",
        )
        if _sha256_digest(payload) != evidence_digest:
            raise CodexBuilderRunnerError(
                "indeterminate provider retry authorization evidence digest changed"
            )
        evidence = _decode_json_object(
            payload,
            label="indeterminate provider retry authorization evidence",
        )
        return (
            evidence.get("schema_version")
            == INDETERMINATE_PROVIDER_RETRY_SCHEMA_VERSION
            and evidence.get("invocation_id") == invocation.get("invocation_id")
            and evidence.get("provider_process_started") is True
            and evidence.get("provider_outcome") == "indeterminate"
            and evidence.get("duplicate_charge_or_execution_risk_acknowledged")
            is True
            and evidence.get("retry_eligible") is True
        )
    if not authorized:
        return False
    accounting = invocation.get("usage_accounting")
    if (
        invocation.get("status")
        != "process_only_ended_with_error_business_outcome_unknown"
        or invocation.get("process_execution_status")
        not in {"exited_nonzero", "timed_out"}
        or invocation.get("business_outcome") != "unknown"
        or invocation.get("formal_archive_supported") is not True
        or not isinstance(invocation.get("thread_id"), str)
        or not invocation.get("thread_id")
        or not isinstance(accounting, dict)
        or accounting.get("receipt_status") != "not_reported"
        or accounting.get("usage_receipt_count") != 0
        or int(accounting.get("unknown_usage_model_calls") or 0) < 1
    ):
        return False
    evidence_files: dict[str, str] = {}
    for path_field, digest_field, label in (
        ("result_path", "result_digest", "result"),
        ("transcript_path", "transcript_digest", "transcript"),
        ("stderr_log_path", "stderr_digest", "stderr log"),
    ):
        path_value = invocation.get(path_field)
        expected_digest = invocation.get(digest_field)
        if not isinstance(path_value, str) or not isinstance(
            expected_digest, str
        ):
            return False
        payload = _read_private_regular_bytes(
            Path(path_value),
            allowed_modes=frozenset({0o600}),
            label=f"indeterminate provider {label} evidence",
        )
        if _sha256_digest(payload) != expected_digest:
            raise CodexBuilderRunnerError(
                f"indeterminate provider {label} evidence digest changed"
            )
        evidence_files[path_field] = expected_digest
    invocation_index = invocation.get("invocation_index")
    if type(invocation_index) is not int or invocation_index < 1:
        return False
    seed = str(
        enterprise_runner._read_private_json(state_path).get("seed", "")
    )
    evidence = {
        "schema_version": INDETERMINATE_PROVIDER_RETRY_SCHEMA_VERSION,
        "invocation_id": invocation.get("invocation_id"),
        "invocation_index": invocation_index,
        "provider_process_started": True,
        "provider_outcome": "indeterminate",
        "usage_receipt_status": "not_reported",
        "unknown_usage_model_calls": int(
            accounting["unknown_usage_model_calls"]
        ),
        "duplicate_charge_or_execution_risk_acknowledged": True,
        "authorization_source": (
            "fresh_cli_flag_and_external_token_spend_acknowledgement"
        ),
        "usage_accounting_policy": "preserve_unknown_never_coerce_to_zero",
        "evidence_digests": evidence_files,
        "retry_eligible": True,
    }
    evidence_path = (
        state_path.parent
        / "observations"
        / (
            "codex-builder-indeterminate-provider-retry-"
            f"{seed}-invocation-{invocation_index:04d}.json"
        )
    )
    evidence_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (
        evidence_path.parent.is_symlink()
        or stat.S_IMODE(evidence_path.parent.stat().st_mode) != 0o700
    ):
        raise CodexBuilderRunnerError(
            "indeterminate provider retry evidence directory is unsafe"
        )
    evidence_payload = enterprise_runner._canonical_json(evidence)
    _write_immutable_private_bytes(evidence_path, evidence_payload)
    invocation["indeterminate_provider_retry_authorization"] = {
        "evidence_path": str(evidence_path),
        "evidence_digest": _sha256_digest(evidence_payload),
        "provider_process_started": True,
        "provider_outcome": "indeterminate",
        "duplicate_charge_or_execution_risk_acknowledged": True,
        "retry_eligible": True,
    }
    return True


_CODEX_USAGE_FIELDS: dict[str, str] = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cached_input_tokens",
    "cache_creation_input_tokens": "cache_write_input_tokens",
    "reasoning_tokens": "reasoning_output_tokens",
}


def _required_codex_rollout_budget(
    limit_tokens: int = CODEX_ROLLOUT_TOKEN_LIMIT,
) -> dict[str, Any]:
    if (
        type(limit_tokens) is not int
        or limit_tokens < 2
        or limit_tokens > MAX_CODEX_ROLLOUT_TOKEN_LIMIT
    ):
        raise CodexBuilderRunnerError(
            "external Codex rollout token limit must be between 2 and "
            f"{MAX_CODEX_ROLLOUT_TOKEN_LIMIT}"
        )
    return {
        "enforcement": "codex_cli_rollout_budget",
        "limit_tokens": limit_tokens,
        "maximum_allowed_limit_tokens": MAX_CODEX_ROLLOUT_TOKEN_LIMIT,
        # Codex configures RolloutBudget in-process. A new ``codex exec
        # resume`` process starts a new CLI-local counter, so cross-process
        # continuity is enforced separately by this runner.
        "continues_on_exact_thread_resume": False,
        "token_weights": {
            "sampling": 1.0,
            "prefill": 1.0,
        },
        "multi_agent_enabled": False,
        "config_supported": {
            "rollout_budget": True,
            "multi_agent": False,
            "multi_agent_v2": False,
        },
    }


def _validated_codex_rollout_budget(
    value: Any,
    *,
    expected_limit_tokens: int = CODEX_ROLLOUT_TOKEN_LIMIT,
) -> dict[str, Any]:
    expected = _required_codex_rollout_budget(expected_limit_tokens)
    if not isinstance(value, dict) or set(value) != set(expected):
        raise CodexBuilderRunnerError("isolated Codex rollout-budget receipt is invalid")
    if (
        value.get("enforcement") != expected["enforcement"]
        or type(value.get("limit_tokens")) is not int
        or value.get("limit_tokens") != expected_limit_tokens
        or type(value.get("maximum_allowed_limit_tokens")) is not int
        or value.get("maximum_allowed_limit_tokens") != MAX_CODEX_ROLLOUT_TOKEN_LIMIT
        or value.get("continues_on_exact_thread_resume") is not False
        or value.get("multi_agent_enabled") is not False
    ):
        raise CodexBuilderRunnerError("isolated Codex rollout-budget receipt is invalid")
    token_weights = value.get("token_weights")
    if (
        not isinstance(token_weights, dict)
        or set(token_weights) != {"sampling", "prefill"}
        or type(token_weights.get("sampling")) is not float
        or token_weights.get("sampling") != 1.0
        or type(token_weights.get("prefill")) is not float
        or token_weights.get("prefill") != 1.0
    ):
        raise CodexBuilderRunnerError("isolated Codex rollout-budget receipt is invalid")
    config_supported = value.get("config_supported")
    expected_config = expected["config_supported"]
    if (
        not isinstance(config_supported, dict)
        or set(config_supported) != set(expected_config)
        or config_supported.get("rollout_budget") is not True
        or config_supported.get("multi_agent") is not False
        or config_supported.get("multi_agent_v2") is not False
    ):
        raise CodexBuilderRunnerError("isolated Codex rollout-budget receipt is invalid")
    return expected


def _runner_cumulative_rollout_budget(
    *,
    cumulative_reported_weighted_tokens: int,
    remaining_tokens: int,
) -> dict[str, Any]:
    if (
        type(cumulative_reported_weighted_tokens) is not int
        or cumulative_reported_weighted_tokens < 0
        or type(remaining_tokens) is not int
        or remaining_tokens < 0
        or cumulative_reported_weighted_tokens + remaining_tokens != CODEX_ROLLOUT_TOKEN_LIMIT
    ):
        raise CodexBuilderRunnerError("runner cumulative rollout-budget state is invalid")
    return {
        "enforcement": ("runner_persisted_reported_usage_remaining_budget"),
        "cumulative_limit_tokens": CODEX_ROLLOUT_TOKEN_LIMIT,
        "weighted_token_formula": ("output_tokens+max(0,input_tokens-cache_read_input_tokens)"),
        "token_weights": {
            "sampling": 1.0,
            "prefill": 1.0,
        },
        "prior_usage_requirement": "reported_only_fail_closed",
        "cumulative_reported_weighted_tokens": (cumulative_reported_weighted_tokens),
        "remaining_tokens": remaining_tokens,
        "next_invocation_cli_limit_tokens": (remaining_tokens if remaining_tokens >= 2 else None),
    }


def _validated_runner_cumulative_rollout_budget(
    value: Any,
    *,
    expected_remaining_tokens: int,
) -> dict[str, Any]:
    if (
        type(expected_remaining_tokens) is not int
        or expected_remaining_tokens < 0
        or expected_remaining_tokens > CODEX_ROLLOUT_TOKEN_LIMIT
    ):
        raise CodexBuilderRunnerError(
            "runner cumulative rollout-budget expected remaining value is invalid"
        )
    expected = _runner_cumulative_rollout_budget(
        cumulative_reported_weighted_tokens=(CODEX_ROLLOUT_TOKEN_LIMIT - expected_remaining_tokens),
        remaining_tokens=expected_remaining_tokens,
    )
    if not isinstance(value, dict) or value != expected:
        raise CodexBuilderRunnerError("runner cumulative rollout-budget receipt is invalid")
    return expected


def _reported_weighted_rollout_tokens(
    invocation: Mapping[str, Any],
) -> int:
    invocation_id = str(invocation.get("invocation_id") or "unknown")
    raw_accounting = invocation.get("usage_accounting")
    if not isinstance(raw_accounting, dict):
        raise CodexBuilderRunnerError(
            "Codex resume is forbidden because prior invocation "
            f"{invocation_id} has missing usage accounting"
        )
    accounting = _usage_accounting({"usage_accounting": raw_accounting})
    model_call_count = accounting.get("model_call_count")
    usage_receipt_count = accounting.get("usage_receipt_count")
    if (
        accounting.get("receipt_status") != "reported"
        or accounting.get("unknown_usage_model_calls") != 0
        or not isinstance(model_call_count, int)
        or not isinstance(usage_receipt_count, int)
        or usage_receipt_count < 1
        or usage_receipt_count != model_call_count
    ):
        raise CodexBuilderRunnerError(
            "Codex resume is forbidden because prior invocation "
            f"{invocation_id} has unknown or unreported usage"
        )
    fields = accounting["fields"]
    amounts: dict[str, int] = {}
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
    ):
        field = fields.get(name)
        value = field.get("value") if isinstance(field, dict) else None
        if (
            not isinstance(field, dict)
            or field.get("support") != "reported"
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise CodexBuilderRunnerError(
                "Codex resume is forbidden because prior invocation "
                f"{invocation_id} has missing {name} usage"
            )
        amounts[name] = value
    return amounts["output_tokens"] + max(
        0,
        amounts["input_tokens"] - amounts["cache_read_input_tokens"],
    )


def _cumulative_reported_rollout_usage(
    invocations: Sequence[Mapping[str, Any]],
) -> int:
    total = 0
    for invocation in invocations:
        expected_limit = CODEX_ROLLOUT_TOKEN_LIMIT - total
        if expected_limit < 2:
            raise CodexBuilderRunnerError(
                "Codex resume is forbidden because a prior invocation "
                "started after the cumulative budget was exhausted"
            )
        persisted_limit = invocation.get("rollout_token_limit")
        if (
            not isinstance(persisted_limit, int)
            or isinstance(persisted_limit, bool)
            or persisted_limit != expected_limit
        ):
            raise CodexBuilderRunnerError(
                "Codex resume is forbidden because a prior invocation "
                "has an invalid persisted remaining rollout token limit"
            )
        _validated_codex_rollout_budget(
            invocation.get("rollout_budget_requirement"),
            expected_limit_tokens=expected_limit,
        )
        _validated_codex_rollout_budget(
            invocation.get("rollout_budget"),
            expected_limit_tokens=expected_limit,
        )
        _validated_runner_cumulative_rollout_budget(
            invocation.get("cumulative_rollout_budget_enforcement"),
            expected_remaining_tokens=expected_limit,
        )
        weighted = _reported_weighted_rollout_tokens(invocation)
        total += weighted
        if total > CODEX_ROLLOUT_TOKEN_LIMIT:
            raise CodexBuilderRunnerError(
                "frozen cumulative Codex rollout token budget was exceeded"
            )
    return total


def _codex_child_paths(
    state_root: Path,
    seed: str,
    invocation_index: int = 1,
    runtime_assignment_id: str | None = None,
) -> dict[str, Path]:
    if invocation_index < 1:
        raise CodexBuilderRunnerError("Codex invocation index must be positive")
    runtime_name = f"seed-{seed}"
    if runtime_assignment_id is not None:
        try:
            normalized_assignment_id = str(UUID(runtime_assignment_id))
        except ValueError as error:
            raise CodexBuilderRunnerError(
                "replacement Codex runtime assignment identity is not a UUID"
            ) from error
        if normalized_assignment_id != runtime_assignment_id.casefold():
            raise CodexBuilderRunnerError(
                "replacement Codex runtime assignment identity is not canonical"
            )
        runtime_name = f"{runtime_name}-assignment-{normalized_assignment_id}"
    invocation = f"{invocation_index:04d}"
    return {
        "runtime_root": state_root / "codex-runtime" / runtime_name,
        "transcript": (
            state_root
            / "observations"
            / f"codex-builder-transcript-{seed}-invocation-{invocation}.jsonl"
        ),
        "stderr_log": (
            state_root / "observations" / f"codex-builder-stderr-{seed}-invocation-{invocation}.log"
        ),
        "result": (
            state_root
            / "observations"
            / f"codex-builder-result-{seed}-invocation-{invocation}.json"
        ),
    }


def _usage_accounting_from_transcript(path: Path) -> dict[str, Any]:
    """Recover field support from the original Codex JSON event receipts.

    The child result intentionally stays compact, and older children normalized
    absent usage fields to zero.  A zero is only billable evidence when the
    corresponding field was actually present in a ``turn.completed`` receipt,
    so the runner re-reads the sanitized JSONL and preserves support separately.
    """

    totals = {field: 0 for field in _CODEX_USAGE_FIELDS}
    reported_counts = {field: 0 for field in _CODEX_USAGE_FIELDS}
    turn_completed_count = 0
    usage_receipt_count = 0
    for raw_line in path.read_bytes().splitlines():
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        turn_completed_count += 1
        candidate = event.get("usage")
        if not isinstance(candidate, dict):
            continue
        reported_values = {
            normalized: candidate.get(source)
            for normalized, source in _CODEX_USAGE_FIELDS.items()
            if isinstance(candidate.get(source), int)
            and not isinstance(candidate.get(source), bool)
            and candidate.get(source) >= 0
        }
        if not reported_values:
            continue
        usage_receipt_count += 1
        for normalized in _CODEX_USAGE_FIELDS:
            value = reported_values.get(normalized)
            if value is not None:
                totals[normalized] += value
                reported_counts[normalized] += 1
    fields: dict[str, dict[str, Any]] = {}
    for normalized in _CODEX_USAGE_FIELDS:
        fully_reported = (
            usage_receipt_count > 0 and reported_counts[normalized] == usage_receipt_count
        )
        fields[normalized] = {
            "support": "reported" if fully_reported else "not_reported",
            **({"value": totals[normalized]} if fully_reported else {}),
        }
    return {
        "receipt_status": "reported" if usage_receipt_count else "not_reported",
        "usage_receipt_count": usage_receipt_count,
        "model_call_count": turn_completed_count if turn_completed_count else None,
        "model_call_count_support": (
            "inferred_from_codex_turn_completed_events" if turn_completed_count else "not_reported"
        ),
        "fields": fields,
    }


def _usage_accounting(result: Mapping[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {
            "receipt_status": "not_reported",
            "usage_receipt_count": 0,
            "model_call_count": None,
            "model_call_count_support": "not_reported",
            "unknown_usage_model_calls": 0,
            "fields": {field: {"support": "not_reported"} for field in _CODEX_USAGE_FIELDS},
        }
    value = result.get("usage_accounting")
    if not isinstance(value, dict):
        legacy = result.get("usage")
        if not isinstance(legacy, dict) or not legacy:
            return _usage_accounting(None)
        legacy_fields: dict[str, dict[str, Any]] = {}
        for normalized, source in _CODEX_USAGE_FIELDS.items():
            raw = legacy.get(source)
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
                legacy_fields[normalized] = {
                    "support": "reported",
                    "value": raw,
                }
            else:
                legacy_fields[normalized] = {"support": "not_reported"}
        return {
            "receipt_status": "reported",
            "usage_receipt_count": 1,
            "model_call_count": 1,
            "model_call_count_support": "inferred_from_child_usage_payload",
            "unknown_usage_model_calls": 0,
            "fields": legacy_fields,
        }
    fields = value.get("fields")
    if not isinstance(fields, dict):
        return _usage_accounting(None)
    projection: dict[str, dict[str, Any]] = {}
    for field in _CODEX_USAGE_FIELDS:
        raw = fields.get(field)
        if not isinstance(raw, dict) or raw.get("support") != "reported":
            projection[field] = {"support": "not_reported"}
            continue
        amount = raw.get("value")
        if not isinstance(amount, int) or isinstance(amount, bool) or amount < 0:
            projection[field] = {"support": "not_reported"}
            continue
        projection[field] = {"support": "reported", "value": amount}
    raw_count = value.get("model_call_count")
    count = (
        raw_count
        if isinstance(raw_count, int) and not isinstance(raw_count, bool) and raw_count >= 1
        else None
    )
    count_support = value.get("model_call_count_support")
    if count_support not in {
        "inferred_from_codex_turn_completed_events",
        "inferred_from_child_usage_payload",
        "inferred_from_codex_process_receipt",
        "attempted_unknown_from_child_wrapper_process",
    }:
        count_support = (
            "inferred_from_codex_turn_completed_events" if count is not None else "not_reported"
        )
    raw_receipt_count = value.get("usage_receipt_count")
    receipt_count = (
        raw_receipt_count
        if isinstance(raw_receipt_count, int)
        and not isinstance(raw_receipt_count, bool)
        and raw_receipt_count >= 0
        else 0
    )
    raw_unknown = value.get("unknown_usage_model_calls")
    unknown_usage_model_calls = (
        raw_unknown
        if isinstance(raw_unknown, int) and not isinstance(raw_unknown, bool) and raw_unknown >= 0
        else max(0, int(count or 0) - receipt_count)
    )
    return {
        "receipt_status": (
            "reported"
            if value.get("receipt_status") == "reported" and receipt_count >= 1
            else "not_reported"
        ),
        "usage_receipt_count": receipt_count,
        "model_call_count": count,
        "model_call_count_support": count_support if count is not None else "not_reported",
        "unknown_usage_model_calls": unknown_usage_model_calls,
        "fields": projection,
    }


def _codex_child_environment(
    inherited_environment: Mapping[str, str],
) -> dict[str, str]:
    allowed = {
        "CODEX_HOME",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TERM",
        "TMPDIR",
    }
    environment = {
        key: value
        for key, value in inherited_environment.items()
        if key in allowed and isinstance(value, str) and value
    }
    environment.setdefault("PATH", os.defpath)
    environment.setdefault("LANG", "C.UTF-8")
    environment.setdefault("LC_ALL", "C.UTF-8")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


async def _launch_codex_child(
    stack: ExitStack,
    *,
    state_root: Path,
    seed: str,
    handoff_path: Path,
    model: str,
    timeout_seconds: int,
    inherited_environment: Mapping[str, str],
    invocation_index: int = 1,
    runtime_assignment_id: str | None = None,
    resume_thread_id: str | None = None,
    resume_authority_transition_path: Path | None = None,
    rollout_token_limit: int = CODEX_ROLLOUT_TOKEN_LIMIT,
) -> tuple[dict[str, Any], dict[str, Path]]:
    _assert_external_codex_spend_enabled(state_root)
    if (
        type(rollout_token_limit) is not int
        or rollout_token_limit < 2
        or rollout_token_limit > MAX_CODEX_ROLLOUT_TOKEN_LIMIT
    ):
        raise CodexBuilderRunnerError(
            "external Codex rollout token limit must be between 2 and "
            f"{MAX_CODEX_ROLLOUT_TOKEN_LIMIT}"
        )
    paths = _codex_child_paths(
        state_root,
        seed,
        invocation_index,
        runtime_assignment_id,
    )
    invocation_outputs = (
        paths["transcript"],
        paths["stderr_log"],
        paths["result"],
    )
    conflicts = [path for path in invocation_outputs if path.exists() or path.is_symlink()]
    if conflicts:
        raise CodexBuilderRunnerError(
            "isolated Codex invocation output already exists; refuse to overwrite evidence"
        )
    if resume_thread_id is None and (
        paths["runtime_root"].exists() or paths["runtime_root"].is_symlink()
    ):
        raise CodexBuilderRunnerError(
            "initial Codex invocation cannot reuse an existing isolated runtime"
        )
    if resume_thread_id is not None and not paths["runtime_root"].is_dir():
        raise CodexBuilderRunnerError(
            "resumed Codex invocation requires the persisted isolated runtime"
        )
    child_arguments = [
        sys.executable,
        str(CODEX_CHILD),
        "--handoff",
        str(handoff_path),
        "--runtime-root",
        str(paths["runtime_root"]),
        "--transcript",
        str(paths["transcript"]),
        "--stderr-log",
        str(paths["stderr_log"]),
        "--result",
        str(paths["result"]),
        "--model",
        model,
        "--timeout-seconds",
        str(timeout_seconds),
        "--rollout-token-limit",
        str(rollout_token_limit),
    ]
    if resume_thread_id is not None:
        child_arguments.extend(("--resume-thread-id", resume_thread_id))
    if resume_authority_transition_path is not None:
        if resume_thread_id is None:
            raise CodexBuilderRunnerError(
                "Codex authority transition requires an exact thread resume"
            )
        child_arguments.extend(
            (
                "--resume-authority-transition",
                str(resume_authority_transition_path),
            )
        )
    process = enterprise_runner._managed_process(
        stack,
        tuple(child_arguments),
        environment=_codex_child_environment(inherited_environment),
        log_path=(
            state_root
            / "logs"
            / f"codex-builder-child-{seed}-invocation-{invocation_index:04d}.log"
        ),
    )
    exit_code = await asyncio.to_thread(process.wait)
    if exit_code not in {0, 3}:
        raise CodexBuilderChildExitError(
            "isolated Codex Builder failed before producing an accountable "
            f"model-process receipt (wrapper status {exit_code})",
            wrapper_exit_code=exit_code,
        )
    try:
        result = enterprise_runner._read_private_json(paths["result"])
    except Exception as error:
        raise CodexBuilderChildExitError(
            "isolated Codex Builder did not persist a readable process receipt "
            f"(wrapper status {exit_code})",
            wrapper_exit_code=exit_code,
        ) from error
    result_exit_code = result.get("exit_code")
    timed_out = result.get("timed_out")
    if (
        result.get("schema_version") != "v0.4.13-t01h-codex-builder-child-1"
        or result.get("builder_actor") != "codex"
        or not isinstance(result_exit_code, int)
        or isinstance(result_exit_code, bool)
        or not isinstance(timed_out, bool)
        or result.get("formal_archive_supported") is not True
        or not isinstance(result.get("usage"), dict)
        or not isinstance(result.get("public_api_manual_digest"), str)
    ):
        raise CodexBuilderRunnerError("isolated Codex result binding is invalid")
    result["rollout_budget"] = _validated_codex_rollout_budget(
        result.get("rollout_budget"),
        expected_limit_tokens=rollout_token_limit,
    )
    result_succeeded = result_exit_code == 0 and timed_out is False
    if (exit_code == 0) != result_succeeded:
        raise CodexBuilderRunnerError(
            "isolated Codex wrapper status contradicts its process receipt"
        )
    for name, digest_field in (
        ("transcript", "transcript_digest"),
        ("stderr_log", "stderr_digest"),
    ):
        path = paths[name]
        if not path.is_file() or path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise CodexBuilderRunnerError(
                f"isolated Codex {name} evidence is unavailable or unsafe"
            )
        observed_digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        if result.get(digest_field) != observed_digest:
            raise CodexBuilderRunnerError(f"isolated Codex {name} evidence digest changed")
    result["usage_accounting"] = _usage_accounting_from_transcript(paths["transcript"])
    if result["usage_accounting"].get("model_call_count") is None:
        # The child receipt proves that the isolated Codex subprocess was
        # launched.  Without a provider usage receipt the call count is known
        # but its token amount is not, regardless of the process exit status.
        result["usage_accounting"]["model_call_count"] = 1
        result["usage_accounting"]["model_call_count_support"] = (
            "inferred_from_codex_process_receipt"
        )
        result["usage_accounting"]["unknown_usage_model_calls"] = 1
    else:
        result["usage_accounting"]["unknown_usage_model_calls"] = max(
            0,
            int(result["usage_accounting"].get("model_call_count") or 0)
            - int(result["usage_accounting"].get("usage_receipt_count") or 0),
        )
    result["child_wrapper_exit_code"] = exit_code
    result["process_execution_status"] = (
        "completed" if result_succeeded else "timed_out" if timed_out else "exited_nonzero"
    )
    result_thread_id = result.get("thread_id")
    if result_succeeded and (not isinstance(result_thread_id, str) or not result_thread_id.strip()):
        raise CodexBuilderRunnerError("isolated Codex result omitted the resumable thread identity")
    if resume_thread_id is not None and result_thread_id != resume_thread_id:
        raise CodexBuilderRunnerError(
            "resumed Codex invocation changed the persisted thread identity"
        )
    if result_thread_id is not None and not isinstance(result_thread_id, str):
        raise CodexBuilderRunnerError("isolated Codex result contains an invalid thread identity")
    resume_state_path = result.get("resume_state_path")
    resume_state_digest = result.get("resume_state_digest")
    if result_thread_id is not None:
        if not isinstance(resume_state_path, str) or not isinstance(resume_state_digest, str):
            raise CodexBuilderRunnerError(
                "isolated Codex result omitted its persistent session binding"
            )
        session_path = Path(resume_state_path)
        try:
            resolved_session = session_path.resolve(strict=True)
            resolved_runtime = paths["runtime_root"].resolve(strict=True)
            resolved_session.relative_to(resolved_runtime)
        except (FileNotFoundError, ValueError) as error:
            raise CodexBuilderRunnerError(
                "isolated Codex session binding left its runtime boundary"
            ) from error
        if (
            session_path.is_symlink()
            or not resolved_session.is_file()
            or stat.S_IMODE(resolved_session.stat().st_mode) != 0o600
            or (
                "sha256:" + hashlib.sha256(resolved_session.read_bytes()).hexdigest()
                != resume_state_digest
            )
        ):
            raise CodexBuilderRunnerError(
                "isolated Codex session binding is unavailable or changed"
            )
    elif resume_state_path is not None or resume_state_digest is not None:
        raise CodexBuilderRunnerError("isolated Codex result has session evidence without a thread")
    return result, paths


def _codex_invocation_identity(
    *,
    receipt: Mapping[str, Any],
    invocation_index: int,
) -> dict[str, str]:
    assignment_id = str(receipt["assignment_id"])
    namespace = f"lilies:{TASK_ID}:r{REVISION}:{assignment_id}:codex"
    return {
        "invocation_id": str(uuid5(NAMESPACE_URL, f"{namespace}:invocation:{invocation_index}")),
        "attempt_id": str(uuid5(NAMESPACE_URL, f"{namespace}:project-attempt:{invocation_index}")),
    }


def _prepare_codex_invocation(
    state_path: Path,
    *,
    receipt: Mapping[str, Any],
    resume: bool,
    replace_context: bool = False,
    authorize_indeterminate_provider_retry: bool = False,
) -> dict[str, Any]:
    state = enterprise_runner._read_private_json(state_path)
    invocations = state.get("codex_invocations", [])
    if not isinstance(invocations, list) or any(not isinstance(item, dict) for item in invocations):
        raise CodexBuilderRunnerError("Codex invocation ledger is invalid")
    if any(
        item.get("status") in {"prepared", "running"}
        for item in invocations
        if isinstance(item, dict)
    ):
        raise CodexBuilderRunnerError("a prior Codex invocation has no terminal process receipt")
    max_build_repair_turns = enterprise_runner._task_max_turns()
    invocation_index = len(invocations) + 1
    active_context_start = 0
    for index, item in enumerate(invocations):
        if isinstance(item.get("replacement_context"), dict):
            active_context_start = index
    active_context_invocations = invocations[active_context_start:]
    historical_completed_invocations = [
        item
        for item in active_context_invocations
        if item.get("status")
        == "process_only_completed_business_outcome_unknown"
    ]
    replacement_assignment_id: str | None = None
    replacement_history: dict[str, Any] | None = None
    if replace_context:
        if resume:
            raise CodexBuilderRunnerError(
                "replacement Codex context cannot resume a prior thread"
            )
        if not invocations:
            raise CodexBuilderRunnerError(
                "replacement Codex context requires preserved prior invocation history"
            )
        replacement_assignment_id = str(receipt["assignment_id"])
        prior_assignment_ids = sorted(
            {
                str(item["assignment_id"])
                for item in invocations
                if isinstance(item.get("assignment_id"), str)
                and item.get("assignment_id")
            }
        )
        if replacement_assignment_id in prior_assignment_ids:
            raise CodexBuilderRunnerError(
                "replacement Codex context requires a fresh formal assignment"
            )
        prior_revisions = [
            item["project_revision"]
            for item in invocations
            if type(item.get("project_revision")) is int
        ]
        replacement_revision = receipt.get("revision")
        if (
            type(replacement_revision) is not int
            or not prior_revisions
            or replacement_revision != max(prior_revisions) + 1
        ):
            raise CodexBuilderRunnerError(
                "replacement Codex context requires the adjacent project revision"
            )
        replacement_history = {
            "authorization": "explicit_cli_flag",
            "reason": "fresh_isolated_builder_for_adjacent_project_revision",
            "prior_assignment_ids": prior_assignment_ids,
            "new_assignment_id": replacement_assignment_id,
            "prior_project_revision": max(prior_revisions),
            "new_project_revision": replacement_revision,
            "prior_invocation_count": len(invocations),
            "public_api_boundary_unchanged": True,
            "authorized_at": enterprise_runner._now(),
        }
        completed_invocations: list[dict[str, Any]] = []
    else:
        completed_invocations = historical_completed_invocations
    previous = completed_invocations[-1] if completed_invocations else None
    if replace_context:
        trailing: list[dict[str, Any]] = []
    elif previous is not None:
        previous_position = active_context_invocations.index(previous)
        trailing = active_context_invocations[previous_position + 1 :]
    else:
        trailing = active_context_invocations
    for failed in trailing:
        if (
            not _reconcile_authority_guard_preflight_failure(
                state_path,
                invocation=failed,
            )
            and not _authorize_indeterminate_provider_retry(
                state_path,
                invocation=failed,
                authorized=authorize_indeterminate_provider_retry,
            )
        ):
            raise CodexBuilderRunnerError(
                "Codex resume requires the latest process attempt to have "
                "a terminal accountable receipt, verified pre-provider failure, "
                "or fresh explicit authorization for an indeterminate provider retry"
            )
    model_invocation_count = len(completed_invocations) + 1
    if model_invocation_count > max_build_repair_turns:
        raise CodexBuilderRunnerError(
            "frozen Codex build/repair invocation budget exhausted: "
            f"{len(completed_invocations)} >= {max_build_repair_turns}"
        )
    if previous is None and resume:
        raise CodexBuilderRunnerError("Codex resume requires a prior invocation")
    if invocations and not resume and not replace_context:
        raise CodexBuilderRunnerError(
            "an existing Codex context must be resumed explicitly; "
            "a replacement Builder context is forbidden"
        )
    resume_thread_id: str | None = None
    if previous is not None:
        if (
            previous.get("status") != "process_only_completed_business_outcome_unknown"
            or previous.get("business_outcome") != "unknown"
            or previous.get("formal_archive_supported") is not True
        ):
            raise CodexBuilderRunnerError(
                "Codex resume requires a process-only completed prior invocation"
            )
        candidate = previous.get("thread_id")
        if not isinstance(candidate, str) or not candidate:
            raise CodexBuilderRunnerError(
                "Codex resume requires the persisted prior thread identity"
            )
        try:
            normalized_thread_id = str(UUID(candidate))
        except ValueError as error:
            raise CodexBuilderRunnerError("Codex resume thread identity is not a UUID") from error
        if normalized_thread_id != candidate.casefold():
            raise CodexBuilderRunnerError("Codex resume thread identity is not canonical")
        runtime_root = previous.get("runtime_root")
        if not isinstance(runtime_root, str) or not Path(runtime_root).is_dir():
            raise CodexBuilderRunnerError(
                "Codex resume requires the persisted isolated runtime directory"
            )
        sessions_root = Path(runtime_root) / "codex-home" / "sessions"
        if not sessions_root.is_dir() or sessions_root.is_symlink():
            raise CodexBuilderRunnerError("Codex resume requires isolated session storage")
        session_files = [
            path
            for path in sessions_root.rglob(f"*-{normalized_thread_id}.jsonl")
            if path.is_file() and not path.is_symlink()
        ]
        if len(session_files) != 1:
            raise CodexBuilderRunnerError(
                "Codex resume thread is not uniquely present in isolated session storage"
            )
        resume_state_path = previous.get("resume_state_path")
        if resume_state_path is not None and (
            not isinstance(resume_state_path, str) or not Path(resume_state_path).is_file()
        ):
            raise CodexBuilderRunnerError("Codex resume state declared by the child is unavailable")
        resume_thread_id = candidate
    cumulative_reported_weighted_tokens = (
        _cumulative_reported_rollout_usage(completed_invocations)
        if completed_invocations
        else 0
    )
    remaining_rollout_tokens = CODEX_ROLLOUT_TOKEN_LIMIT - cumulative_reported_weighted_tokens
    if remaining_rollout_tokens < 2:
        raise CodexBuilderRunnerError(
            "frozen cumulative Codex rollout token budget is exhausted; "
            f"{remaining_rollout_tokens} tokens remain"
        )
    rollout_budget_requirement = _required_codex_rollout_budget(remaining_rollout_tokens)
    cumulative_rollout_budget = _runner_cumulative_rollout_budget(
        cumulative_reported_weighted_tokens=(cumulative_reported_weighted_tokens),
        remaining_tokens=remaining_rollout_tokens,
    )
    identities = _codex_invocation_identity(
        receipt=receipt,
        invocation_index=invocation_index,
    )
    runtime_assignment_id = replacement_assignment_id
    if previous is not None:
        persisted_runtime_assignment_id = previous.get("runtime_assignment_id")
        if persisted_runtime_assignment_id is not None and not isinstance(
            persisted_runtime_assignment_id,
            str,
        ):
            raise CodexBuilderRunnerError(
                "Codex resume runtime assignment identity is invalid"
            )
        runtime_assignment_id = persisted_runtime_assignment_id
    paths = _codex_child_paths(
        state_path.parent,
        str(state["seed"]),
        invocation_index,
        runtime_assignment_id,
    )
    if previous is not None and str(paths["runtime_root"]) != previous.get(
        "runtime_root"
    ):
        raise CodexBuilderRunnerError(
            "Codex resume runtime identity does not match the persisted context"
        )
    authority_transition_path = (
        _prepare_resume_authority_transition(
            state_path,
            state=state,
            receipt=receipt,
            previous_invocation=previous,
        )
        if previous is not None
        else None
    )
    invocation = {
        **identities,
        "invocation_index": invocation_index,
        "builder_actor": "codex",
        "project_id": str(receipt["task_id"]),
        "project_revision": int(receipt["revision"]),
        "assignment_id": str(receipt["assignment_id"]),
        "application_id": str(receipt["application_id"]),
        "session_id": str(receipt["session_id"]),
        "status": "prepared",
        "business_outcome": "unknown",
        "claim_level": "process_execution_only",
        "max_build_repair_turns": max_build_repair_turns,
        "rollout_token_limit": remaining_rollout_tokens,
        "rollout_budget_requirement": rollout_budget_requirement,
        "cumulative_rollout_budget_enforcement": cumulative_rollout_budget,
        "subscription_cost_support": "unsupported_no_realtime_usd_meter",
        "formal_archive_supported": True,
        "replacement_context": replacement_history,
        "runtime_assignment_id": runtime_assignment_id,
        "resume_thread_id": resume_thread_id,
        "resumed_from_invocation_id": (
            str(previous["invocation_id"]) if previous is not None else None
        ),
        "preflight_retry_of_invocation_id": (
            str(trailing[-1]["invocation_id"]) if trailing else None
        ),
        "resume_authority_transition_path": (
            str(authority_transition_path)
            if authority_transition_path is not None
            else None
        ),
        "runtime_root": str(paths["runtime_root"]),
        "transcript_path": str(paths["transcript"]),
        "stderr_log_path": str(paths["stderr_log"]),
        "result_path": str(paths["result"]),
        "prepared_at": enterprise_runner._now(),
    }
    invocations.append(invocation)
    state["codex_invocations"] = invocations
    state["codex_context"] = {
        "thread_id": resume_thread_id,
        "runtime_root": str(paths["runtime_root"]),
        "next_invocation_index": invocation_index,
        "resume_required_after_first_invocation": True,
        "replacement_context": replacement_history,
    }
    if replacement_history is not None:
        replacements = state.get("codex_context_replacements", [])
        if not isinstance(replacements, list):
            raise CodexBuilderRunnerError(
                "Codex context replacement ledger is invalid"
            )
        replacements.append(replacement_history)
        state["codex_context_replacements"] = replacements
    state["codex_budget"] = {
        "max_build_repair_turns": max_build_repair_turns,
        "rollout_budget_requirement": rollout_budget_requirement,
        "cumulative_rollout_budget_enforcement": cumulative_rollout_budget,
        "cumulative_limit_tokens": CODEX_ROLLOUT_TOKEN_LIMIT,
        "cumulative_reported_weighted_tokens": (cumulative_reported_weighted_tokens),
        "remaining_tokens_before_invocation": remaining_rollout_tokens,
        "current_invocation_cli_limit_tokens": remaining_rollout_tokens,
        "prepared_invocation_count": invocation_index,
        "remaining_invocations": max(
            0,
            max_build_repair_turns - model_invocation_count,
        ),
        "subscription_cost_support": "unsupported_no_realtime_usd_meter",
        "realtime_cost_limit_usd": None,
    }
    state["updated_at"] = enterprise_runner._now()
    enterprise_runner._atomic_private_json(state_path, state)
    return dict(invocation)


def _mark_codex_invocation_running(
    state_path: Path,
    *,
    invocation_id: str,
) -> None:
    state = enterprise_runner._read_private_json(state_path)
    invocations = state.get("codex_invocations")
    if not isinstance(invocations, list):
        raise CodexBuilderRunnerError("Codex invocation ledger is unavailable")
    matching = [
        item
        for item in invocations
        if isinstance(item, dict) and item.get("invocation_id") == invocation_id
    ]
    if len(matching) != 1 or matching[0].get("status") != "prepared":
        raise CodexBuilderRunnerError("Codex invocation is not startable")
    matching[0]["status"] = "running"
    matching[0]["started_at"] = enterprise_runner._now()
    state["updated_at"] = enterprise_runner._now()
    enterprise_runner._atomic_private_json(state_path, state)


def _mark_codex_invocation_failed(
    state_path: Path,
    *,
    invocation_id: str,
    error_code: str,
    accounting_result: Mapping[str, Any] | None = None,
) -> None:
    state = enterprise_runner._read_private_json(state_path)
    invocations = state.get("codex_invocations")
    if not isinstance(invocations, list):
        return
    matching = [
        item
        for item in invocations
        if isinstance(item, dict) and item.get("invocation_id") == invocation_id
    ]
    if len(matching) != 1:
        return
    matching[0].update(
        {
            "status": "process_failed",
            "business_outcome": "unknown",
            "error_code": error_code,
            "finished_at": enterprise_runner._now(),
        }
    )
    if accounting_result is not None:
        matching[0]["accounting_evidence_level"] = accounting_result.get(
            "accounting_evidence_level"
        )
        matching[0]["child_wrapper_exit_code"] = accounting_result.get("child_wrapper_exit_code")
        matching[0]["usage_accounting"] = _usage_accounting(accounting_result)
    state["updated_at"] = enterprise_runner._now()
    enterprise_runner._atomic_private_json(state_path, state)


def _attach_codex_result(
    state_path: Path,
    *,
    invocation_id: str,
    result: Mapping[str, Any],
    paths: Mapping[str, Path],
) -> None:
    state = enterprise_runner._read_private_json(state_path)
    invocations = state.get("codex_invocations")
    if not isinstance(invocations, list):
        raise CodexBuilderRunnerError("Codex invocation ledger is unavailable")
    matching = [
        item
        for item in invocations
        if isinstance(item, dict) and item.get("invocation_id") == invocation_id
    ]
    if len(matching) != 1 or matching[0].get("status") != "running":
        raise CodexBuilderRunnerError("Codex result has no matching running invocation")
    invocation = matching[0]
    prior_thread_id = invocation.get("resume_thread_id")
    thread_id = result.get("thread_id")
    process_execution_status = str(result.get("process_execution_status") or "completed")
    process_succeeded = process_execution_status == "completed"
    if process_succeeded and (not isinstance(thread_id, str) or not thread_id):
        raise CodexBuilderRunnerError("Codex result omitted its thread identity")
    if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
        raise CodexBuilderRunnerError("Codex result contains an invalid thread identity")
    if prior_thread_id is not None and thread_id != prior_thread_id:
        raise CodexBuilderRunnerError("Codex result changed or omitted its thread identity")
    expected_rollout_token_limit = invocation.get("rollout_token_limit")
    if not isinstance(expected_rollout_token_limit, int) or isinstance(
        expected_rollout_token_limit, bool
    ):
        raise CodexBuilderRunnerError("Codex invocation omitted its persisted rollout token limit")
    _validated_codex_rollout_budget(
        invocation.get("rollout_budget_requirement"),
        expected_limit_tokens=expected_rollout_token_limit,
    )
    cumulative_rollout_budget = _validated_runner_cumulative_rollout_budget(
        invocation.get("cumulative_rollout_budget_enforcement"),
        expected_remaining_tokens=expected_rollout_token_limit,
    )
    rollout_budget = _validated_codex_rollout_budget(
        result.get("rollout_budget"),
        expected_limit_tokens=expected_rollout_token_limit,
    )
    usage = _usage_accounting(result)
    weighted_rollout_tokens: int | None
    try:
        weighted_rollout_tokens = _reported_weighted_rollout_tokens(
            {
                "invocation_id": invocation_id,
                "usage_accounting": usage,
            }
        )
    except CodexBuilderRunnerError:
        weighted_rollout_tokens = None
    result_digest = "sha256:" + hashlib.sha256(paths["result"].read_bytes()).hexdigest()
    invocation.update(
        {
            "status": (
                "process_only_completed_business_outcome_unknown"
                if process_succeeded
                else "process_only_ended_with_error_business_outcome_unknown"
            ),
            "process_execution_status": process_execution_status,
            "business_outcome": "unknown",
            "project_success": False,
            "thread_id": thread_id,
            "child_wrapper_exit_code": result.get("child_wrapper_exit_code"),
            "exit_code": result.get("exit_code"),
            "timed_out": result.get("timed_out"),
            "duration_ms": result.get("duration_ms"),
            "transcript_digest": result.get("transcript_digest"),
            "stderr_digest": result.get("stderr_digest"),
            "result_digest": result_digest,
            "sandbox": result.get("sandbox"),
            "filesystem_read_boundary": result.get("filesystem_read_boundary"),
            "network_boundary": result.get("network_boundary"),
            "rollout_budget": rollout_budget,
            "usage_accounting": usage,
            "reported_weighted_rollout_tokens": weighted_rollout_tokens,
            "cumulative_resume_budget_eligible": (weighted_rollout_tokens is not None),
            "cost_support": "unsupported",
            "public_api_manual_digest": result.get("public_api_manual_digest"),
            "formal_archive_supported": True,
            "finished_at": enterprise_runner._now(),
        }
    )
    for name in ("resume_state_path", "resume_state_digest"):
        value = result.get(name)
        if value is not None:
            invocation[name] = value
    state["codex_execution"] = {
        "status": (
            "process_only_completed_business_outcome_unknown"
            if process_succeeded
            else "process_only_ended_with_error_business_outcome_unknown"
        ),
        "process_execution_status": process_execution_status,
        "business_outcome": "unknown",
        "project_success": False,
        "invocation_id": invocation_id,
        "attempt_id": invocation["attempt_id"],
        "thread_id": result.get("thread_id"),
        "child_wrapper_exit_code": result.get("child_wrapper_exit_code"),
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out"),
        "duration_ms": result.get("duration_ms"),
        "transcript_digest": result.get("transcript_digest"),
        "stderr_digest": result.get("stderr_digest"),
        "sandbox": result.get("sandbox"),
        "filesystem_read_boundary": result.get("filesystem_read_boundary"),
        "network_boundary": result.get("network_boundary"),
        "rollout_budget": rollout_budget,
        "usage_accounting": usage,
        "reported_weighted_rollout_tokens": weighted_rollout_tokens,
        "cumulative_resume_budget_eligible": (weighted_rollout_tokens is not None),
        "cost_support": "unsupported",
        "public_api_manual_digest": result.get("public_api_manual_digest"),
        "formal_archive_supported": True,
        "transcript_path": str(paths["transcript"]),
        "stderr_log_path": str(paths["stderr_log"]),
        "result_path": str(paths["result"]),
    }
    state["codex_context"] = (
        {
            "thread_id": thread_id,
            "runtime_root": str(paths["runtime_root"]),
            "next_invocation_index": int(invocation["invocation_index"]) + 1,
            "resume_required_after_first_invocation": True,
            "resumable": process_succeeded,
        }
        if thread_id is not None
        else {
            "thread_id": None,
            "runtime_root": str(paths["runtime_root"]),
            "next_invocation_index": int(invocation["invocation_index"]) + 1,
            "resume_required_after_first_invocation": True,
            "resumable": False,
        }
    )
    cumulative_before_tokens = cumulative_rollout_budget["cumulative_reported_weighted_tokens"]
    if (
        weighted_rollout_tokens is not None
        and isinstance(cumulative_before_tokens, int)
        and not isinstance(cumulative_before_tokens, bool)
    ):
        cumulative_after = cumulative_before_tokens + weighted_rollout_tokens
        remaining_after = max(
            0,
            CODEX_ROLLOUT_TOKEN_LIMIT - cumulative_after,
        )
        state["codex_budget"] = {
            **dict(state.get("codex_budget", {})),
            "accounting_status": "reported",
            "cumulative_reported_weighted_tokens_after_invocation": (cumulative_after),
            "remaining_tokens_after_invocation": remaining_after,
            "resume_allowed_by_cumulative_budget": remaining_after >= 2,
        }
    else:
        state["codex_budget"] = {
            **dict(state.get("codex_budget", {})),
            "accounting_status": "unknown_fail_closed",
            "cumulative_reported_weighted_tokens_after_invocation": None,
            "remaining_tokens_after_invocation": None,
            "resume_allowed_by_cumulative_budget": False,
        }
    state["updated_at"] = enterprise_runner._now()
    enterprise_runner._atomic_private_json(state_path, state)


def _codex_harness_task_id(
    assignment_id: str,
    invocation_id: str | None = None,
) -> str:
    suffix = f":{invocation_id}" if invocation_id else ""
    return f"external-codex-builder:{assignment_id}{suffix}"


async def _start_codex_harness_task(
    services: Any,
    *,
    receipt: Mapping[str, Any],
    seed: str,
    model: str,
    invocation: Mapping[str, Any] | None = None,
) -> str:
    rollout_token_limit = CODEX_ROLLOUT_TOKEN_LIMIT
    if invocation is not None:
        candidate_limit = invocation.get("rollout_token_limit")
        if not isinstance(candidate_limit, int) or isinstance(candidate_limit, bool):
            raise CodexBuilderRunnerError(
                "Codex invocation omitted its persisted rollout token limit"
            )
        rollout_token_limit = candidate_limit
    rollout_budget_requirement = _required_codex_rollout_budget(rollout_token_limit)
    if invocation is not None and invocation.get("rollout_budget_requirement") is not None:
        rollout_budget_requirement = _validated_codex_rollout_budget(
            invocation.get("rollout_budget_requirement"),
            expected_limit_tokens=rollout_token_limit,
        )
    cumulative_rollout_budget_enforcement = (
        _validated_runner_cumulative_rollout_budget(
            invocation.get("cumulative_rollout_budget_enforcement"),
            expected_remaining_tokens=rollout_token_limit,
        )
        if invocation is not None
        else _runner_cumulative_rollout_budget(
            cumulative_reported_weighted_tokens=0,
            remaining_tokens=CODEX_ROLLOUT_TOKEN_LIMIT,
        )
    )
    invocation_id = (
        str(invocation["invocation_id"])
        if invocation is not None and invocation.get("invocation_id")
        else None
    )
    task_id = _codex_harness_task_id(
        str(receipt["assignment_id"]),
        invocation_id,
    )
    await services.harness.start_task(
        task_id,
        kind="builder_build",
        owner_id=str(receipt["application_id"]),
        resource_id=str(receipt["session_id"]),
        metadata={
            "phase": "external_codex_builder",
            "builder_actor": "codex",
            "application_id": str(receipt["application_id"]),
            "assignment_id": str(receipt["assignment_id"]),
            "session_id": str(receipt["session_id"]),
            "seed": seed,
            "model": model,
            "provider": "openai-codex-cli",
            "cost_support": "unsupported",
            "claim_level": "process_execution_only",
            "accounting_evidence_level": "pending",
            "business_outcome": "unknown",
            "project_success": False,
            "success_aggregation_eligible": False,
            "invocation_id": invocation_id,
            "attempt_id": (
                str(invocation["attempt_id"])
                if invocation is not None and invocation.get("attempt_id")
                else None
            ),
            "max_build_repair_turns": (
                int(invocation["max_build_repair_turns"])
                if invocation is not None
                and isinstance(invocation.get("max_build_repair_turns"), int)
                else enterprise_runner._task_max_turns()
            ),
            "rollout_budget_requirement": rollout_budget_requirement,
            "cumulative_rollout_budget_enforcement": (cumulative_rollout_budget_enforcement),
            "rollout_budget_verification": "pending_child_preflight",
            "subscription_cost_support": (
                invocation.get(
                    "subscription_cost_support",
                    "unsupported_no_realtime_usd_meter",
                )
                if invocation is not None
                else "unsupported_no_realtime_usd_meter"
            ),
            "realtime_cost_limit_usd": None,
            "formal_archive_supported": True,
        },
    )
    return task_id


async def _finish_codex_harness_task(
    services: Any,
    *,
    task_id: str,
    receipt: Mapping[str, Any],
    model: str,
    result: Mapping[str, Any] | None,
    succeeded: bool,
    expected_rollout_token_limit: int = CODEX_ROLLOUT_TOKEN_LIMIT,
    cumulative_rollout_budget_enforcement: Mapping[str, Any] | None = None,
) -> None:
    accounting = _usage_accounting(result)
    if cumulative_rollout_budget_enforcement is None:
        if expected_rollout_token_limit != CODEX_ROLLOUT_TOKEN_LIMIT:
            raise CodexBuilderRunnerError(
                "dynamic Codex harness completion omitted cumulative rollout-budget enforcement"
            )
        cumulative_budget = _runner_cumulative_rollout_budget(
            cumulative_reported_weighted_tokens=0,
            remaining_tokens=CODEX_ROLLOUT_TOKEN_LIMIT,
        )
    else:
        cumulative_budget = _validated_runner_cumulative_rollout_budget(
            cumulative_rollout_budget_enforcement,
            expected_remaining_tokens=expected_rollout_token_limit,
        )
    rollout_budget = (
        _validated_codex_rollout_budget(
            result.get("rollout_budget"),
            expected_limit_tokens=expected_rollout_token_limit,
        )
        if result is not None and result.get("rollout_budget") is not None
        else None
    )
    evidence_bearing_process_receipt = (
        result is not None
        and result.get("accounting_evidence_level") != "child_wrapper_attempt_only"
    )
    if result is not None:
        model_call_count = accounting["model_call_count"]
        if isinstance(model_call_count, int):
            for call_index in range(model_call_count):
                await services.harness.record_usage(
                    task_id,
                    "model_call",
                    metadata={
                        "phase": "external_codex_builder",
                        "builder_actor": "codex",
                        "application_id": str(receipt["application_id"]),
                        "assignment_id": str(receipt["assignment_id"]),
                        "session_id": str(receipt["session_id"]),
                        "provider": "openai-codex-cli",
                        "model": model,
                        "model_call_index": call_index + 1,
                        "model_call_count": model_call_count,
                        "model_call_count_support": accounting["model_call_count_support"],
                    },
                )
        if accounting["receipt_status"] == "reported":
            fields = accounting["fields"]

            def amount(field: str) -> int:
                value = fields[field].get("value")
                return int(value) if isinstance(value, int) else 0

            await services.harness.record_model_usage(
                task_id,
                Usage(
                    input_tokens=amount("input_tokens"),
                    output_tokens=amount("output_tokens"),
                    cache_read_input_tokens=amount("cache_read_input_tokens"),
                    cache_creation_input_tokens=amount("cache_creation_input_tokens"),
                    reasoning_tokens=(
                        amount("reasoning_tokens")
                        if fields["reasoning_tokens"]["support"] == "reported"
                        else None
                    ),
                    cost_source="unsupported",
                    field_support={
                        field: str(fields[field]["support"]) for field in _CODEX_USAGE_FIELDS
                    }
                    | {"cost_usd": "unsupported"},
                ),
                model=model,
                provider="openai-codex-cli",
                metadata={
                    "phase": "external_codex_builder",
                    "builder_actor": "codex",
                    "application_id": str(receipt["application_id"]),
                    "assignment_id": str(receipt["assignment_id"]),
                    "session_id": str(receipt["session_id"]),
                    "usage_receipt_count": accounting["usage_receipt_count"],
                    "model_call_count": accounting["model_call_count"],
                    "model_call_count_support": accounting["model_call_count_support"],
                },
            )
    await services.harness.finish_task(
        task_id,
        # An evidence-bearing timeout/non-zero exit is still paused rather than
        # failed/succeeded: its model usage is accountable, while the business
        # outcome remains unknown.  A child failure with no bound receipt is a
        # runner failure and cannot be accounted as a model attempt.
        status=("paused" if succeeded or evidence_bearing_process_receipt else "failed"),
        error=(
            ""
            if succeeded
            else (
                f"external Codex process {result.get('process_execution_status')}"
                if result is not None
                else "external Codex Builder child failed before an accountable receipt"
            )
        ),
        metadata={
            "transcript_digest": (result.get("transcript_digest") if result is not None else None),
            "cost_support": "unsupported",
            "claim_level": "process_execution_only",
            "accounting_evidence_level": (
                result.get("accounting_evidence_level", "process_receipt")
                if result is not None
                else "none"
            ),
            "process_execution_status": (
                result.get("process_execution_status")
                or ("completed" if succeeded else "exited_nonzero")
                if result is not None
                else "completed"
                if succeeded
                else "failed_before_receipt"
            ),
            "business_outcome": "unknown",
            "project_success": False,
            "success_aggregation_eligible": False,
            "usage_receipt_status": accounting["receipt_status"],
            "model_call_count": accounting["model_call_count"],
            "model_call_count_support": accounting["model_call_count_support"],
            "unknown_usage_model_calls": accounting["unknown_usage_model_calls"],
            "rollout_budget_requirement": _required_codex_rollout_budget(
                expected_rollout_token_limit
            ),
            "cumulative_rollout_budget_enforcement": (cumulative_budget),
            "rollout_budget_receipt": rollout_budget,
            "rollout_budget_verification": (
                "verified_child_receipt"
                if rollout_budget is not None
                else "unavailable_before_child_receipt"
            ),
            "subscription_cost_support": ("unsupported_no_realtime_usd_meter"),
            "realtime_cost_limit_usd": None,
            "formal_archive_supported": True,
        },
    )


def _record_external_token_monitor_snapshot(
    state_root: Path,
    *,
    previous: dict[str, Any] | None,
    previous_at: float,
    observed_at: float,
) -> tuple[dict[str, Any], float]:
    """Use the campaign monitor with the external runner's real breaker state."""

    collector = enterprise_runner.collect_token_monitor_snapshot

    def model_off_collector(**kwargs: Any) -> dict[str, Any]:
        kwargs["model_egress_enabled"] = False
        return collector(**kwargs)

    with patch.object(
        enterprise_runner,
        "collect_token_monitor_snapshot",
        model_off_collector,
    ):
        return enterprise_runner._record_token_monitor_snapshot(
            state_root,
            previous=previous,
            previous_at=previous_at,
            observed_at=observed_at,
        )


async def _external_token_monitor_loop(
    state_root: Path,
    *,
    stop: asyncio.Event,
    previous: dict[str, Any],
    previous_at: float,
    interval_seconds: float = 5.0,
) -> tuple[dict[str, Any], float]:
    if interval_seconds <= 0:
        raise CodexBuilderRunnerError("token monitor interval must be positive")
    snapshot = previous
    observed_at = previous_at
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            break
        except asyncio.TimeoutError:
            current_at = time.monotonic()
            snapshot, observed_at = await asyncio.to_thread(
                _record_external_token_monitor_snapshot,
                state_root,
                previous=snapshot,
                previous_at=observed_at,
                observed_at=current_at,
            )
    current_at = time.monotonic()
    return await asyncio.to_thread(
        _record_external_token_monitor_snapshot,
        state_root,
        previous=snapshot,
        previous_at=observed_at,
        observed_at=current_at,
    )


def _attach_token_monitor_coverage(
    state_path: Path,
    snapshot: Mapping[str, Any],
) -> None:
    state = enterprise_runner._read_private_json(state_path)
    safety = snapshot.get("safety")
    usage = snapshot.get("usage")
    safety_values = safety if isinstance(safety, dict) else {}
    usage_values = usage if isinstance(usage, dict) else {}
    totals = usage_values.get("totals")
    state["token_monitoring"] = {
        "status": (
            "ledger_complete"
            if safety_values.get("evidence_complete") is True
            else "partial_evidence_unknown_not_zero"
        ),
        "missing_required_sources": list(safety_values.get("missing_required_sources", [])),
        "model_egress_enabled": safety_values.get("model_egress_enabled"),
        "totals": dict(totals) if isinstance(totals, dict) else None,
        "latest_path": str(state_path.parent / "monitoring" / "token-monitor.latest.json"),
        "history_path": str(state_path.parent / "monitoring" / "token-monitor.jsonl"),
        "updated_at": enterprise_runner._now(),
    }
    state["updated_at"] = enterprise_runner._now()
    enterprise_runner._atomic_private_json(state_path, state)


async def _serve_and_bootstrap(args: argparse.Namespace) -> int:
    _assert_builder_bootstrap_enabled(args.state_root)
    if args.codex_timeout_seconds < 1 or args.codex_timeout_seconds > 86_400:
        raise CodexBuilderRunnerError("Codex timeout must be between 1 and 86400 seconds")
    state_root = args.state_root.resolve()
    if args.launch_codex:
        _assert_external_codex_spend_enabled(state_root)
    token_spend_authorized = bool(getattr(args, "authorize_external_codex_token_spend", False))
    if args.launch_codex and not token_spend_authorized:
        raise CodexBuilderRunnerError(
            "--launch-codex also requires the fresh --authorize-external-codex-token-spend flag"
        )
    if token_spend_authorized and not args.launch_codex:
        raise CodexBuilderRunnerError(
            "--authorize-external-codex-token-spend requires --launch-codex"
        )
    if args.keep_platform_after_codex and not args.launch_codex:
        raise CodexBuilderRunnerError("--keep-platform-after-codex requires --launch-codex")
    resume_codex = bool(getattr(args, "resume_codex", False))
    replace_codex_context = bool(
        getattr(args, "replace_codex_context", False)
    )
    authorize_indeterminate_provider_retry = bool(
        getattr(args, "authorize_indeterminate_provider_retry", False)
    )
    owner_setup_only = bool(getattr(args, "owner_setup_only", False))
    advance_project_revision = bool(
        getattr(args, "advance_project_revision", False)
    )
    retire_active_predecessor = bool(
        getattr(args, "retire_active_predecessor_authority", False)
    )
    if retire_active_predecessor and not advance_project_revision:
        raise CodexBuilderRunnerError(
            "--retire-active-predecessor-authority requires "
            "--advance-project-revision"
        )
    if resume_codex and not args.launch_codex:
        raise CodexBuilderRunnerError("--resume-codex requires --launch-codex")
    if replace_codex_context and not (
        args.launch_codex
        and token_spend_authorized
        and advance_project_revision
    ):
        raise CodexBuilderRunnerError(
            "--replace-codex-context requires --launch-codex, "
            "--authorize-external-codex-token-spend, and "
            "--advance-project-revision"
        )
    if replace_codex_context and resume_codex:
        raise CodexBuilderRunnerError(
            "--replace-codex-context cannot be combined with --resume-codex"
        )
    if authorize_indeterminate_provider_retry and not (
        args.launch_codex
        and resume_codex
        and token_spend_authorized
    ):
        raise CodexBuilderRunnerError(
            "--authorize-indeterminate-provider-retry requires "
            "--launch-codex, --resume-codex, and "
            "--authorize-external-codex-token-spend"
        )
    if owner_setup_only and (
        args.launch_codex
        or resume_codex
        or replace_codex_context
        or advance_project_revision
        or retire_active_predecessor
        or args.exit_after_bootstrap
    ):
        raise CodexBuilderRunnerError(
            "--owner-setup-only cannot launch Codex, advance a revision, "
            "retire predecessor authority, or exit immediately"
        )
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    default_handoff_path = _handoff_path(state_root, args.seed)
    existing_context = _existing_bootstrap_context(state_root, args.seed)
    handoff_path = (
        existing_context[3]
        if existing_context is not None
        else default_handoff_path
    )
    handoff_exists = (
        existing_context is not None
        or default_handoff_path.exists()
        or default_handoff_path.is_symlink()
    )
    if handoff_exists and not args.skip_environment_prepare:
        raise CodexBuilderRunnerError(
            "external Builder handoff already exists; pass "
            "--skip-environment-prepare to resume it or use a fresh state root"
        )
    if advance_project_revision and not args.skip_environment_prepare:
        raise CodexBuilderRunnerError(
            "--advance-project-revision requires --skip-environment-prepare"
        )
    configured_environment_state_root = getattr(
        args,
        "environment_state_root",
        None,
    )
    if (
        configured_environment_state_root is not None
        and not args.skip_environment_prepare
    ):
        raise CodexBuilderRunnerError(
            "--environment-state-root requires --skip-environment-prepare"
        )
    environment_state_root = _environment_state_root(
        state_root,
        configured_environment_state_root,
    )
    next_handoff_path: Path | None = None
    revision_advance_replay = False
    if advance_project_revision:
        if existing_context is None:
            raise CodexBuilderRunnerError(
                "--advance-project-revision requires an existing Builder state"
            )
        existing_revision = existing_context[1].get("revision")
        bootstrap_revision = existing_context[2].get("revision")
        if type(existing_revision) is not int or (
            existing_revision not in {REVISION - 1, REVISION}
            or bootstrap_revision != existing_revision
        ):
            raise CodexBuilderRunnerError(
                "project revision advance must consume or replay the "
                "immediately preceding immutable Builder state"
            )
        revision_advance_replay = existing_revision == REVISION
        if not revision_advance_replay:
            next_handoff_path = _revision_handoff_path(
                state_root,
                args.seed,
                REVISION,
            )
            if next_handoff_path.is_symlink():
                raise CodexBuilderRunnerError(
                    "next-revision external Builder handoff boundary is unsafe"
                )
    elif existing_context is not None and existing_context[1].get("revision") != REVISION:
        raise CodexBuilderRunnerError(
            "existing external Builder state belongs to another task revision; "
            "use --advance-project-revision only for an expired immediate "
            "predecessor"
        )
    if args.skip_environment_prepare and not advance_project_revision:
        _validate_resumable_environment(
            environment_state_root,
            seed=args.seed,
        )
    _claim_environment_control(
        environment_state_root,
        platform_state_root=state_root,
        seed=args.seed,
    )
    environment_adoption: dict[str, Any] | None = None
    if advance_project_revision:
        environment_adoption = _adopt_environment_revision(
            environment_state_root,
            seed=args.seed,
        )
        _validate_resumable_environment(
            environment_state_root,
            seed=args.seed,
        )
    inherited_environment = os.environ.copy()
    secrets_state = enterprise_runner._runner_secrets(
        state_root,
        create=_create_runner_secrets_for_bootstrap(
            skip_environment_prepare=args.skip_environment_prepare,
            configured_environment_state_root=(
                configured_environment_state_root
            ),
            handoff_exists=handoff_exists,
        ),
    )
    task_token_factory = _deterministic_task_token_factory(
        secrets_state["platform_envelope_key"]
    )
    _record_lifecycle(state_root, args.seed, "runner_started", status="preparing")
    server: uvicorn.Server | None = None
    server_task: asyncio.Task[Any] | None = None
    try:
        if (
            args.skip_environment_prepare
            and configured_environment_state_root is None
            and not advance_project_revision
        ):
            await asyncio.to_thread(
                _resume_host_environment,
                environment_state_root,
                inherited_environment=inherited_environment,
            )
        elif not args.skip_environment_prepare:
            await asyncio.to_thread(
                _prepare_host_environment,
                environment_state_root,
                seed=args.seed,
                inherited_environment=inherited_environment,
            )
            _validate_resumable_environment(
                environment_state_root,
                seed=args.seed,
            )
        settings = _platform_settings(
            state_root,
            secrets_state,
            platform_port=args.platform_port,
        )
        package = await asyncio.to_thread(
            enterprise_runner._freeze_package,
            settings.data_dir,
        )
        _record_lifecycle(
            state_root,
            args.seed,
            "environment_prepared",
            package_public_summary_digest=package.get("public_summary_digest"),
            status="ready",
        )

        # The API module exposes an unused module-level app. Import it only for
        # a real bootstrap and under a provider-free environment so that app
        # cannot accidentally acquire model authority either.
        import_environment = _model_off_platform_environment(
            state_root,
            secrets_state,
            platform_port=args.platform_port,
        )
        with patch.dict(os.environ, import_environment, clear=False):
            from agent_platform.api import create_app

        app = create_app(settings=settings, provider=_NoModelProvider())
        platform_url = f"http://127.0.0.1:{args.platform_port}"
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.platform_port,
            log_level=args.log_level,
            access_log=False,
        )
        server = uvicorn.Server(config)
        with ExitStack() as stack:
            boundary_process = enterprise_runner._managed_process(
                stack,
                (
                    sys.executable,
                    str(ENVIRONMENT_CONTROL),
                    "--state-root",
                    str(environment_state_root),
                    "--package-root",
                    str(TASK_ROOT),
                    "serve",
                ),
                environment=inherited_environment,
                log_path=state_root / "logs" / f"boundary-seed-{args.seed}.log",
            )
            server_task = asyncio.create_task(
                server.serve(),
                name=f"codex-builder-platform-seed-{args.seed}",
            )
            await _wait_for_server(server, server_task, timeout_seconds=60)
            await asyncio.to_thread(
                enterprise_runner._wait_json,
                platform_url,
                "/health",
                timeout_seconds=60,
            )
            await asyncio.to_thread(
                _wait_for_managed_attestation,
                boundary_process,
                attestation_secret=enterprise_runner._host_secrets(
                    environment_state_root.parent
                )["exp-lilies-001-environment-attestation"],
                timeout_seconds=120,
            )
            host_secrets = enterprise_runner._host_secrets(
                environment_state_root.parent
            )
            receipts = await asyncio.to_thread(
                enterprise_runner._install_environment_secrets,
                platform_url,
                secrets_state["platform_api_token"],
                host_secrets,
            )
            if not receipts:
                raise CodexBuilderRunnerError("controlled host secrets were not installed")
            state_path = state_root / f"codex-builder-seed-{args.seed}.json"
            advanced_from: dict[str, Any] | None = None
            if handoff_exists and (
                not advance_project_revision
                or revision_advance_replay
            ):
                existing_state = (
                    existing_context[1]
                    if existing_context is not None
                    else enterprise_runner._read_private_json(state_path)
                )
                existing_bootstrap = existing_state.get("bootstrap")
                if (
                    existing_state.get("task_id") != TASK_ID
                    or existing_state.get("revision") != REVISION
                    or existing_state.get("seed") != args.seed
                    or not isinstance(existing_bootstrap, dict)
                    or existing_bootstrap.get("handoff_path") != str(handoff_path)
                ):
                    raise CodexBuilderRunnerError(
                        "existing external Builder state is not resumable"
                    )
                safe_receipt = _safe_bootstrap_projection(existing_bootstrap)
                owner_urls = _owner_observation_urls(
                    platform_url=platform_url,
                    owner_ui_url=args.owner_ui_url,
                    application_id=str(safe_receipt["application_id"]),
                    assignment_id=str(safe_receipt["assignment_id"]),
                    channel_id=str(safe_receipt["channel_id"]),
                )
                _record_lifecycle(
                    state_root,
                    args.seed,
                    "external_builder_resumed",
                    assignment_id=safe_receipt.get("assignment_id"),
                    session_id=safe_receipt.get("session_id"),
                    status="authority_reused",
                )
            elif advance_project_revision:
                assert existing_context is not None
                assert next_handoff_path is not None
                existing_state = existing_context[1]
                existing_bootstrap = existing_context[2]
                authority_retirement = await _retire_predecessor_authority(
                    app.state.services,
                    bootstrap=existing_bootstrap,
                    allow_active=retire_active_predecessor,
                )
                assert environment_adoption is not None
                advanced_from = {
                    "revision": existing_state["revision"],
                    "bootstrap": _safe_bootstrap_projection(existing_bootstrap),
                    "owner_observation_urls": dict(
                        existing_state.get("owner_observation_urls", {})
                    ),
                    "package_public_summary_digest": existing_state.get(
                        "package_public_summary_digest"
                    ),
                    "package_sealed_digest": existing_state.get(
                        "package_sealed_digest"
                    ),
                    "archived_at": authority_retirement[
                        "collaboration_channel_closed_at"
                    ],
                    "status": "retired_revision_history_snapshot",
                    "channel_appendability": "closed_and_credentials_revoked",
                    "environment_revision_adoption": environment_adoption,
                    "authority_retirement": authority_retirement,
                }
                pending_owner = enterprise_runner._read_private_json(
                    state_path
                )
                if (
                    pending_owner.get("revision") != REVISION - 1
                    or pending_owner.get("bootstrap")
                    != existing_state.get("bootstrap")
                ):
                    raise CodexBuilderRunnerError(
                        "predecessor owner state changed before successor bootstrap"
                    )
                existing_pending = pending_owner.get(
                    "pending_revision_advance"
                )
                if (
                    existing_pending is not None
                    and existing_pending != advanced_from
                ):
                    raise CodexBuilderRunnerError(
                        "pending project revision advance evidence conflicts"
                    )
                pending_owner["pending_revision_advance"] = advanced_from
                pending_owner["updated_at"] = enterprise_runner._now()
                enterprise_runner._atomic_private_json(
                    state_path,
                    pending_owner,
                )
                request = _bootstrap_request(
                    state_root=state_root,
                    seed=args.seed,
                    application_id=str(existing_bootstrap["application_id"]),
                    handoff_path=next_handoff_path,
                )
                receipt_model = await bootstrap_external_builder_async(
                    services=app.state.services,
                    request=request,
                    task_token_factory=task_token_factory,
                )
                safe_receipt = _safe_bootstrap_projection(
                    receipt_model.model_dump(mode="json")
                )
                owner_urls = _owner_observation_urls(
                    platform_url=platform_url,
                    owner_ui_url=args.owner_ui_url,
                    application_id=str(safe_receipt["application_id"]),
                    assignment_id=str(safe_receipt["assignment_id"]),
                    channel_id=str(safe_receipt["channel_id"]),
                )
            else:
                application = await asyncio.to_thread(
                    _load_owner_setup_application,
                    state_root,
                    seed=args.seed,
                    package=package,
                    platform_url=platform_url,
                    platform_token=secrets_state["platform_api_token"],
                )
                if application is None:
                    application = await asyncio.to_thread(
                        enterprise_runner._create_application,
                        platform_url,
                        secrets_state["platform_api_token"],
                        seed=args.seed,
                    )
                    await asyncio.to_thread(
                        _write_owner_setup_state,
                        state_root,
                        seed=args.seed,
                        application=application,
                        package=package,
                    )
                if owner_setup_only:
                    application_id = str(application["id"])
                    owner_setup_path = _owner_setup_path(
                        state_root,
                        args.seed,
                    )
                    _record_lifecycle(
                        state_root,
                        args.seed,
                        "external_builder_owner_setup_ready",
                        application_id=application_id,
                        status="awaiting_owner_connector_configuration",
                    )
                    print(
                        json.dumps(
                            {
                                "status": "awaiting_owner_connector_configuration",
                                "owner_setup_path": str(owner_setup_path),
                                "application_id": application_id,
                                "owner_observation_urls": {
                                    "platform_health": f"{platform_url}/health",
                                    "application": (
                                        f"{args.owner_ui_url.rstrip('/')}"
                                        f"/applications/{application_id}"
                                    ),
                                },
                                "platform_model_egress_enabled": False,
                                "external_codex_launched": False,
                            },
                            ensure_ascii=False,
                            indent=2,
                            sort_keys=True,
                        )
                    )
                    await server_task
                    return 0
                request = _bootstrap_request(
                    state_root=state_root,
                    seed=args.seed,
                    application_id=str(application["id"]),
                )
                receipt_model = await bootstrap_external_builder_async(
                    services=app.state.services,
                    request=request,
                    task_token_factory=task_token_factory,
                )
                receipt = receipt_model.model_dump(mode="json")
                safe_receipt = _safe_bootstrap_projection(receipt)
                owner_urls = _owner_observation_urls(
                    platform_url=platform_url,
                    owner_ui_url=args.owner_ui_url,
                    application_id=str(safe_receipt["application_id"]),
                    assignment_id=str(safe_receipt["assignment_id"]),
                    channel_id=str(safe_receipt["channel_id"]),
                )
                state_path = _write_bootstrap_state(
                    state_root,
                    seed=args.seed,
                    receipt=safe_receipt,
                    owner_urls=owner_urls,
                    package=package,
                )
                _record_lifecycle(
                    state_root,
                    args.seed,
                    "external_builder_bootstrapped",
                    application_id=safe_receipt.get("application_id"),
                    assignment_id=safe_receipt.get("assignment_id"),
                    build_id=safe_receipt.get("build_id"),
                    session_id=safe_receipt.get("session_id"),
                    channel_id=safe_receipt.get("channel_id"),
                    handoff_digest=safe_receipt.get("handoff_digest"),
                    status="awaiting_external_codex",
                )
            handoff_path = Path(str(safe_receipt["handoff_path"]))
            auto_forward = await asyncio.to_thread(
                enterprise_runner._set_auto_forward,
                platform_url,
                secrets_state["platform_api_token"],
                assignment_id=str(safe_receipt["assignment_id"]),
            )
            if (
                not isinstance(auto_forward, dict)
                or auto_forward.get("approval_mode") != "auto_forward"
                or str(auto_forward.get("channel_id")) != str(safe_receipt["channel_id"])
            ):
                raise CodexBuilderRunnerError("autonomous collaboration did not enter auto_forward")
            owner_state = enterprise_runner._read_private_json(state_path)
            revision_advanced = False
            if advanced_from is not None:
                owner_state, revision_advanced = (
                    _finalize_revision_owner_state(
                        owner_state,
                        safe_receipt=safe_receipt,
                        package=package,
                        advanced_from=advanced_from,
                    )
                )
            owner_state["collaboration_policy"] = "auto_forward"
            owner_state["human_monitoring_required"] = False
            owner_state["permission_auto_expansion_enabled"] = False
            owner_state["external_codex_launch_authorized"] = bool(args.launch_codex)
            owner_state["external_codex_token_spend_authorized"] = token_spend_authorized
            owner_state["owner_observation_urls"] = owner_urls
            owner_state["updated_at"] = enterprise_runner._now()
            enterprise_runner._atomic_private_json(state_path, owner_state)
            if revision_advanced:
                _record_lifecycle(
                    state_root,
                    args.seed,
                    "external_builder_project_revision_advanced",
                    application_id=safe_receipt.get("application_id"),
                    assignment_id=safe_receipt.get("assignment_id"),
                    build_id=safe_receipt.get("build_id"),
                    session_id=safe_receipt.get("session_id"),
                    channel_id=safe_receipt.get("channel_id"),
                    handoff_digest=safe_receipt.get("handoff_digest"),
                    status="new_immutable_project_revision",
                )
            print(
                json.dumps(
                    {
                        "status": (
                            "launching_external_codex"
                            if args.launch_codex
                            else "awaiting_external_codex"
                        ),
                        "state_path": str(state_path),
                        "handoff_path": safe_receipt.get("handoff_path"),
                        "owner_observation_urls": owner_urls,
                        "platform_model_egress_enabled": False,
                        "collaboration_policy": "auto_forward",
                        "human_monitoring_required": False,
                        "permission_auto_expansion_enabled": False,
                        "external_codex_token_spend_authorized": token_spend_authorized,
                        "formal_archive_supported": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            if args.launch_codex:
                invocation = _prepare_codex_invocation(
                    state_path,
                    receipt=safe_receipt,
                    resume=resume_codex,
                    replace_context=replace_codex_context,
                    authorize_indeterminate_provider_retry=(
                        authorize_indeterminate_provider_retry
                    ),
                )
                _record_lifecycle(
                    state_root,
                    args.seed,
                    "external_codex_started",
                    session_id=safe_receipt.get("session_id"),
                    status="running",
                )
                harness_task_id = await _start_codex_harness_task(
                    app.state.services,
                    receipt=safe_receipt,
                    seed=args.seed,
                    model=args.codex_model,
                    invocation=invocation,
                )
                monitor_at = time.monotonic()
                monitor_snapshot, monitor_at = await asyncio.to_thread(
                    _record_external_token_monitor_snapshot,
                    state_root,
                    previous=None,
                    previous_at=monitor_at,
                    observed_at=monitor_at,
                )
                monitor_stop = asyncio.Event()
                monitor_task = asyncio.create_task(
                    _external_token_monitor_loop(
                        state_root,
                        stop=monitor_stop,
                        previous=monitor_snapshot,
                        previous_at=monitor_at,
                        interval_seconds=5.0,
                    ),
                    name=f"external-codex-token-monitor-{args.seed}",
                )
                try:
                    _mark_codex_invocation_running(
                        state_path,
                        invocation_id=str(invocation["invocation_id"]),
                    )
                    try:
                        codex_result, codex_paths = await _launch_codex_child(
                            stack,
                            state_root=state_root,
                            seed=args.seed,
                            handoff_path=handoff_path,
                            model=args.codex_model,
                            timeout_seconds=args.codex_timeout_seconds,
                            inherited_environment=inherited_environment,
                            invocation_index=int(invocation["invocation_index"]),
                            runtime_assignment_id=(
                                str(invocation["runtime_assignment_id"])
                                if invocation.get("runtime_assignment_id")
                                else None
                            ),
                            rollout_token_limit=int(invocation["rollout_token_limit"]),
                            resume_thread_id=(
                                str(invocation["resume_thread_id"])
                                if invocation.get("resume_thread_id")
                                else None
                            ),
                            resume_authority_transition_path=(
                                Path(
                                    str(
                                        invocation[
                                            "resume_authority_transition_path"
                                        ]
                                    )
                                )
                                if invocation.get(
                                    "resume_authority_transition_path"
                                )
                                else None
                            ),
                        )
                    except CodexBuilderChildExitError as error:
                        _mark_codex_invocation_failed(
                            state_path,
                            invocation_id=str(invocation["invocation_id"]),
                            error_code=type(error).__name__,
                            accounting_result=error.accounting_result,
                        )
                        await _finish_codex_harness_task(
                            app.state.services,
                            task_id=harness_task_id,
                            receipt=safe_receipt,
                            model=args.codex_model,
                            result=error.accounting_result,
                            succeeded=False,
                            expected_rollout_token_limit=int(invocation["rollout_token_limit"]),
                            cumulative_rollout_budget_enforcement=invocation[
                                "cumulative_rollout_budget_enforcement"
                            ],
                        )
                        raise
                    except Exception as error:
                        _mark_codex_invocation_failed(
                            state_path,
                            invocation_id=str(invocation["invocation_id"]),
                            error_code=type(error).__name__,
                        )
                        await _finish_codex_harness_task(
                            app.state.services,
                            task_id=harness_task_id,
                            receipt=safe_receipt,
                            model=args.codex_model,
                            result=None,
                            succeeded=False,
                            expected_rollout_token_limit=int(invocation["rollout_token_limit"]),
                            cumulative_rollout_budget_enforcement=invocation[
                                "cumulative_rollout_budget_enforcement"
                            ],
                        )
                        raise
                    await _finish_codex_harness_task(
                        app.state.services,
                        task_id=harness_task_id,
                        receipt=safe_receipt,
                        model=args.codex_model,
                        result=codex_result,
                        succeeded=(codex_result.get("process_execution_status") == "completed"),
                        expected_rollout_token_limit=int(invocation["rollout_token_limit"]),
                        cumulative_rollout_budget_enforcement=invocation[
                            "cumulative_rollout_budget_enforcement"
                        ],
                    )
                    _attach_codex_result(
                        state_path,
                        invocation_id=str(invocation["invocation_id"]),
                        result=codex_result,
                        paths=codex_paths,
                    )
                finally:
                    monitor_stop.set()
                    final_monitor_snapshot, _ = await monitor_task
                    _attach_token_monitor_coverage(
                        state_path,
                        final_monitor_snapshot,
                    )
                _record_lifecycle(
                    state_root,
                    args.seed,
                    "external_codex_process_exited",
                    session_id=safe_receipt.get("session_id"),
                    codex_thread_id=codex_result.get("thread_id"),
                    transcript_digest=codex_result.get("transcript_digest"),
                    rollout_budget=codex_result.get("rollout_budget"),
                    status=(
                        "process_only_completed_business_outcome_unknown"
                        if codex_result.get("process_execution_status") == "completed"
                        else "process_only_ended_with_error_business_outcome_unknown"
                    ),
                )
                if codex_result.get("process_execution_status") != "completed":
                    raise CodexBuilderRunnerError(
                        "isolated Codex process "
                        f"{codex_result.get('process_execution_status')} after "
                        "its model-call evidence was durably accounted"
                    )
            if not args.exit_after_bootstrap and (
                not args.launch_codex or args.keep_platform_after_codex
            ):
                await server_task
            else:
                server.should_exit = True
                await server_task
        _record_lifecycle(
            state_root,
            args.seed,
            "runner_stopped",
            status="platform_stopped",
        )
        return 0
    except Exception as error:
        _record_lifecycle(
            state_root,
            args.seed,
            "runner_failed",
            status="failed",
            error_code=type(error).__name__,
        )
        raise
    finally:
        if server is not None and server_task is not None and not server_task.done():
            server.should_exit = True
            try:
                await asyncio.wait_for(server_task, timeout=30)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                server_task.cancel()
                await asyncio.gather(server_task, return_exceptions=True)


def _repair_ledger_path(state_root: Path, seed: str) -> Path:
    return state_root / "observations" / f"codex-builder-repair-cycles-{seed}.json"


def _load_repair_ledger(state_root: Path, seed: str) -> dict[str, Any]:
    path = _repair_ledger_path(state_root, seed)
    if not path.exists():
        return {
            "schema_version": "v0.4.13-t01h-codex-builder-repair-ledger-1",
            "task_id": TASK_ID,
            "revision": REVISION,
            "seed": seed,
            "required_order": list(REPAIR_PHASES),
            "cycles": [],
        }
    value = enterprise_runner._read_private_json(path)
    if (
        value.get("schema_version") != "v0.4.13-t01h-codex-builder-repair-ledger-1"
        or value.get("task_id") != TASK_ID
        or value.get("revision") != REVISION
        or value.get("seed") != seed
        or value.get("required_order") != list(REPAIR_PHASES)
        or not isinstance(value.get("cycles"), list)
    ):
        raise CodexBuilderRunnerError("repair-cycle ledger binding is invalid")
    return value


def _record_repair_phase(
    state_root: Path,
    *,
    seed: str,
    cycle_id: str,
    project_id: str,
    phase: RepairPhase,
    session_id: str,
    record_ref: str,
    record_digest: str,
    outcome: str,
    verified_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        normalized_session_id = str(UUID(session_id))
    except ValueError as error:
        raise CodexBuilderRunnerError("repair-cycle session id must be a UUID") from error
    if (
        not cycle_id
        or not project_id
        or not record_ref
        or not outcome
        or len(cycle_id) > 160
        or len(project_id) > 160
        or len(record_ref) > 500
        or len(outcome) > 160
    ):
        raise CodexBuilderRunnerError("repair-cycle record contains an invalid identifier")
    if (
        not record_digest.startswith("sha256:")
        or len(record_digest) != len("sha256:") + 64
        or any(character not in "0123456789abcdef" for character in record_digest[7:])
    ):
        raise CodexBuilderRunnerError("repair-cycle record digest must be sha256")
    ledger = _load_repair_ledger(state_root, seed)
    matching = [
        cycle
        for cycle in ledger["cycles"]
        if isinstance(cycle, dict) and cycle.get("cycle_id") == cycle_id
    ]
    if phase == "builder_report":
        if matching:
            raise CodexBuilderRunnerError("repair cycle already exists")
        cycle = {
            "cycle_id": cycle_id,
            "project_id": project_id,
            "segments": [],
        }
        ledger["cycles"].append(cycle)
    else:
        if len(matching) != 1:
            raise CodexBuilderRunnerError("repair cycle must begin with a Builder report")
        cycle = matching[0]
        if cycle.get("project_id") != project_id:
            raise CodexBuilderRunnerError("repair cycle cannot change project before its rerun")
    segments = cycle.get("segments")
    if not isinstance(segments, list):
        raise CodexBuilderRunnerError("repair cycle has invalid segments")
    expected_phase = REPAIR_PHASES[len(segments)] if len(segments) < 3 else None
    if phase != expected_phase:
        raise CodexBuilderRunnerError(
            f"repair cycle requires {expected_phase or 'a new cycle'}, not {phase}"
        )
    segment = {
        "phase": phase,
        "project_id": project_id,
        "session_id": normalized_session_id,
        "record_ref": record_ref,
        "record_digest": record_digest,
        "outcome": outcome,
        "recorded_at": enterprise_runner._now(),
        "evidence_verification": (
            "verified_against_platform_db_and_owner_state"
            if verified_binding is not None
            else "unverified_internal_only"
        ),
    }
    if verified_binding is not None:
        segment["verified_binding"] = dict(verified_binding)
    segments.append(segment)
    verified_bindings = [
        item.get("verified_binding") if isinstance(item, dict) else None for item in segments
    ]
    cycle["verified_complete"] = (
        len(segments) == len(REPAIR_PHASES)
        and all(
            isinstance(item, dict)
            and item.get("evidence_verification") == "verified_against_platform_db_and_owner_state"
            for item in segments
        )
        and isinstance(verified_bindings[1], dict)
        and verified_bindings[1].get("source_promotion_verified") is True
        and isinstance(verified_bindings[2], dict)
        and verified_bindings[2].get("history_replay_complete") is True
    )
    cycle["closure_eligible"] = bool(cycle["verified_complete"])
    ledger["updated_at"] = enterprise_runner._now()
    enterprise_runner._atomic_private_json(
        _repair_ledger_path(state_root, seed),
        ledger,
    )
    verified_complete = bool(cycle["verified_complete"])
    return {
        "cycle_id": cycle_id,
        "project_id": project_id,
        "recorded_phase": phase,
        "next_phase": (
            REPAIR_PHASES[len(segments)] if len(segments) < len(REPAIR_PHASES) else None
        ),
        "complete": len(segments) == len(REPAIR_PHASES),
        "verified_complete": verified_complete,
        "closure_eligible": verified_complete,
    }


def _parse_utc(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CodexBuilderRunnerError(f"{label} timestamp is unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CodexBuilderRunnerError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise CodexBuilderRunnerError(f"{label} timestamp has no timezone")
    return parsed


def _platform_evidence_connection(state_root: Path) -> sqlite3.Connection:
    db_path = state_root / "platform-data" / "agent_platform.db"
    try:
        resolved = db_path.resolve(strict=True)
    except FileNotFoundError as error:
        raise CodexBuilderRunnerError("platform evidence database is unavailable") from error
    connection = sqlite3.connect(
        f"{resolved.as_uri()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _evidence_row(
    connection: sqlite3.Connection,
    query: str,
    parameters: Sequence[Any],
    *,
    label: str,
) -> sqlite3.Row:
    try:
        row = connection.execute(query, tuple(parameters)).fetchone()
    except sqlite3.Error as error:
        raise CodexBuilderRunnerError(
            f"{label} cannot be verified in the platform database"
        ) from error
    if row is None:
        raise CodexBuilderRunnerError(f"{label} does not exist")
    return row


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError as error:
        raise CodexBuilderRunnerError(f"{label} payload is invalid") from error
    if not isinstance(decoded, dict):
        raise CodexBuilderRunnerError(f"{label} payload is not an object")
    return decoded


def _uuid_text(value: str, *, label: str) -> str:
    try:
        return str(UUID(value))
    except ValueError as error:
        raise CodexBuilderRunnerError(f"{label} must be a UUID") from error


def _developer_source_promotion_receipt(
    state_root: Path,
    *,
    assignment_id: str,
    response_id: str,
) -> formal_source_provenance.DeveloperSourcePromotionReceipt:
    path = (
        state_root
        / "platform-data"
        / "formal-source-provenance"
        / "assignments"
        / assignment_id
        / "promotions"
        / response_id
        / "activated.json"
    )
    try:
        payload = formal_source_provenance._read_private(
            path,
            limit=2 * 1024 * 1024,
        )
        receipt = formal_source_provenance.DeveloperSourcePromotionReceipt.model_validate(
            formal_source_provenance._strict_json(payload)
        )
        canonical = formal_source_provenance._canonical_json(receipt)
    except Exception as error:
        raise CodexBuilderRunnerError(
            "developer supplementation has no trusted immutable source promotion"
        ) from error
    if not hmac.compare_digest(payload, canonical):
        raise CodexBuilderRunnerError("developer source promotion receipt is not canonical")
    return receipt


def _collaboration_history_replay(
    state_root: Path,
    *,
    state: Mapping[str, Any],
    channel_id: str,
    report_id: str,
    builder_message_id: str,
    developer_message_id: str,
    developer_response_id: str,
    reprobe_message_id: str,
    reprobe_id: str,
) -> dict[str, Any]:
    owner_urls = state.get("owner_observation_urls")
    export_url = (
        owner_urls.get("collaboration_export_api_url") if isinstance(owner_urls, dict) else None
    )
    if not isinstance(export_url, str):
        raise CodexBuilderRunnerError("owner state has no live Collaboration export route")
    parsed = urlsplit(export_url)
    expected_path = f"/api/v1/studio/collaboration/channels/{quote(channel_id, safe='')}/export"
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise CodexBuilderRunnerError(
            "Collaboration export route is not the bound loopback owner API"
        )
    try:
        secrets = enterprise_runner._runner_secrets(state_root, create=False)
        response = enterprise_runner._request_json(
            f"{parsed.scheme}://{parsed.netloc}",
            parsed.path,
            token=secrets["platform_api_token"],
        )
    except Exception as error:
        raise CodexBuilderRunnerError("live Collaboration history export is unavailable") from error
    if (
        not isinstance(response, dict)
        or response.get("schema_version") != "1.0"
        or response.get("channel_id") != channel_id
        or not isinstance(response.get("export"), dict)
        or not isinstance(response.get("counters"), dict)
        or not isinstance(response.get("digest"), str)
    ):
        raise CodexBuilderRunnerError("Collaboration history export envelope is invalid")
    exported = response["export"]
    observed_digest = (
        "sha256:" + hashlib.sha256(enterprise_runner._canonical_json(exported)).hexdigest()
    )
    if not hmac.compare_digest(str(response["digest"]), observed_digest):
        raise CodexBuilderRunnerError("Collaboration history export digest changed")
    messages = exported.get("messages")
    counts = exported.get("counts")
    watermark = exported.get("watermark")
    if (
        exported.get("schema_version") != "1.0"
        or exported.get("complete") is not True
        or not isinstance(messages, list)
        or not messages
        or len(messages) > MAX_COLLABORATION_REPLAY_MESSAGES
        or any(not isinstance(item, dict) for item in messages)
        or not isinstance(counts, dict)
        or not isinstance(watermark, dict)
    ):
        raise CodexBuilderRunnerError(
            "Collaboration history export is incomplete or outside its bound"
        )
    required_counts = {
        "messages",
        "reports",
        "developer_responses",
        "reprobes",
    }
    if not required_counts.issubset(counts):
        raise CodexBuilderRunnerError("Collaboration history export counters are incomplete")
    for collection, count in counts.items():
        values = exported.get(collection)
        if (
            not isinstance(count, int)
            or isinstance(count, bool)
            or not isinstance(values, list)
            or count != len(values)
        ):
            raise CodexBuilderRunnerError("Collaboration history export counters do not match")
    outer_counters = response["counters"]
    correlations = {str(message.get("correlation_id")) for message in messages}
    if (
        outer_counters.get("messages") != len(messages)
        or outer_counters.get("correlations") != len(correlations)
        or outer_counters.get("reports") != len(exported["reports"])
        or outer_counters.get("claims") != len(exported.get("claims", []))
    ):
        raise CodexBuilderRunnerError("Collaboration history export envelope counters do not match")
    if (
        watermark.get("min_message_seq") != 1
        or watermark.get("max_message_seq") != len(messages)
        or watermark.get("next_seq") != len(messages) + 1
    ):
        raise CodexBuilderRunnerError("Collaboration history export watermark is incomplete")
    by_id: dict[str, dict[str, Any]] = {}
    for expected_seq, message in enumerate(messages, start=1):
        message_id = str(message.get("message_id") or "")
        parent_id = message.get("causal_parent_id")
        if (
            message.get("seq") != expected_seq
            or not message_id
            or message_id in by_id
            or (parent_id is not None and str(parent_id) not in by_id)
        ):
            raise CodexBuilderRunnerError(
                "Collaboration history message sequence or parent is invalid"
            )
        by_id[message_id] = message

    required_messages = {
        "builder": (
            builder_message_id,
            "collaboration.report.v1",
            "report_id",
            report_id,
        ),
        "developer": (
            developer_message_id,
            "collaboration.developer_response.v1",
            "response_id",
            developer_response_id,
        ),
        "reprobe": (
            reprobe_message_id,
            "collaboration.lilies_reprobe_result.v1",
            "reprobe_id",
            reprobe_id,
        ),
    }
    message_seqs: dict[str, int] = {}
    for label, (
        message_id,
        payload_schema,
        identity_field,
        identity,
    ) in required_messages.items():
        message = by_id.get(message_id)
        payload = message.get("payload") if isinstance(message, dict) else None
        if (
            not isinstance(message, dict)
            or message.get("payload_schema") != payload_schema
            or str(message.get("correlation_id")) != report_id
            or not isinstance(payload, dict)
            or str(payload.get("report_id")) != report_id
            or str(payload.get(identity_field)) != identity
            or not isinstance(message.get("seq"), int)
        ):
            raise CodexBuilderRunnerError(
                f"Collaboration history omitted or changed the {label} record"
            )
        message_seqs[label] = int(message["seq"])
    if not (message_seqs["builder"] < message_seqs["developer"] < message_seqs["reprobe"]):
        raise CodexBuilderRunnerError(
            "Collaboration supplementation records are out of causal order"
        )

    def has_ancestor(child_id: str, ancestor_id: str) -> bool:
        parent = by_id[child_id].get("causal_parent_id")
        while parent is not None:
            normalized = str(parent)
            if normalized == ancestor_id:
                return True
            parent = by_id[normalized].get("causal_parent_id")
        return False

    if not has_ancestor(developer_message_id, builder_message_id) or not has_ancestor(
        reprobe_message_id,
        developer_message_id,
    ):
        raise CodexBuilderRunnerError(
            "Collaboration supplementation records do not share one causal chain"
        )
    return {
        "history_replay_complete": True,
        "history_export_digest": observed_digest,
        "history_export_message_count": len(messages),
        "history_export_watermark": dict(watermark),
        "history_message_sequences": message_seqs,
        "history_message_ids": {
            "builder": builder_message_id,
            "developer": developer_message_id,
            "reprobe": reprobe_message_id,
        },
        "history_replay_message_limit": MAX_COLLABORATION_REPLAY_MESSAGES,
        "history_replay_http_byte_limit": enterprise_runner.MAX_HTTP_BYTES,
    }


def _verified_owner_and_channel(
    state_root: Path,
    *,
    seed: str,
    project_id: str,
    project_revision: int,
    session_id: str,
    channel_revision: int,
    connection: sqlite3.Connection,
) -> tuple[dict[str, Any], sqlite3.Row]:
    state_path = state_root / f"codex-builder-seed-{seed}.json"
    state = enterprise_runner._read_private_json(state_path)
    bootstrap = state.get("bootstrap")
    if (
        state.get("task_id") != TASK_ID
        or state.get("revision") != REVISION
        or state.get("seed") != seed
        or state.get("builder_actor") != "codex"
        or not isinstance(bootstrap, dict)
    ):
        raise CodexBuilderRunnerError("owner state is not bound to this Codex project")
    normalized_session_id = _uuid_text(session_id, label="repair-cycle session id")
    if (
        str(bootstrap.get("task_id")) != project_id
        or int(bootstrap.get("revision", 0)) != project_revision
        or str(bootstrap.get("session_id")) != normalized_session_id
    ):
        raise CodexBuilderRunnerError(
            "repair-cycle project revision or session does not match owner state"
        )
    channel_id = _uuid_text(
        str(bootstrap.get("channel_id", "")),
        label="owner channel id",
    )
    channel = _evidence_row(
        connection,
        "SELECT * FROM collaboration_channels WHERE channel_id=?",
        (channel_id,),
        label="collaboration channel",
    )
    application_ids = _json_object(
        '{"items":' + str(channel["application_ids_json"]) + "}",
        label="collaboration channel applications",
    ).get("items")
    if (
        str(channel["task_id"]) != project_id
        or int(channel["task_revision"]) != project_revision
        or str(channel["assignment_id"]) != str(bootstrap.get("assignment_id"))
        or str(channel["lilies_session_id"]) != normalized_session_id
        or int(channel["revision"]) != channel_revision
        or not isinstance(application_ids, list)
        or str(bootstrap.get("application_id")) not in {str(item) for item in application_ids}
        or str(channel["status"]) not in {"active", "disconnected", "closing"}
    ):
        raise CodexBuilderRunnerError(
            "collaboration channel revision or project binding is incompatible"
        )
    return state, channel


def _verified_invocation(
    state: Mapping[str, Any],
    *,
    attempt_id: str,
) -> dict[str, Any]:
    normalized_attempt_id = _uuid_text(attempt_id, label="project attempt id")
    invocations = state.get("codex_invocations")
    if not isinstance(invocations, list):
        raise CodexBuilderRunnerError("owner state has no Codex invocation evidence")
    matching = [
        item
        for item in invocations
        if isinstance(item, dict) and item.get("attempt_id") == normalized_attempt_id
    ]
    if len(matching) != 1:
        raise CodexBuilderRunnerError("project attempt does not exist in owner evidence")
    invocation = matching[0]
    if (
        invocation.get("builder_actor") != "codex"
        or invocation.get("status") != "process_only_completed_business_outcome_unknown"
        or invocation.get("business_outcome") != "unknown"
        or invocation.get("project_success") is not False
        or invocation.get("formal_archive_supported") is not True
        or not isinstance(invocation.get("thread_id"), str)
        or not isinstance(invocation.get("transcript_digest"), str)
        or not isinstance(invocation.get("result_digest"), str)
    ):
        raise CodexBuilderRunnerError(
            "project attempt lacks a compatible process-only Codex receipt"
        )
    transcript_path = invocation.get("transcript_path")
    result_path = invocation.get("result_path")
    if (
        not isinstance(transcript_path, str)
        or not isinstance(result_path, str)
        or not Path(transcript_path).is_file()
        or not Path(result_path).is_file()
    ):
        raise CodexBuilderRunnerError("project attempt evidence files are unavailable")
    transcript_digest = "sha256:" + hashlib.sha256(Path(transcript_path).read_bytes()).hexdigest()
    result_digest = "sha256:" + hashlib.sha256(Path(result_path).read_bytes()).hexdigest()
    if (
        transcript_digest != invocation["transcript_digest"]
        or result_digest != invocation["result_digest"]
    ):
        raise CodexBuilderRunnerError("project attempt evidence digest changed")
    return dict(invocation)


def _report_revision_row(
    connection: sqlite3.Connection,
    *,
    report_id: str,
    report_revision: int,
) -> sqlite3.Row:
    try:
        row = connection.execute(
            """
            SELECT report_id,revision,status,route,phase,severity,payload_json,
                   payload_digest,created_at
            FROM collaboration_report_revisions
            WHERE report_id=? AND revision=?
            """,
            (report_id, report_revision),
        ).fetchone()
    except sqlite3.Error as error:
        raise CodexBuilderRunnerError("collaboration report revision cannot be verified") from error
    if row is not None:
        return row
    current = _evidence_row(
        connection,
        "SELECT * FROM collaboration_reports WHERE report_id=? AND revision=?",
        (report_id, report_revision),
        label="collaboration report revision",
    )
    return current


def _repair_cycle_segments(
    state_root: Path,
    *,
    seed: str,
    cycle_id: str,
) -> list[dict[str, Any]]:
    ledger = _load_repair_ledger(state_root, seed)
    matching = [
        cycle
        for cycle in ledger["cycles"]
        if isinstance(cycle, dict) and cycle.get("cycle_id") == cycle_id
    ]
    if len(matching) != 1:
        raise CodexBuilderRunnerError("verified repair cycle predecessor is unavailable")
    segments = matching[0].get("segments")
    if not isinstance(segments, list) or any(not isinstance(item, dict) for item in segments):
        raise CodexBuilderRunnerError("verified repair cycle predecessor is invalid")
    if any(
        item.get("evidence_verification") != "verified_against_platform_db_and_owner_state"
        for item in segments
    ):
        raise CodexBuilderRunnerError(
            "unverified internal ledger segments cannot enter a causal closure"
        )
    return segments


def _verified_repair_binding(
    state_root: Path,
    *,
    seed: str,
    cycle_id: str,
    project_id: str,
    phase: RepairPhase,
    session_id: str,
    record_ref: str,
    record_digest: str,
    outcome: str,
    channel_revision: int,
    report_id: str,
    report_revision: int,
    project_revision: int,
    attempt_id: str,
    report_attempt_id: str | None,
) -> dict[str, Any]:
    normalized_record_ref = _uuid_text(record_ref, label="repair record reference")
    normalized_report_id = _uuid_text(report_id, label="collaboration report id")
    if channel_revision < 1 or report_revision < 1 or project_revision < 1:
        raise CodexBuilderRunnerError("repair evidence revisions must be positive")
    with _platform_evidence_connection(state_root) as connection:
        state, channel = _verified_owner_and_channel(
            state_root,
            seed=seed,
            project_id=project_id,
            project_revision=project_revision,
            session_id=session_id,
            channel_revision=channel_revision,
            connection=connection,
        )
        invocation = _verified_invocation(state, attempt_id=attempt_id)
        if (
            invocation.get("project_id") != project_id
            or int(invocation.get("project_revision", 0)) != project_revision
            or invocation.get("session_id") != str(channel["lilies_session_id"])
        ):
            raise CodexBuilderRunnerError(
                "project attempt crossed project revision or session boundaries"
            )
        report = _evidence_row(
            connection,
            "SELECT * FROM collaboration_reports WHERE report_id=?",
            (normalized_report_id,),
            label="collaboration report",
        )
        if str(report["channel_id"]) != str(channel["channel_id"]) or str(
            report["category"]
        ) not in {"platform_capability_gap", "platform_defect_suspected"}:
            raise CodexBuilderRunnerError(
                "repair report is not a platform gap in the current channel"
            )
        common = {
            "channel_id": str(channel["channel_id"]),
            "channel_revision": int(channel["revision"]),
            "report_id": normalized_report_id,
            "report_revision": report_revision,
            "project_revision": project_revision,
            "attempt_id": str(invocation["attempt_id"]),
            "invocation_id": str(invocation["invocation_id"]),
            "invocation_index": int(invocation["invocation_index"]),
            "builder_actor": "codex",
            "thread_id": str(invocation["thread_id"]),
            "transcript_digest": str(invocation["transcript_digest"]),
            "result_digest": str(invocation["result_digest"]),
        }
        if phase == "builder_report":
            if normalized_record_ref != normalized_report_id:
                raise CodexBuilderRunnerError(
                    "Builder report record reference must be its report id"
                )
            revision = _report_revision_row(
                connection,
                report_id=normalized_report_id,
                report_revision=report_revision,
            )
            allowed_route_statuses = {
                "capability_approval": {
                    "observed",
                    "evidence_collecting",
                    "needs_more_evidence",
                    "awaiting_user_review",
                    "rejected",
                    "withdrawn",
                },
                "developer": {
                    "approved_for_codex",
                    "implementing",
                    "ready_for_lilies_verification",
                    "lilies_verified",
                    "verification_failed",
                },
                "verifier": {
                    "lilies_verified",
                    "independently_verified",
                    "verification_failed",
                },
            }
            if str(revision["payload_digest"]) != record_digest:
                raise CodexBuilderRunnerError(
                    "Builder report digest does not match the persisted revision"
                )
            if outcome != str(report["category"]) or str(
                revision["status"]
            ) not in allowed_route_statuses.get(str(revision["route"]), set()):
                raise CodexBuilderRunnerError(
                    "Builder report category, route, or status is incompatible"
                )
            payload = _json_object(
                revision["payload_json"],
                label="Builder report revision",
            )
            attempted_routes = payload.get("attempted_routes")
            if (
                not isinstance(attempted_routes, list)
                or not attempted_routes
                or not payload.get("expected")
                or not payload.get("actual")
                or not payload.get("missing_contract")
                or not payload.get("manuals_checked")
                or not payload.get("evidence_refs")
            ):
                raise CodexBuilderRunnerError(
                    "Builder report is incomplete for platform-gap routing"
                )
            if report_attempt_id is None:
                raise CodexBuilderRunnerError("Builder report requires an exact attempted-route id")
            normalized_route_attempt = _uuid_text(
                report_attempt_id,
                label="Builder report attempted-route id",
            )
            if normalized_route_attempt not in {
                str(item.get("attempt_id")) for item in attempted_routes if isinstance(item, dict)
            }:
                raise CodexBuilderRunnerError("Builder report attempted-route id does not exist")
            report_created = _parse_utc(report["created_at"], label="Builder report")
            if not (
                _parse_utc(invocation["started_at"], label="Codex invocation")
                <= report_created
                <= _parse_utc(invocation["finished_at"], label="Codex invocation")
            ):
                raise CodexBuilderRunnerError(
                    "Builder report was not created by the bound Codex invocation window"
                )
            builder_message_id = _uuid_text(
                str(report["message_id"]),
                label="Builder report message id",
            )
            return common | {
                "record_kind": "collaboration_report_revision",
                "record_ref": normalized_record_ref,
                "record_digest": record_digest,
                "message_id": builder_message_id,
                "report_attempt_id": normalized_route_attempt,
                "report_status": str(revision["status"]),
                "report_route": str(revision["route"]),
            }

        segments = _repair_cycle_segments(
            state_root,
            seed=seed,
            cycle_id=cycle_id,
        )
        builder_binding = segments[0].get("verified_binding")
        if (
            not isinstance(builder_binding, dict)
            or builder_binding.get("report_id") != normalized_report_id
        ):
            raise CodexBuilderRunnerError(
                "repair record is not linked to the cycle's Builder report"
            )
        if phase == "development_enablement":
            if invocation["attempt_id"] != builder_binding.get("attempt_id"):
                raise CodexBuilderRunnerError(
                    "developer response must bind the reported project attempt"
                )
            response = _evidence_row(
                connection,
                """
                SELECT * FROM collaboration_developer_responses
                WHERE response_id=?
                """,
                (normalized_record_ref,),
                label="developer supplementation response",
            )
            if (
                str(response["channel_id"]) != str(channel["channel_id"])
                or str(response["report_id"]) != normalized_report_id
                or str(response["outcome"]) != "implemented"
                or str(response["outcome"]) != outcome
                or int(response["resulting_report_revision"]) != report_revision
                or int(response["expected_report_revision"])
                != int(builder_binding["report_revision"])
                or str(response["request_digest"]) != record_digest
            ):
                raise CodexBuilderRunnerError(
                    "developer supplementation binding or status is incompatible"
                )
            payload = _json_object(
                response["payload_json"],
                label="developer supplementation response",
            )
            tests_run = payload.get("tests_run")
            implementation_diff_digest = payload.get("implementation_diff_digest")
            generality_rationale = payload.get("generality_rationale")
            if (
                not payload.get("commit_sha")
                or not payload.get("new_contract_digest")
                or not payload.get("generic_capability_changes")
                or not isinstance(implementation_diff_digest, str)
                or not isinstance(generality_rationale, str)
                or not generality_rationale.strip()
                or not isinstance(tests_run, list)
                or not tests_run
                or any(
                    not isinstance(item, dict) or item.get("exit_code") != 0 for item in tests_run
                )
            ):
                raise CodexBuilderRunnerError(
                    "developer supplementation lacks generic change or passing verification"
                )
            assignment_id = _uuid_text(
                str(state["bootstrap"]["assignment_id"]),
                label="developer promotion assignment id",
            )
            lease_id = _uuid_text(
                str(response["lease_id"]),
                label="developer promotion lease id",
            )
            developer_message_id = _uuid_text(
                str(response["message_id"]),
                label="developer response message id",
            )
            promotion = _developer_source_promotion_receipt(
                state_root,
                assignment_id=assignment_id,
                response_id=normalized_record_ref,
            )
            if (
                str(promotion.assignment_id) != assignment_id
                or str(promotion.channel_id) != str(channel["channel_id"])
                or str(promotion.report_id) != normalized_report_id
                or promotion.report_revision != int(response["expected_report_revision"])
                or str(promotion.lease_id) != lease_id
                or str(promotion.response_id) != normalized_record_ref
                or promotion.commit_sha != str(payload["commit_sha"])
                or promotion.intent_digest != implementation_diff_digest
            ):
                raise CodexBuilderRunnerError(
                    "developer supplementation differs from its trusted source promotion"
                )
            if _parse_utc(
                response["created_at"],
                label="developer supplementation",
            ) <= _parse_utc(
                invocation["finished_at"],
                label="reported Codex invocation",
            ):
                raise CodexBuilderRunnerError(
                    "developer supplementation does not postdate the Builder report attempt"
                )
            resulting_revision = _report_revision_row(
                connection,
                report_id=normalized_report_id,
                report_revision=report_revision,
            )
            if (
                str(resulting_revision["status"]) != "ready_for_lilies_verification"
                or str(resulting_revision["route"]) != "developer"
            ):
                raise CodexBuilderRunnerError(
                    "developer response did not produce the reprobe-ready report state"
                )
            return common | {
                "record_kind": "collaboration_developer_response",
                "record_ref": normalized_record_ref,
                "record_digest": record_digest,
                "message_id": developer_message_id,
                "expected_report_revision": int(response["expected_report_revision"]),
                "capability_contract_digest": str(payload["new_contract_digest"]),
                "implementation_commit_sha": str(payload["commit_sha"]),
                "implementation_diff_digest": str(implementation_diff_digest),
                "generality_rationale": generality_rationale,
                "source_promotion_verified": True,
                "promotion_receipt_digest": promotion.receipt_digest,
                "promotion_intent_digest": promotion.intent_digest,
                "promotion_source_manifest_digest": (promotion.source_manifest_digest),
                "promotion_workspace_manifest_digest": (promotion.workspace_manifest_digest),
                "promotion_tree_sha": promotion.tree_sha,
                "promotion_branch_ref": promotion.branch_ref,
                "promotion_changed_paths": list(promotion.changed_paths),
                "verification_test_count": len(tests_run),
                "created_at": str(response["created_at"]),
            }

        developer_binding = segments[1].get("verified_binding")
        if not isinstance(developer_binding, dict):
            raise CodexBuilderRunnerError(
                "same-project rerun has no verified developer predecessor"
            )
        if (
            invocation["attempt_id"] == builder_binding.get("attempt_id")
            or int(invocation.get("invocation_index", 0))
            <= int(builder_binding.get("invocation_index", 0))
            or invocation.get("resume_thread_id") != builder_binding.get("thread_id")
            or invocation.get("thread_id") != builder_binding.get("thread_id")
        ):
            raise CodexBuilderRunnerError(
                "rerun is not a later invocation of the same Codex Builder context"
            )
        reprobe = _evidence_row(
            connection,
            "SELECT * FROM collaboration_reprobes WHERE reprobe_id=?",
            (normalized_record_ref,),
            label="same-project collaboration reprobe",
        )
        payload = _json_object(
            reprobe["payload_json"],
            label="same-project collaboration reprobe",
        )
        if (
            str(reprobe["channel_id"]) != str(channel["channel_id"])
            or str(reprobe["report_id"]) != normalized_report_id
            or str(reprobe["outcome"]) != outcome
            or str(reprobe["request_digest"]) != record_digest
            or int(payload.get("report_revision", 0)) != report_revision
            or payload.get("contract_digest") != developer_binding.get("capability_contract_digest")
            or str(report["status"]) != str(reprobe["outcome"])
        ):
            raise CodexBuilderRunnerError(
                "same-project reprobe binding or contract digest is incompatible"
            )
        reprobe_created = _parse_utc(reprobe["created_at"], label="same-project reprobe")
        if not (
            _parse_utc(invocation["started_at"], label="rerun Codex invocation")
            <= reprobe_created
            <= _parse_utc(invocation["finished_at"], label="rerun Codex invocation")
            and reprobe_created
            > _parse_utc(
                developer_binding.get("created_at"),
                label="developer supplementation",
            )
        ):
            raise CodexBuilderRunnerError(
                "same-project reprobe does not postdate supplementation in its rerun window"
            )
        pre_reprobe_revision = _report_revision_row(
            connection,
            report_id=normalized_report_id,
            report_revision=report_revision,
        )
        if (
            str(pre_reprobe_revision["status"]) != "ready_for_lilies_verification"
            or str(pre_reprobe_revision["route"]) != "developer"
        ):
            raise CodexBuilderRunnerError(
                "same-project reprobe is not bound to the ready report revision"
            )
        reprobe_message_id = _uuid_text(
            str(reprobe["message_id"]),
            label="same-project reprobe message id",
        )
        builder_message_id = _uuid_text(
            str(builder_binding.get("message_id") or ""),
            label="Builder report message id",
        )
        developer_message_id = _uuid_text(
            str(developer_binding.get("message_id") or ""),
            label="developer response message id",
        )
        developer_response_id = _uuid_text(
            str(developer_binding.get("record_ref") or ""),
            label="developer response id",
        )
        history_replay = _collaboration_history_replay(
            state_root,
            state=state,
            channel_id=str(channel["channel_id"]),
            report_id=normalized_report_id,
            builder_message_id=builder_message_id,
            developer_message_id=developer_message_id,
            developer_response_id=developer_response_id,
            reprobe_message_id=reprobe_message_id,
            reprobe_id=normalized_record_ref,
        )
        return (
            common
            | {
                "record_kind": "collaboration_reprobe",
                "record_ref": normalized_record_ref,
                "record_digest": record_digest,
                "message_id": reprobe_message_id,
                "capability_contract_digest": str(payload["contract_digest"]),
                "reprobe_outcome": str(reprobe["outcome"]),
            }
            | history_replay
        )


def _record_cycle_command(args: argparse.Namespace) -> int:
    binding = _verified_repair_binding(
        args.state_root.resolve(),
        seed=args.seed,
        cycle_id=args.cycle_id,
        project_id=args.project_id,
        phase=args.phase,
        session_id=args.session_id,
        record_ref=args.record_ref,
        record_digest=args.record_digest,
        outcome=args.outcome,
        channel_revision=args.channel_revision,
        report_id=args.report_id,
        report_revision=args.report_revision,
        project_revision=args.project_revision,
        attempt_id=args.attempt_id,
        report_attempt_id=args.report_attempt_id,
    )
    result = _record_repair_phase(
        args.state_root.resolve(),
        seed=args.seed,
        cycle_id=args.cycle_id,
        project_id=args.project_id,
        phase=args.phase,
        session_id=args.session_id,
        record_ref=args.record_ref,
        record_digest=args.record_digest,
        outcome=args.outcome,
        verified_binding=binding,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _dry_run_plan(args: argparse.Namespace) -> int:
    plan = {
        "schema_version": "v0.4.13-t01h-codex-builder-plan-1",
        "task_id": TASK_ID,
        "revision": REVISION,
        "seed": args.seed,
        "state_root": str(args.state_root.resolve()),
        "builder_actor": "codex",
        "processes": [
            "controlled_host_boundary",
            "in_process_platform",
            "isolated_codex_child_when_explicitly_authorized",
        ],
        "platform_model_egress_enabled": False,
        "external_codex_model_launch": (
            "requires both fresh --launch-codex and "
            "--authorize-external-codex-token-spend flags, and is impossible "
            "while the state-root EXTERNAL_CODEX_SPEND_DISABLED sentinel exists"
        ),
        "external_codex_resume": (
            "explicit --resume-codex reuses one persisted thread and isolated runtime"
        ),
        "token_monitoring": (
            "0600 preflight, five-second, and terminal snapshots; missing ledgers remain unknown"
        ),
        "rollout_budget_requirement": _required_codex_rollout_budget(),
        "cumulative_rollout_budget_enforcement": (
            _runner_cumulative_rollout_budget(
                cumulative_reported_weighted_tokens=0,
                remaining_tokens=CODEX_ROLLOUT_TOKEN_LIMIT,
            )
        ),
        "subscription_cost_support": "unsupported_no_realtime_usd_meter",
        "realtime_cost_limit_usd": None,
        "builder_handoff": "private_0600_public_contract_and_bearers",
        "owner_state": "private_0600_secret_free_projection_and_urls",
        "human_monitoring_required": False,
        "collaboration_policy": "auto_forward",
        "permission_auto_expansion_enabled": False,
        "repair_cycle_required_order": list(REPAIR_PHASES),
        "forbidden_routes": [
            "standard enterprise runner run/resume",
            "local Lilies daemon",
            "local Lilies formal-build endpoint",
            "platform model provider execution",
        ],
    }
    print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare the frozen EXP-LILIES-001 portfolio for an isolated external "
            "Codex Builder through public platform authority."
        )
    )
    parser.add_argument("--state-root", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("plan", "bootstrap"):
        target = subparsers.add_parser(command)
        target.add_argument(
            "--seed",
            choices=("debug", "101", "202", "303"),
            required=True,
        )
        if command == "bootstrap":
            target.add_argument("--platform-port", type=int, default=DEFAULT_PLATFORM_PORT)
            target.add_argument("--owner-ui-url", default=DEFAULT_OWNER_UI_URL)
            target.add_argument(
                "--skip-environment-prepare",
                action="store_true",
                help=(
                    "Reuse an already seeded state root without reset; also resumes "
                    "an existing handoff."
                ),
            )
            target.add_argument(
                "--environment-state-root",
                type=Path,
                help=(
                    "Adopt this separately owned, already-running private environment "
                    "directory without issuing Docker lifecycle commands while "
                    "preserving the platform/collaboration state root; requires "
                    "--skip-environment-prepare."
                ),
            )
            target.add_argument(
                "--advance-project-revision",
                action="store_true",
                help=(
                    "Adopt byte-identical public inputs and private environment "
                    "identity from the immediate predecessor without reset, "
                    "retire its verified authorities after platform startup, "
                    "and then create the current task revision's assignment."
                ),
            )
            target.add_argument(
                "--retire-active-predecessor-authority",
                action="store_true",
                help=(
                    "Explicitly authorize revocation and exact formal-channel "
                    "closure when the verified immediate predecessor authority "
                    "is still active; valid only with "
                    "--advance-project-revision."
                ),
            )
            target.add_argument(
                "--launch-codex",
                action="store_true",
                help=(
                    "Request the isolated subscription-backed Codex child after "
                    "bootstrap; the separate fresh token-spend flag is also required."
                ),
            )
            target.add_argument(
                "--authorize-external-codex-token-spend",
                action="store_true",
                help=(
                    "Fresh per-process acknowledgement that this invocation may "
                    "consume external Codex tokens; has no effect without "
                    "--launch-codex."
                ),
            )
            target.add_argument(
                "--owner-setup-only",
                action="store_true",
                help=(
                    "Start the controlled hosts and provider-free platform, "
                    "create or verify the empty target application, persist "
                    "its private owner setup identity, and wait so connector "
                    "configuration can be completed before the immutable "
                    "Builder contract is rendered."
                ),
            )
            target.add_argument(
                "--resume-codex",
                action="store_true",
                help=(
                    "Resume the prior isolated Codex thread and runtime for the "
                    "next same-project invocation; requires --launch-codex and "
                    "fails closed if durable thread state is unavailable."
                ),
            )
            target.add_argument(
                "--replace-codex-context",
                action="store_true",
                help=(
                    "Explicitly start a fresh isolated Codex context after an "
                    "adjacent project-revision advance with a new formal "
                    "assignment, while preserving the prior invocation ledger; "
                    "requires --launch-codex and the external token-spend "
                    "acknowledgement and cannot be combined with --resume-codex."
                ),
            )
            target.add_argument(
                "--authorize-indeterminate-provider-retry",
                action="store_true",
                help=(
                    "Fresh one-process acknowledgement that the latest provider "
                    "attempt has no terminal usage/outcome receipt and retrying "
                    "may duplicate token charges or provider-side execution; "
                    "requires --launch-codex, --resume-codex, and the external "
                    "token-spend acknowledgement. Unknown usage remains unknown."
                ),
            )
            target.add_argument("--codex-model", default="gpt-5.6-terra")
            target.add_argument(
                "--codex-timeout-seconds",
                type=int,
                default=10_800,
            )
            target.add_argument(
                "--keep-platform-after-codex",
                action="store_true",
            )
            target.add_argument(
                "--exit-after-bootstrap",
                action="store_true",
                help="Stop boundary and platform after writing the handoff (test only).",
            )
            target.add_argument(
                "--log-level",
                choices=("critical", "error", "warning", "info"),
                default="warning",
            )

    record = subparsers.add_parser("record-cycle")
    record.add_argument(
        "--seed",
        choices=("debug", "101", "202", "303"),
        required=True,
    )
    record.add_argument("--cycle-id", required=True)
    record.add_argument("--project-id", required=True)
    record.add_argument("--phase", choices=REPAIR_PHASES, required=True)
    record.add_argument("--session-id", required=True)
    record.add_argument("--record-ref", required=True)
    record.add_argument("--record-digest", required=True)
    record.add_argument("--outcome", required=True)
    record.add_argument("--channel-revision", type=int, required=True)
    record.add_argument("--report-id", required=True)
    record.add_argument("--report-revision", type=int, required=True)
    record.add_argument("--project-revision", type=int, required=True)
    record.add_argument("--attempt-id", required=True)
    record.add_argument(
        "--report-attempt-id",
        help="Exact attempted_routes[].attempt_id; required for builder_report.",
    )
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    try:
        if args.command == "plan":
            return _dry_run_plan(args)
        if args.command == "record-cycle":
            return _record_cycle_command(args)
        return asyncio.run(_serve_and_bootstrap(args))
    except (
        CodexBuilderRunnerError,
        ExternalBuilderBootstrapError,
        enterprise_runner.EnterpriseExperimentError,
        OSError,
        ValueError,
    ) as error:
        print(
            f"external Codex Builder runner failed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
