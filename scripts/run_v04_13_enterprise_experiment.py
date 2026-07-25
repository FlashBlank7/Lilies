from __future__ import annotations

import argparse
import hashlib
import json
import os
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
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from agent_platform.lilies_client import LiliesClient
from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_models import LocalScope
from agent_platform.task_packages import BudgetSpec, TaskPackageManager
from agent_platform.token_monitoring import (
    collect_token_monitor_snapshot,
    snapshot_delta,
)


ROOT = Path(__file__).resolve().parents[1]
TASK_ID = "EXP-LILIES-001"
REVISION = 16
TASK_ROOT = (
    ROOT
    / "docs"
    / "experiments"
    / "lilies-collaboration"
    / TASK_ID
    / str(REVISION)
)
TASK_REVISIONS_ROOT = TASK_ROOT.parent
ENVIRONMENT_CONTROL = (
    ROOT
    / "scripts"
    / "experiments"
    / "exp_lilies_001"
    / "environment_control.py"
)
HOST_SNAPSHOT_VERIFIER = (
    ROOT
    / "scripts"
    / "experiments"
    / "exp_lilies_001"
    / "verify_host_snapshot.py"
)
DEFAULT_PLATFORM_PORT = 18100
DEFAULT_DAEMON_PORT = 18101
TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})
MAX_HTTP_BYTES = 32 * 1024 * 1024
PLATFORM_BRIDGE_SCOPES = (
    LocalScope.session_read.value,
    LocalScope.session_write.value,
    LocalScope.permission_resolve.value,
    LocalScope.credential_write.value,
)
OPERATIONAL_PERMISSION_POLICIES = ("manual", "task_local_workspace")
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


class EnterpriseExperimentError(RuntimeError):
    """The controlled EXP-LILIES-001 run could not advance safely."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_HTTP_BYTES + 1)
            if len(raw) > MAX_HTTP_BYTES:
                raise EnterpriseExperimentError("platform response exceeds the runner limit")
    except HTTPError as error:
        detail = error.read(4_096).decode("utf-8", errors="replace")
        raise EnterpriseExperimentError(
            f"platform request failed: {method} {path} -> {error.code}: {detail[:500]}"
        ) from error
    except (URLError, OSError, TimeoutError) as error:
        raise EnterpriseExperimentError(
            f"platform request failed: {method} {path}"
        ) from error
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


def _platform_environment(
    state_root: Path,
    secrets_state: Mapping[str, str],
    *,
    port: int,
    collaboration_policy: str,
    enable_model_egress: bool = False,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "API_TOKEN": secrets_state["platform_api_token"],
            "HOST": "127.0.0.1",
            "PORT": str(port),
            "DATA_DIR": str(state_root / "platform-data"),
            "WORKSPACE_ROOT": str(state_root / "platform-workspaces"),
            "MODEL_EGRESS_ENABLED": "true" if enable_model_egress else "false",
            "PLATFORM_HARNESS_SECRET_ENVELOPE_KEY": secrets_state[
                "platform_envelope_key"
            ],
            "LILIES_LOCAL_AGENT_ENABLED": "true",
            "LILIES_COLLABORATION_ENABLED": "true",
            "LILIES_COLLABORATION_DEVELOPER_TOKEN": secrets_state[
                "collaboration_developer_token"
            ],
            "LILIES_COLLABORATION_VERIFIER_TOKEN": secrets_state[
                "collaboration_verifier_token"
            ],
            "LILIES_FORMAL_HIDDEN_SEED_KEY": secrets_state[
                "formal_hidden_seed_key"
            ],
            "LILIES_COLLABORATIVE_DEVELOPMENT_ENABLED": "true",
            "LILIES_COLLABORATIVE_DEVELOPMENT_SIGNING_KEY": secrets_state[
                "collaborative_development_signing_key"
            ],
            "LILIES_AUTONOMOUS_COLLABORATION_ENABLED": (
                "true" if collaboration_policy == "auto_forward" else "false"
            ),
            "LILIES_PLATFORM_BASE_URL": f"http://127.0.0.1:{port}",
        }
    )
    return environment


def _task_max_turns() -> int:
    try:
        budget = BudgetSpec.model_validate_json((TASK_ROOT / "budget.json").read_bytes())
    except (OSError, ValueError) as error:
        raise EnterpriseExperimentError(
            "frozen task budget is unavailable or invalid"
        ) from error
    if budget.task_id != TASK_ID or budget.revision != REVISION:
        raise EnterpriseExperimentError("frozen task budget identity is invalid")
    return budget.max_build_repair_turns


def _daemon_environment(
    state_root: Path,
    *,
    port: int,
    enable_model_egress: bool = False,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "LILIES_DATA_DIR": str(state_root / "lilies-data"),
            "LILIES_WORKSPACE_ROOT": str(state_root / "lilies-workspaces"),
            "LILIES_HOST": "127.0.0.1",
            "LILIES_PORT": str(port),
            "LILIES_DEFAULT_MAX_TURNS": str(_task_max_turns()),
            "LILIES_MODEL_EGRESS_ENABLED": (
                "true" if enable_model_egress else "false"
            ),
        }
    )
    return environment


def _freeze_package(platform_data: Path) -> dict[str, Any]:
    manager = TaskPackageManager(platform_data / "task-packages")
    temporary = Path(tempfile.mkdtemp(prefix="lilies-t01h-runner-source-"))
    try:
        source_manager = TaskPackageManager(temporary)
        source = None
        for revision in range(1, REVISION + 1):
            source_root = (
                TASK_ROOT
                if revision == REVISION
                else TASK_REVISIONS_ROOT / str(revision)
            )
            source = source_manager.freeze_revision(source_root)
            if revision < REVISION:
                manager.freeze_revision(source_root)
        assert source is not None
        if manager.has_frozen_revision(TASK_ID, REVISION):
            package = manager.load_frozen(TASK_ID, REVISION)
            if (
                package.record.public_summary_digest
                != source.record.public_summary_digest
                or package.record.sealed_package_digest
                != source.record.sealed_package_digest
            ):
                raise EnterpriseExperimentError(
                    "run state already freezes another EXP-LILIES-001 "
                    f"revision-{REVISION} payload"
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
        "exp-lilies-001-environment-attestation": secrets_state.get(
            "attestation_secret"
        ),
        "exp-lilies-001-paperless-builder-token": credentials.get(
            "paperless_builder_token"
        ),
        "exp-lilies-001-inventree-builder-token": credentials.get(
            "inventree_builder_token"
        ),
        "exp-lilies-001-paperless-verifier-token": credentials.get(
            "paperless_verifier_token"
        ),
        "exp-lilies-001-inventree-verifier-token": credentials.get(
            "inventree_verifier_token"
        ),
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


def _pair_daemon(
    *,
    state_root: Path,
    daemon_port: int,
    platform_url: str,
    platform_token: str,
) -> dict[str, Any]:
    settings = LiliesSettings(
        data_dir=state_root / "lilies-data",
        workspace_root=state_root / "lilies-workspaces",
        host="127.0.0.1",
        port=daemon_port,
    )
    settings.prepare()
    pairing = LiliesClient(settings).create_pairing_code(PLATFORM_BRIDGE_SCOPES)
    status = _request_json(
        platform_url,
        "/api/v1/local-lilies/connections",
        method="POST",
        token=platform_token,
        value={
            "idempotency_key": f"{TASK_ID.lower()}.pair.{daemon_port:05d}",
            "base_url": f"http://127.0.0.1:{daemon_port}",
            "pairing_code": pairing["pairing_code"],
            "expected_daemon_fingerprint": pairing["daemon_fingerprint"],
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
    ]
    if len(connected) != 1:
        raise EnterpriseExperimentError("platform did not establish one exact daemon connection")
    return connected[0]


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
            "name": f"{TASK_ID} seed {seed}",
            "description": "Frozen real-host enterprise experiment",
            "requirement": "",
            "mode": "workflow",
            "delivery_mode": "guided",
            "governed_hard_gate": True,
        },
    )
    if not isinstance(application, dict) or not isinstance(application.get("id"), str):
        raise EnterpriseExperimentError("platform did not create an empty application")
    return application


def _start_formal_build(
    platform_url: str,
    platform_token: str,
    *,
    application_id: str,
    connection_id: str,
    seed: str,
) -> dict[str, Any]:
    result = _request_json(
        platform_url,
        f"/api/v1/local-lilies/applications/{application_id}/formal-builds",
        method="POST",
        token=platform_token,
        value={
            "idempotency_key": f"{TASK_ID.lower()}.formal.seed-{seed}.revision-{REVISION}",
            "connection_id": connection_id,
            "task_id": TASK_ID,
            "revision": REVISION,
            "environment_instance_id": f"{TASK_ID.lower()}:r{REVISION}:seed-{seed}",
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
        raise EnterpriseExperimentError(
            "formal collaboration channel inventory is invalid"
        )
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
        raise EnterpriseExperimentError(
            "unattended workspace permission path is not canonical"
        )
    path = PurePosixPath(value)
    if path.is_absolute():
        raise EnterpriseExperimentError(
            "unattended workspace permission path is not canonical"
        )
    parts = tuple(part for part in path.parts if part != ".")
    if not parts or any(part in {"", ".."} for part in parts):
        raise EnterpriseExperimentError(
            "unattended workspace permission path is not canonical"
        )
    denied = {item.casefold() for item in TASK_LOCAL_DENIED_SEGMENTS}
    if any(part.casefold() in denied for part in parts):
        raise EnterpriseExperimentError(
            "unattended workspace permission targets a denied segment"
        )
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
        raise EnterpriseExperimentError(
            "task-local permission idempotency bindings are invalid"
        )
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
        raise EnterpriseExperimentError(
            "formal collaboration channel inventory is invalid"
        )
    matching = [
        item
        for item in channels
        if isinstance(item, dict) and item.get("assignment_id") == assignment_id
    ]
    if len(matching) != 1 or not isinstance(matching[0].get("channel_id"), str):
        raise EnterpriseExperimentError(
            "formal collaboration channel is not uniquely visible"
        )
    detail = _request_json(
        platform_url,
        (
            "/api/v1/studio/collaboration/channels/"
            f"{matching[0]['channel_id']}"
        ),
        token=platform_token,
    )
    context = detail.get("context") if isinstance(detail, dict) else None
    context_assignment = (
        context.get("assignment") if isinstance(context, dict) else None
    )
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
        raise EnterpriseExperimentError(
            "formal collaboration permission timeline is invalid"
        )
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
        (seq, request)
        for seq, request in pending
        if request["request_id"] not in resolved
    ]
    if len(unresolved) != 1:
        raise EnterpriseExperimentError(
            "formal assignment does not expose one exact pending permission"
        )
    return dict(max(unresolved, key=lambda item: item[0])[1])


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
        (
            f"/api/v1/local-lilies/assignments/{assignment_id}/"
            f"permissions/{request_id}"
        ),
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
        raise EnterpriseExperimentError(
            "daemon returned an invalid unattended permission receipt"
        )
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
        if (
            relay_error is not None
            and "security_boundary_violation" in str(relay_error)
        ):
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
                        (
                            "/api/v1/local-lilies/assignments/"
                            f"{assignment_id}/relay"
                        ),
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
                            "pending permission synchronization failed: "
                            f"{error}"
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
) -> dict[str, Any]:
    try:
        return _poll_assignment_inner(
            platform_url,
            platform_token,
            assignment_id=assignment_id,
            deadline_seconds=deadline_seconds,
            operational_permission_policy=operational_permission_policy,
            token_state_root=token_state_root,
            token_monitor_interval=token_monitor_interval,
        )
    finally:
        if token_state_root is not None and token_monitor_interval > 0:
            observed_at = time.monotonic()
            _record_token_monitor_snapshot(
                token_state_root,
                previous=None,
                previous_at=observed_at,
                observed_at=observed_at,
            )


def _record_token_monitor_snapshot(
    state_root: Path,
    *,
    previous: dict[str, Any] | None,
    previous_at: float,
    observed_at: float,
) -> tuple[dict[str, Any], float]:
    snapshot = collect_token_monitor_snapshot(
        platform_db=state_root / "platform-data" / "agent_platform.db",
        lilies_db=state_root / "lilies-data" / "lilies.db",
        bridge_db=state_root / "platform-data" / "local-lilies-bridge.db",
        development_db=(
            state_root / "platform-data" / "collaborative-development.db"
        ),
        required_sources=("platform", "local_lilies", "bridge"),
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
        "observed_at": snapshot["generated_at"],
        "safety": snapshot["safety"],
        "totals": snapshot["usage"]["totals"],
        "by_stage": snapshot["usage"]["by_stage"],
        "by_model": snapshot["usage"]["by_model"],
        "delta": delta,
        "active": {
            "processes": snapshot["processes"],
            "platform_tasks": snapshot["sources"]["platform"]["active_tasks"],
            "local_sessions": snapshot["sources"]["local_lilies"]["active_sessions"],
            "recoverable_assignments": snapshot["sources"]["bridge"][
                "recoverable_assignments"
            ],
            "development_assignments": snapshot["sources"][
                "collaborative_development"
            ]["active_assignments"],
        },
    }
    monitor_root = state_root / "monitoring"
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
    print(
        "[token-monitor] "
        f"tokens={int(totals['tokens']):,} "
        f"delta={delta_tokens:+,} "
        f"calls={int(totals['model_calls']):,} "
        f"cost_usd={float(totals['cost_usd']):.6f}",
        file=sys.stderr,
        flush=True,
    )
    return snapshot, observed_at


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
        "last_error",
        "created_at",
        "updated_at",
        "runner_timeout",
        "runner_terminal",
        "runner_terminal_detail",
        "runner_auto_permissions",
    }
    return {key: value[key] for key in sorted(allowed) if key in value}


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
    snapshot_path = (
        state_root
        / "environment"
        / f"host-snapshot-{seed}-final.json"
    )
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
        / (
            f"host-verification-{seed}-"
            f"{verification_identity.removeprefix('sha256:')}.json"
        )
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
        or result.get("verdict")
        not in {"independently_verified", "verification_failed"}
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
        (
            f"/api/v1/local-lilies/assignments/{assignment_id}/"
            "independent-verification"
        ),
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
    verdict = verification.get("verdict")
    claim_status = result.get("claim_status")
    if (
        verdict not in {"independently_verified", "verification_failed"}
        or claim_status
        not in {"independently_verified", "verification_failed"}
    ):
        raise EnterpriseExperimentError(
            "platform independent verification has an invalid verdict"
        )
    differences = verification.get("differences")
    stable_progress = result.get("stable_progress")
    if not isinstance(stable_progress, dict):
        raise EnterpriseExperimentError(
            "platform independent verification omitted stable-seed progress"
        )
    stable_verdict = stable_progress.get("stable_verdict")
    if stable_verdict is not None and not isinstance(stable_verdict, dict):
        raise EnterpriseExperimentError(
            "platform stable-seed verdict has an invalid projection"
        )
    return {
        "claim_id": result.get("claim_id"),
        "claim_status": claim_status,
        "verification_id": verification.get("verification_id"),
        "verdict": verdict,
        "oracle_digest": verification.get("oracle_digest"),
        "difference_count": len(differences) if isinstance(differences, list) else None,
        "stable_hidden_runs": stable_progress.get("stable_hidden_runs"),
        "consecutive_passes": stable_progress.get("consecutive_passes"),
        "stable_progress_digest": stable_progress.get("progress_digest"),
        "stable_verdict": (
            None
            if stable_verdict is None
            else {
                "verdict": stable_verdict.get("verdict"),
                "qualification_digest": stable_verdict.get(
                    "qualification_digest"
                ),
                "verdict_digest": stable_verdict.get("verdict_digest"),
            }
        ),
    }


def _active_run_path(state_root: Path, seed: str) -> Path:
    return state_root / f"active-run-{seed}.json"


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
) -> None:
    _atomic_private_json(
        _active_run_path(state_root, seed),
        {
            "schema_version": "1.1",
            "task_id": TASK_ID,
            "revision": REVISION,
            "seed": seed,
            "collaboration_policy": collaboration_policy,
            "operational_permission_policy": operational_permission_policy,
            "platform_port": platform_port,
            "daemon_port": daemon_port,
            "application_id": application.get("id"),
            "connection_id": connection.get("connection_id"),
            "assignment_id": assignment.get("assignment_id"),
            "updated_at": _now(),
        },
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
    digest = attempt_id.removeprefix("sha256:")
    return evidence_root / "attempts" / f"seed-{seed}" / f"{digest}.json"


def _archive_latest_run_evidence(
    evidence_root: Path,
    *,
    seed: str,
    latest_path: Path,
) -> str | None:
    if not latest_path.exists():
        return None
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
        archive_path.write_bytes(raw)
    return attempt_id


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
    error: str | None,
    finished_at: str | None = None,
    model_egress_authorized: bool = False,
    token_monitor: Mapping[str, Any] | None = None,
) -> Path:
    evidence_root.mkdir(parents=True, exist_ok=True)
    path = evidence_root / f"seed-{seed}.json"
    previous_attempt_id = _archive_latest_run_evidence(
        evidence_root,
        seed=seed,
        latest_path=path,
    )
    attempt_id = _run_attempt_id(seed, started_at)
    value = {
        "schema_version": "v0.4.13-t01h-run-1",
        "stage_task_id": "V04-13-T01H",
        "experiment_task_id": TASK_ID,
        "revision": REVISION,
        "seed": seed,
        "attempt_id": attempt_id,
        "previous_attempt_id": previous_attempt_id,
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at or _now(),
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
        "assignment": (
            None if assignment is None else _safe_assignment_projection(assignment)
        ),
        "secret_receipts": list(secret_receipts),
        "host_snapshots": list(host_snapshots),
        "platform_verification": platform_verification,
        "host_verification": host_verification,
        "model_egress_authorized": model_egress_authorized,
        "token_monitor": token_monitor,
        "error": error,
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
        if attempt_path.read_bytes() != encoded:
            raise EnterpriseExperimentError(
                f"run attempt evidence conflicts with its immutable archive: {attempt_path}"
            )
    else:
        attempt_path.write_bytes(encoded)
    path.write_bytes(encoded)
    return path


def _token_monitor_evidence(
    state_root: Path,
    *,
    interval_seconds: float,
) -> dict[str, Any]:
    latest = state_root / "monitoring" / "token-monitor.latest.json"
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
    return {
        "status": "recorded",
        "interval_seconds": interval_seconds,
        "latest_digest": _digest(payload),
        "observed_at": value.get("observed_at"),
        "totals": value.get("totals"),
        "safety": value.get("safety"),
    }


def run_seed(args: argparse.Namespace) -> int:
    state_root = args.state_root.resolve()
    evidence_root = args.evidence_root.resolve()
    operational_permission_policy = str(
        getattr(args, "operational_permission_policy", "task_local_workspace")
    )
    if operational_permission_policy not in OPERATIONAL_PERMISSION_POLICIES:
        raise EnterpriseExperimentError("operational permission policy is invalid")
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    started_at = _now()
    runner_secrets = _runner_secrets(state_root, create=True)
    inherited_environment = os.environ.copy()
    package: dict[str, Any] | None = None
    application: dict[str, Any] | None = None
    connection: dict[str, Any] | None = None
    assignment: dict[str, Any] | None = None
    secret_receipts: list[dict[str, Any]] = []
    host_snapshots: list[dict[str, Any]] = []
    platform_verification: dict[str, Any] | None = None
    host_verification: dict[str, Any] | None = None
    try:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            raise EnterpriseExperimentError(
                "DEEPSEEK_API_KEY is required for a real Lilies model run"
            )
        if not getattr(args, "enable_model_egress", False):
            raise EnterpriseExperimentError(
                "real model egress remains disabled; pass --enable-model-egress "
                "for this authorized run"
            )
        if args.token_monitor_interval <= 0:
            raise EnterpriseExperimentError(
                "a paid formal run requires --token-monitor-interval greater than zero"
            )
        _environment_command(
            state_root,
            "config",
            environment=inherited_environment,
        )
        _environment_command(
            state_root,
            "reset",
            "--confirm-task-id",
            TASK_ID,
            environment=inherited_environment,
        )
        _environment_command(
            state_root,
            "up",
            environment=inherited_environment,
        )
        _environment_command(
            state_root,
            "initialize",
            environment=inherited_environment,
        )
        _environment_command(
            state_root,
            "seed",
            "--seed",
            args.seed,
            environment=inherited_environment,
        )
        _environment_command(
            state_root,
            "snapshot",
            "--seed",
            args.seed,
            "--phase",
            "baseline",
            environment=inherited_environment,
        )
        host_snapshots.append(
            _snapshot_summary(
                state_root
                / "environment"
                / f"host-snapshot-{args.seed}-baseline.json"
            )
        )

        platform_environment = _platform_environment(
            state_root,
            runner_secrets,
            port=args.platform_port,
            collaboration_policy=args.collaboration_policy,
            enable_model_egress=True,
        )
        daemon_environment = _daemon_environment(
            state_root,
            port=args.daemon_port,
            enable_model_egress=True,
        )
        platform_url = f"http://127.0.0.1:{args.platform_port}"
        daemon_url = f"http://127.0.0.1:{args.daemon_port}"
        package = _freeze_package(Path(platform_environment["DATA_DIR"]))

        with ExitStack() as stack:
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
                environment=inherited_environment,
                log_path=state_root / "logs" / f"boundary-seed-{args.seed}.log",
            )
            _managed_process(
                stack,
                (sys.executable, "-m", "agent_platform.cli"),
                environment=platform_environment,
                log_path=state_root / "logs" / f"platform-seed-{args.seed}.log",
            )
            _managed_process(
                stack,
                (
                    sys.executable,
                    "-m",
                    "agent_platform.lilies_cli",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(args.daemon_port),
                ),
                environment=daemon_environment,
                log_path=state_root / "logs" / f"lilies-seed-{args.seed}.log",
            )
            _wait_json(platform_url, "/health", timeout_seconds=60)
            _wait_json(daemon_url, "/local/v1/health", timeout_seconds=60)
            _wait_tcp("127.0.0.1", 18002, timeout_seconds=120)
            secret_receipts = _install_environment_secrets(
                platform_url,
                runner_secrets["platform_api_token"],
                _host_secrets(state_root),
            )
            connection = _pair_daemon(
                state_root=state_root,
                daemon_port=args.daemon_port,
                platform_url=platform_url,
                platform_token=runner_secrets["platform_api_token"],
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
            )
            if args.collaboration_policy == "auto_forward":
                _set_auto_forward(
                    platform_url,
                    runner_secrets["platform_api_token"],
                    assignment_id=str(assignment["assignment_id"]),
                )
            assignment = _poll_assignment(
                platform_url,
                runner_secrets["platform_api_token"],
                assignment_id=str(assignment["assignment_id"]),
                deadline_seconds=args.deadline_seconds,
                operational_permission_policy=operational_permission_policy,
                token_state_root=state_root,
                token_monitor_interval=args.token_monitor_interval,
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
            )
            _environment_command(
                state_root,
                "snapshot",
                "--seed",
                args.seed,
                "--phase",
                "final",
                environment=inherited_environment,
            )
            host_snapshots.append(
                _snapshot_summary(
                    state_root
                    / "environment"
                    / f"host-snapshot-{args.seed}-final.json"
                )
            )
            if str(assignment.get("phase")) == "completed":
                host_verification = _run_host_snapshot_verification(
                    state_root,
                    seed=args.seed,
                )
                if host_verification.get("verdict") == "independently_verified":
                    platform_verification = _run_platform_independent_verification(
                        platform_url,
                        runner_secrets["platform_api_token"],
                        assignment_id=str(assignment["assignment_id"]),
                    )
        phase = str(assignment.get("phase"))
        independently_verified = (
            platform_verification is not None
            and platform_verification.get("claim_status")
            == "independently_verified"
            and host_verification is not None
            and host_verification.get("verdict") == "independently_verified"
        )
        status = (
            "enterprise_run_passed"
            if phase == "completed" and independently_verified
            else "assignment_completed_verification_failed"
            if phase == "completed"
            else "assignment_builder_incomplete"
            if assignment.get("runner_terminal")
            == "builder_ready_without_completion_claim"
            else "assignment_unattended_permission_rejected"
            if assignment.get("runner_terminal")
            == "unattended_permission_rejected"
            else "assignment_relay_security_rejected"
            if assignment.get("runner_terminal")
            == "relay_security_boundary_rejected"
            else "assignment_waiting_user"
            if phase == "waiting"
            else "assignment_failed"
            if phase in {"failed", "cancelled"}
            else "assignment_timeout"
        )
        path = _write_run_evidence(
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
            model_egress_authorized=bool(
                getattr(args, "enable_model_egress", False)
            ),
            token_monitor=_token_monitor_evidence(
                state_root,
                interval_seconds=float(
                    getattr(args, "token_monitor_interval", 5.0)
                ),
            ),
        )
        print(path)
        return 0 if status == "enterprise_run_passed" else 3
    except Exception as error:
        path = _write_run_evidence(
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
            error=f"{type(error).__name__}: {error}",
            model_egress_authorized=bool(
                getattr(args, "enable_model_egress", False)
            ),
            token_monitor=_token_monitor_evidence(
                state_root,
                interval_seconds=float(
                    getattr(args, "token_monitor_interval", 5.0)
                ),
            ),
        )
        print(path, file=sys.stderr)
        print(str(error), file=sys.stderr)
        return 2


def resume_seed(args: argparse.Namespace) -> int:
    state_root = args.state_root.resolve()
    evidence_root = args.evidence_root.resolve()
    active = _read_private_json(_active_run_path(state_root, args.seed))
    if (
        active.get("schema_version") != "1.1"
        or active.get("task_id") != TASK_ID
        or active.get("revision") != REVISION
        or active.get("seed") != args.seed
    ):
        raise EnterpriseExperimentError("active run does not bind the requested seed")
    platform_port = int(active["platform_port"])
    daemon_port = int(active["daemon_port"])
    collaboration_policy = str(active["collaboration_policy"])
    operational_permission_policy = str(active["operational_permission_policy"])
    assignment_id = str(active["assignment_id"])
    application_id = str(active["application_id"])
    connection_id = str(active["connection_id"])
    if collaboration_policy not in {"manual", "auto_forward"}:
        raise EnterpriseExperimentError("active run has an invalid collaboration policy")
    if operational_permission_policy not in OPERATIONAL_PERMISSION_POLICIES:
        raise EnterpriseExperimentError(
            "active run has an invalid operational permission policy"
        )
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise EnterpriseExperimentError(
            "DEEPSEEK_API_KEY is required to resume the real Lilies model run"
        )
    if not getattr(args, "enable_model_egress", False):
        raise EnterpriseExperimentError(
            "real model egress remains disabled; pass --enable-model-egress "
            "for this authorized resume"
        )
    if args.token_monitor_interval <= 0:
        raise EnterpriseExperimentError(
            "a paid formal resume requires --token-monitor-interval greater than zero"
        )
    runner_secrets = _runner_secrets(state_root, create=False)
    inherited_environment = os.environ.copy()
    _environment_command(
        state_root,
        "up",
        environment=inherited_environment,
    )
    platform_environment = _platform_environment(
        state_root,
        runner_secrets,
        port=platform_port,
        collaboration_policy=collaboration_policy,
        enable_model_egress=True,
    )
    daemon_environment = _daemon_environment(
        state_root,
        port=daemon_port,
        enable_model_egress=True,
    )
    package = _freeze_package(Path(platform_environment["DATA_DIR"]))
    platform_url = f"http://127.0.0.1:{platform_port}"
    daemon_url = f"http://127.0.0.1:{daemon_port}"
    assignment: dict[str, Any] | None = None
    host_snapshots: list[dict[str, Any]] = []
    platform_verification: dict[str, Any] | None = None
    host_verification: dict[str, Any] | None = None
    baseline_path = (
        state_root
        / "environment"
        / f"host-snapshot-{args.seed}-baseline.json"
    )
    if baseline_path.exists():
        host_snapshots.append(_snapshot_summary(baseline_path))
    try:
        with ExitStack() as stack:
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
                environment=inherited_environment,
                log_path=state_root / "logs" / f"boundary-seed-{args.seed}.log",
            )
            _managed_process(
                stack,
                (sys.executable, "-m", "agent_platform.cli"),
                environment=platform_environment,
                log_path=state_root / "logs" / f"platform-seed-{args.seed}.log",
            )
            _managed_process(
                stack,
                (
                    sys.executable,
                    "-m",
                    "agent_platform.lilies_cli",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(daemon_port),
                ),
                environment=daemon_environment,
                log_path=state_root / "logs" / f"lilies-seed-{args.seed}.log",
            )
            _wait_json(platform_url, "/health", timeout_seconds=60)
            _wait_json(daemon_url, "/local/v1/health", timeout_seconds=60)
            _wait_tcp("127.0.0.1", 18002, timeout_seconds=120)
            _request_json(
                platform_url,
                f"/api/v1/local-lilies/connections/{connection_id}/refresh",
                method="POST",
                token=runner_secrets["platform_api_token"],
            )
            assignment = _request_json(
                platform_url,
                f"/api/v1/local-lilies/assignments/{assignment_id}/resume",
                method="POST",
                token=runner_secrets["platform_api_token"],
                timeout=180.0,
            )
            if not isinstance(assignment, dict):
                raise EnterpriseExperimentError("resume returned an invalid assignment")
            if collaboration_policy == "auto_forward":
                _set_auto_forward(
                    platform_url,
                    runner_secrets["platform_api_token"],
                    assignment_id=assignment_id,
                )
            assignment = _poll_assignment(
                platform_url,
                runner_secrets["platform_api_token"],
                assignment_id=assignment_id,
                deadline_seconds=args.deadline_seconds,
                operational_permission_policy=operational_permission_policy,
                token_state_root=state_root,
                token_monitor_interval=args.token_monitor_interval,
            )
            _environment_command(
                state_root,
                "snapshot",
                "--seed",
                args.seed,
                "--phase",
                "final",
                environment=inherited_environment,
            )
            host_snapshots.append(
                _snapshot_summary(
                    state_root
                    / "environment"
                    / f"host-snapshot-{args.seed}-final.json"
                )
            )
            if str(assignment.get("phase")) == "completed":
                host_verification = _run_host_snapshot_verification(
                    state_root,
                    seed=args.seed,
                )
                if host_verification.get("verdict") == "independently_verified":
                    platform_verification = _run_platform_independent_verification(
                        platform_url,
                        runner_secrets["platform_api_token"],
                        assignment_id=assignment_id,
                    )
        application = {"id": application_id}
        connection = {
            "connection_id": connection_id,
            "base_url": daemon_url,
            "status": "connected",
        }
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
        )
        phase = str(assignment.get("phase"))
        independently_verified = (
            platform_verification is not None
            and platform_verification.get("claim_status")
            == "independently_verified"
            and host_verification is not None
            and host_verification.get("verdict") == "independently_verified"
        )
        status = (
            "enterprise_run_passed"
            if phase == "completed" and independently_verified
            else "assignment_completed_verification_failed"
            if phase == "completed"
            else "assignment_builder_incomplete"
            if assignment.get("runner_terminal")
            == "builder_ready_without_completion_claim"
            else "assignment_unattended_permission_rejected"
            if assignment.get("runner_terminal")
            == "unattended_permission_rejected"
            else "assignment_relay_security_rejected"
            if assignment.get("runner_terminal")
            == "relay_security_boundary_rejected"
            else "assignment_waiting_user"
            if phase == "waiting"
            else "assignment_failed"
            if phase in {"failed", "cancelled"}
            else "assignment_timeout"
        )
        path = _write_run_evidence(
            evidence_root,
            seed=args.seed,
            started_at=str(active.get("updated_at") or _now()),
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
            model_egress_authorized=bool(
                getattr(args, "enable_model_egress", False)
            ),
            token_monitor=_token_monitor_evidence(
                state_root,
                interval_seconds=float(
                    getattr(args, "token_monitor_interval", 5.0)
                ),
            ),
        )
        print(path)
        return 0 if status == "enterprise_run_passed" else 3
    except Exception as error:
        path = _write_run_evidence(
            evidence_root,
            seed=args.seed,
            started_at=str(active.get("updated_at") or _now()),
            status="resume_failed",
            package=package,
            application={"id": application_id},
            connection={
                "connection_id": connection_id,
                "base_url": daemon_url,
            },
            assignment=assignment,
            secret_receipts=[],
            host_snapshots=host_snapshots,
            platform_verification=platform_verification,
            host_verification=host_verification,
            error=f"{type(error).__name__}: {error}",
            model_egress_authorized=bool(
                getattr(args, "enable_model_egress", False)
            ),
            token_monitor=_token_monitor_evidence(
                state_root,
                interval_seconds=float(
                    getattr(args, "token_monitor_interval", 5.0)
                ),
            ),
        )
        print(path, file=sys.stderr)
        print(str(error), file=sys.stderr)
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
