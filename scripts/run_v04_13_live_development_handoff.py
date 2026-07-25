#!/usr/bin/env python3
"""Run one real Codex -> Lilies handoff through the durable T01G lifecycle.

This is deliberately not a model-to-model transcript shortcut.  A provider-only
Codex invocation sees one metered source projection but no mounted workspace or
shell; a trusted, role-scoped tool service applies its structured proposal and
submits a strict DevelopmentResult through the autonomous worker.  Lilies then
receives a separately materialized review snapshot and must use the development
tools to inspect the source, inspect the diff, and execute the frozen acceptance
command before its review can be accepted by the service.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent_platform.agent_core import collect_model_stream  # noqa: E402
from agent_platform.collaboration_qualification import (  # noqa: E402
    canonical_digest,
    qualification_source_revision,
)
from agent_platform.collaborative_development_auth import (  # noqa: E402
    DevelopmentPrincipal,
)
from agent_platform.collaborative_development_dispatcher import (  # noqa: E402
    CollaborativeDevelopmentDispatchJournal,
    DispatchOutcome,
    DispatchOutcomeStatus,
    RoleBoundDispatchContext,
)
from agent_platform.collaborative_development_models import (  # noqa: E402
    AcceptanceCheck,
    AgentRole,
    AgentRoleGrant,
    ApprovalMode,
    CommandReceipt,
    DevelopmentAssignment,
    DevelopmentBudget,
    DevelopmentResult,
    DevelopmentTaskRole,
    DevelopmentWorkItem,
    ExecutionMode,
    LiliesReview,
    ReviewVerdict,
    SideEffect,
    TestReceipt,
    WorkItemKind,
    WorkItemStatus,
    utc_now,
)
from agent_platform.collaborative_development_provider import (  # noqa: E402
    BoundDevelopmentProviderCapability,
    normalized_provider_endpoint_host,
)
from agent_platform.collaborative_development_service import (  # noqa: E402
    CollaborativeDevelopmentService,
)
from agent_platform.collaborative_development_storage import (  # noqa: E402
    CollaborativeDevelopmentStore,
    TrustedProviderCostAuthorization,
    TrustedProviderCostReceipt,
)
from agent_platform.collaborative_development_worker import (  # noqa: E402
    AutonomousDevelopmentLifecycleBridge,
    AutonomousHandlerCompletion,
    run_dispatch_worker,
)
from agent_platform.development_workspace_broker import (  # noqa: E402
    DevelopmentReviewSnapshotReceipt,
    DevelopmentWorkspaceBroker,
    DevelopmentWorkspaceSpec,
)
from agent_platform.lilies_config import LiliesSettings  # noqa: E402
from agent_platform.lilies_development_tools import (  # noqa: E402
    DevelopmentToolAuthority,
    DevelopmentToolName,
    DevelopmentWorkspaceTools,
    GitDiffRequest,
    ProcessRunRequest,
    ProcessRunResult,
    WorkspaceReadRequest,
    WorkspacePatchRequest,
    WorkspaceSearchRequest,
)
from agent_platform.lilies_identity import (  # noqa: E402
    build_lilies_development_system_prompt,
)
from agent_platform.models import (  # noqa: E402
    ChatMessage,
    ContentBlock,
    ToolDefinition,
)
from agent_platform.providers.deepseek import DeepSeekProvider  # noqa: E402


CODEX_MODEL = "gpt-5.6-terra"
CODEX_PROVIDER = "openai-codex-cli"
DEEPSEEK_SECRET_REF = "deepseek-runtime-credential"
DEEPSEEK_CREDENTIAL_IDENTITY = "deepseek-api-account"
CODEX_SECRET_REF = "codex-cli-session"
CODEX_SUBSCRIPTION_IDENTITY = "codex-cli-subscription"
CODEX_ALLOWED_PROVIDER_HOSTS = (
    "api.openai.com",
    "auth.openai.com",
    "chatgpt.com",
)
_MACOS_SANDBOX = "/usr/bin/sandbox-exec"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LiveAcceptanceCheck(_StrictModel):
    criterion: str = Field(min_length=1, max_length=2_000)
    passed: bool
    evidence: str = Field(min_length=1, max_length=5_000)


class _AllowlistedConnectProxy:
    """A loopback CONNECT proxy that records and enforces exact TLS hosts."""

    def __init__(self, allowed_hosts: tuple[str, ...]) -> None:
        self.allowed_hosts = frozenset(
            host.strip().casefold() for host in allowed_hosts
        )
        self._records: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> _AllowlistedConnectProxy:
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                owner._handle(self)

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = Server(("127.0.0.1", 0), Handler)
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="t01g-codex-provider-proxy",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
        self._server = None
        self._thread = None

    @property
    def port(self) -> int:
        if self._server is None:
            raise RuntimeError("provider proxy is not running")
        return int(self._server.server_address[1])

    def observations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(record) for record in self._records]

    def _record(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._records.append(record)

    @staticmethod
    def _reject(
        handler: socketserver.StreamRequestHandler,
        status: bytes,
    ) -> None:
        handler.wfile.write(status + b"\r\nConnection: close\r\n\r\n")
        handler.wfile.flush()

    def _handle(self, handler: socketserver.StreamRequestHandler) -> None:
        handler.connection.settimeout(30)
        request_line = handler.rfile.readline(8_193)
        if len(request_line) > 8_192:
            self._reject(handler, b"HTTP/1.1 431 Request Header Fields Too Large")
            return
        header_bytes = len(request_line)
        for _ in range(64):
            line = handler.rfile.readline(8_193)
            header_bytes += len(line)
            if len(line) > 8_192 or header_bytes > 65_536:
                self._reject(
                    handler,
                    b"HTTP/1.1 431 Request Header Fields Too Large",
                )
                return
            if line in {b"\r\n", b"\n", b""}:
                break
        try:
            method, target, _ = request_line.decode("ascii").strip().split()
            raw_host, raw_port = target.rsplit(":", 1)
            host = raw_host.strip("[]").casefold()
            port = int(raw_port)
        except (UnicodeDecodeError, ValueError):
            self._record(
                {
                    "method": "invalid",
                    "host": "<invalid>",
                    "port": 0,
                    "allowed": False,
                }
            )
            self._reject(handler, b"HTTP/1.1 400 Bad Request")
            return
        allowed = (
            method.upper() == "CONNECT"
            and host in self.allowed_hosts
            and port == 443
        )
        record: dict[str, Any] = {
            "method": method.upper(),
            "host": host,
            "port": port,
            "allowed": allowed,
            "client_to_provider_bytes": 0,
            "provider_to_client_bytes": 0,
        }
        if not allowed:
            self._record(record)
            self._reject(handler, b"HTTP/1.1 403 Forbidden")
            return
        try:
            upstream = socket.create_connection((host, port), timeout=20)
        except OSError:
            record["upstream_connected"] = False
            self._record(record)
            self._reject(handler, b"HTTP/1.1 502 Bad Gateway")
            return
        record["upstream_connected"] = True
        handler.wfile.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        handler.wfile.flush()
        client = handler.connection
        client.setblocking(False)
        upstream.setblocking(False)
        sockets = (client, upstream)
        idle_deadline = time.monotonic() + 120
        try:
            while time.monotonic() < idle_deadline:
                readable, _, exceptional = select.select(
                    sockets,
                    (),
                    sockets,
                    0.5,
                )
                if exceptional:
                    break
                if not readable:
                    continue
                for source in readable:
                    try:
                        chunk = source.recv(64 * 1024)
                    except (BlockingIOError, ConnectionResetError, OSError):
                        chunk = b""
                    if not chunk:
                        return
                    target_socket = upstream if source is client else client
                    try:
                        target_socket.sendall(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    counter = (
                        "client_to_provider_bytes"
                        if source is client
                        else "provider_to_client_bytes"
                    )
                    record[counter] += len(chunk)
                    idle_deadline = time.monotonic() + 120
        finally:
            upstream.close()
            self._record(record)


class LiveLiliesDecision(_StrictModel):
    verdict: Literal["accepted", "rework"]
    acceptance_checks: list[LiveAcceptanceCheck] = Field(
        min_length=1,
        max_length=20,
    )
    summary: str = Field(min_length=1, max_length=5_000)
    limitations: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def verdict_matches_checks(self) -> LiveLiliesDecision:
        passed = all(item.passed for item in self.acceptance_checks)
        if (self.verdict == "accepted") != passed:
            raise ValueError("verdict must agree with all acceptance checks")
        return self


def _normalized_live_review_verdict(value: Any) -> Literal["accepted", "rework"] | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    exact = {
        "accept": "accepted",
        "accepted": "accepted",
        "pass": "accepted",
        "passed": "accepted",
        "needs_rework": "rework",
        "needs rework": "rework",
        "rework": "rework",
        "fail": "rework",
        "failed": "rework",
    }.get(normalized)
    if exact is not None:
        return exact
    if re.match(r"^(?:accept(?:ed)?|pass(?:ed)?)(?:\b|\s*[—:;-])", normalized):
        return "accepted"
    if re.match(r"^(?:needs[_ ]rework|rework|fail(?:ed)?)(?:\b|\s*[—:;-])", normalized):
        return "rework"
    return None


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _fixture(root: Path) -> tuple[Path, str]:
    source = root / "source"
    (source / "src").mkdir(parents=True)
    (source / "tests").mkdir()
    (source / "src" / "mathlib.py").write_text(
        "def add(left: int, right: int) -> int:\n"
        '    """Return the sum of two integers."""\n'
        "    return left - right\n",
        encoding="utf-8",
    )
    (source / "tests" / "check.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.dont_write_bytecode = True\n"
        "sys.path.insert(0, str(Path(__file__).parents[1]))\n"
        "from src.mathlib import add\n"
        "assert add(2, 3) == 5\n"
        "assert add(-2, 2) == 0\n"
        "print('2 acceptance checks passed')\n",
        encoding="utf-8",
    )
    _git(source, "init")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "config", "user.name", "T01G Live Fixture")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "frozen unrelated fixture baseline")
    baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
    return source, baseline


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            stripped = "\n".join(lines[1:-1])
    value = json.loads(stripped)
    if not isinstance(value, dict):
        raise ValueError("Lilies review response was not an object")
    return value


def _sanitize_evidence_text(
    value: Any,
    *,
    replacements: tuple[tuple[str, str], ...],
) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize_evidence_text(
                child,
                replacements=replacements,
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _sanitize_evidence_text(child, replacements=replacements)
            for child in value
        ]
    if isinstance(value, str):
        sanitized = value
        for raw, replacement in replacements:
            if raw:
                sanitized = sanitized.replace(raw, replacement)
        return sanitized
    return value


def _prepare_isolated_codex_identity(
    runtime_root: Path,
) -> tuple[Path, Path, dict[str, Any]]:
    """Copy only a safe ChatGPT-subscription identity into the sandbox."""

    source_home = Path(
        os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    ).expanduser()
    source_auth = source_home / "auth.json"
    try:
        lexical_metadata = source_auth.lstat()
    except OSError as error:
        raise RuntimeError("Codex CLI authentication is unavailable") from error
    source_mode = stat.S_IMODE(lexical_metadata.st_mode)
    if (
        not stat.S_ISREG(lexical_metadata.st_mode)
        or lexical_metadata.st_nlink != 1
        or source_mode & 0o077
        or source_mode & 0o111
        or not source_mode & stat.S_IRUSR
    ):
        raise RuntimeError(
            "Codex CLI authentication must be an owner-only non-symlink regular file"
        )

    open_flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source_auth, open_flags)
    except OSError as error:
        raise RuntimeError(
            "Codex CLI authentication must be an owner-only non-symlink regular file"
        ) from error
    try:
        opened_metadata = os.fstat(descriptor)
        if (
            opened_metadata.st_dev != lexical_metadata.st_dev
            or opened_metadata.st_ino != lexical_metadata.st_ino
        ):
            raise RuntimeError("Codex CLI authentication changed while it was opened")
        encoded_parts: list[bytes] = []
        encoded_size = 0
        while encoded_size <= 1024 * 1024:
            chunk = os.read(
                descriptor,
                min(64 * 1024, 1024 * 1024 + 1 - encoded_size),
            )
            if not chunk:
                break
            encoded_parts.append(chunk)
            encoded_size += len(chunk)
        encoded_auth = b"".join(encoded_parts)
        finished_metadata = os.fstat(descriptor)
        if (
            finished_metadata.st_dev != opened_metadata.st_dev
            or finished_metadata.st_ino != opened_metadata.st_ino
            or finished_metadata.st_size != opened_metadata.st_size
            or finished_metadata.st_mtime_ns != opened_metadata.st_mtime_ns
            or finished_metadata.st_ctime_ns != opened_metadata.st_ctime_ns
        ):
            raise RuntimeError("Codex CLI authentication changed while it was read")
    finally:
        os.close(descriptor)
    if len(encoded_auth) > 1024 * 1024:
        raise RuntimeError("Codex CLI authentication exceeds the safety limit")
    try:
        auth_payload = json.loads(encoded_auth)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("Codex CLI authentication is invalid") from error
    if not isinstance(auth_payload, dict):
        raise RuntimeError("Codex CLI authentication is invalid")
    tokens = auth_payload.get("tokens")
    if (
        auth_payload.get("auth_mode") != "chatgpt"
        or bool(auth_payload.get("OPENAI_API_KEY"))
        or not isinstance(tokens, dict)
        or not tokens
    ):
        raise RuntimeError(
            "Codex live handoff requires ChatGPT subscription authentication "
            "without an API key"
        )

    billing_metadata = {
        "auth_mode": "chatgpt",
        "api_key_present": False,
        "tokens_present": True,
        "billing_mode": "chatgpt_subscription",
        "credential_identity": CODEX_SUBSCRIPTION_IDENTITY,
    }
    codex_home = runtime_root / "codex-home"
    user_home = runtime_root / "user-home"
    codex_home.mkdir(mode=0o700)
    user_home.mkdir(mode=0o700)
    auth = codex_home / "auth.json"
    destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        destination_flags |= os.O_CLOEXEC
    destination = os.open(auth, destination_flags, 0o600)
    try:
        written = 0
        while written < len(encoded_auth):
            written += os.write(destination, encoded_auth[written:])
        os.fsync(destination)
    finally:
        os.close(destination)
    return codex_home, user_home, billing_metadata


def _clean_codex_environment(
    *,
    codex_home: Path,
    user_home: Path,
    temporary_directory: Path,
    proxy_port: int,
) -> tuple[dict[str, str], list[str]]:
    """Expose only an isolated auth copy and the exact loopback proxy."""

    proxy = f"http://127.0.0.1:{proxy_port}"
    environment: dict[str, str] = {
        "ALL_PROXY": proxy,
        "CODEX_HOME": str(codex_home),
        "HOME": str(user_home),
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "NO_COLOR": "1",
        "NO_PROXY": "",
        "PATH": os.defpath,
        "TERM": "dumb",
        "TMPDIR": str(temporary_directory),
        "all_proxy": proxy,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "no_proxy": "",
    }
    for name in ("SSL_CERT_DIR", "SSL_CERT_FILE"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment, sorted(environment)


def _sandbox_literal(value: Path | str) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _sandboxed_codex_argv(
    *,
    executable: Path,
    argv: tuple[str, ...],
    runtime_root: Path,
    proxy_port: int,
) -> tuple[str, ...]:
    sandbox = Path(_MACOS_SANDBOX)
    if not sandbox.is_file() or not os.access(sandbox, os.X_OK):
        raise RuntimeError("Codex live handoff requires macOS Seatbelt")
    resolved_executable = executable.resolve(strict=True)
    read_paths = {
        Path("/System"),
        Path("/usr"),
        Path("/bin"),
        Path("/sbin"),
        Path("/Library/Apple"),
        Path("/Library/Developer/CommandLineTools"),
        # Newer Codex CLI builds probe the system requirements policy before
        # loading the isolated CODEX_HOME.  Permit only that exact policy path
        # (and its macOS /etc symlink target), including when the file is
        # absent, so Seatbelt reports the real filesystem result instead of a
        # misleading permission failure.
        Path("/etc/codex/requirements.toml"),
        Path("/private/etc/codex/requirements.toml"),
        resolved_executable,
        resolved_executable.parent,
        runtime_root,
    }
    for name in ("SSL_CERT_DIR", "SSL_CERT_FILE"):
        configured = os.environ.get(name)
        if configured:
            read_paths.add(Path(configured).expanduser().resolve())
    metadata_paths: set[Path] = {Path("/")}
    for path in read_paths:
        metadata_paths.add(path)
        metadata_paths.update(path.parents)
    read_filters = "\n".join(
        f"  (subpath {_sandbox_literal(path)})"
        for path in sorted(read_paths, key=str)
    )
    metadata_filters = "\n".join(
        f"  (literal {_sandbox_literal(path)})"
        for path in sorted(metadata_paths, key=str)
    )
    profile = "\n".join(
        (
            "(version 1)",
            "(deny default)",
            (
                "(allow process-exec "
                f"(literal {_sandbox_literal(resolved_executable)}))"
            ),
            "(allow process-info*)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            "(allow mach-lookup)",
            "(allow ipc-posix-shm-read*)",
            "(allow file-read-metadata",
            metadata_filters,
            ")",
            "(allow file-read*",
            read_filters,
            '  (literal "/")',
            '  (literal "/dev/null")',
            '  (literal "/dev/urandom")',
            '  (literal "/private/var/db/timezone/zoneinfo/UTC"))',
            (
                "(allow file-write* "
                f"(subpath {_sandbox_literal(runtime_root)}) "
                '(literal "/dev/null"))'
            ),
            (
                "(allow network-outbound "
                f'(remote ip "localhost:{proxy_port}"))'
            ),
        )
    )
    return (str(sandbox), "-p", profile, "--", *argv)


def _codex_jsonl_summary(stdout: bytes) -> tuple[str, dict[str, Any]]:
    final_messages: list[str] = []
    command_records: list[dict[str, Any]] = []
    file_change_events = 0
    event_count = 0
    usage: dict[str, int] | None = None
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        event_count += 1
        if event.get("type") == "turn.completed":
            candidate = event.get("usage")
            if isinstance(candidate, dict):
                usage = {
                    key: int(candidate.get(key, 0))
                    for key in (
                        "input_tokens",
                        "cached_input_tokens",
                        "cache_write_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    )
                }
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "agent_message" and isinstance(item.get("text"), str):
            final_messages.append(item["text"])
        elif item_type == "reasoning":
            continue
        elif item_type == "command_execution":
            command = item.get("command")
            command_text = command if isinstance(command, str) else ""
            lowered = command_text.casefold()
            if any(
                forbidden in lowered
                for forbidden in (
                    "curl ",
                    "wget ",
                    "http://",
                    "https://",
                    "ssh ",
                    "scp ",
                )
            ):
                raise RuntimeError("Codex attempted an undeclared network command")
            command_records.append(
                {
                    "command_digest": canonical_digest(command_text),
                    "exit_code": item.get("exit_code"),
                    "status": item.get("status"),
                }
            )
            file_change_events += 1
        else:
            file_change_events += 1
        if item_type not in {"agent_message", "reasoning"}:
            normalized_type = item_type if isinstance(item_type, str) else "<missing>"
            raise RuntimeError(
                "Codex provider-only stream emitted undeclared item type: "
                f"{normalized_type}"
            )
    final_message = final_messages[-1] if final_messages else ""
    if usage is None:
        raise RuntimeError("Codex event stream omitted trusted token usage")
    return final_message, {
        "event_count": event_count,
        "command_count": len(command_records),
        "commands": command_records,
        "file_or_external_tool_events": file_change_events,
        "usage": usage,
        "event_stream_digest": _sha256_bytes(stdout),
    }


def _provider_proxy_observations_stay_fenced(
    observations: list[dict[str, Any]],
) -> bool:
    """Accept denied discovery probes only when the proxy blocked them fully."""

    allowed = [item for item in observations if item.get("allowed") is True]
    denied = [item for item in observations if item.get("allowed") is not True]
    return bool(allowed) and all(
        item.get("upstream_connected") is True
        and item.get("host") in CODEX_ALLOWED_PROVIDER_HOSTS
        for item in allowed
    ) and all(
        item.get("upstream_connected") is not True
        and item.get("client_to_provider_bytes") == 0
        and item.get("provider_to_client_bytes") == 0
        for item in denied
    )


def _proposal_is_exact_arithmetic_repair(
    proposal: dict[str, Any],
    *,
    source_text: str,
) -> bool:
    # workspace_read is line-oriented and omits the file's final newline.
    # Reconstruct that delimiter before validating a patch that must include it.
    observed_source = source_text if source_text.endswith("\n") else source_text + "\n"
    old_string = proposal.get("old_string")
    new_string = proposal.get("new_string")
    if (
        not isinstance(old_string, str)
        or not isinstance(new_string, str)
        or not old_string.endswith("\n")
        or not new_string.endswith("\n")
        or observed_source.count(old_string) != 1
    ):
        return False
    expected = observed_source.replace(
        "    return left - right\n",
        "    return left + right\n",
        1,
    )
    return (
        expected != observed_source
        and observed_source.replace(old_string, new_string, 1) == expected
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.poll() is None:
            process.kill()


def _communicate_codex_with_cancellation(
    process: subprocess.Popen[bytes],
    *,
    request: bytes,
    timeout_seconds: int,
    cancel_event: Any,
) -> tuple[bytes, bytes]:
    """Communicate while a watcher enforces the assignment cancellation fence."""

    communication_finished = threading.Event()
    cancellation_observed = threading.Event()

    def watch_cancellation() -> None:
        while not communication_finished.wait(0.02):
            if cancel_event.is_set():
                cancellation_observed.set()
                _kill_process_group(process)
                return

    watcher = threading.Thread(
        target=watch_cancellation,
        name="t01g-codex-cancellation-watch",
        daemon=True,
    )
    watcher.start()
    try:
        try:
            stdout, stderr = process.communicate(
                input=request,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            process.communicate(timeout=5)
            raise RuntimeError(
                "Codex implementer exceeded its time limit"
            ) from None
    finally:
        communication_finished.set()
        watcher.join(timeout=1)
    if cancellation_observed.is_set() or cancel_event.is_set():
        if process.poll() is None:
            _kill_process_group(process)
            process.communicate(timeout=5)
        raise RuntimeError("Codex implementer crossed a cancelled assignment boundary")
    return stdout, stderr


def _run_codex(
    *,
    executable: str,
    argv: tuple[str, ...],
    model_workspace: Path,
    runtime_root: Path,
    source_text: str,
    timeout_seconds: int,
    cancel_event: Any,
) -> dict[str, Any]:
    prompt = (
        "You are Codex acting only as the implementer in an explicitly authorized "
        "collaborative-development assignment. You have no workspace or shell tools in "
        "this model invocation. Do not call commands, tools, web search, or MCP. Review "
        "the supplied synthetic file content and propose one bounded string replacement "
        "that makes add() perform arithmetic addition. The trusted worker, not you, will "
        "apply the replacement and run the frozen tests. Include the trailing newline in "
        "both replacement strings. Return only the requested JSON.\n\n"
        "Relative file: src/mathlib.py\n"
        f"Current UTF-8 content:\n{source_text}"
    )
    codex_home, user_home, billing_metadata = _prepare_isolated_codex_identity(
        runtime_root
    )
    temporary_directory = runtime_root / "tmp"
    temporary_directory.mkdir(mode=0o700)
    with _AllowlistedConnectProxy(CODEX_ALLOWED_PROVIDER_HOSTS) as proxy:
        environment, environment_keys = _clean_codex_environment(
            codex_home=codex_home,
            user_home=user_home,
            temporary_directory=temporary_directory,
            proxy_port=proxy.port,
        )
        sandboxed_argv = _sandboxed_codex_argv(
            executable=Path(executable),
            argv=argv,
            runtime_root=runtime_root,
            proxy_port=proxy.port,
        )
        started = time.perf_counter()
        process = subprocess.Popen(
            sandboxed_argv,
            cwd=model_workspace,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout, stderr = _communicate_codex_with_cancellation(
            process,
            request=prompt.encode("utf-8"),
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
    proxy_observations = proxy.observations()
    final_message, stream_summary = _codex_jsonl_summary(stdout)
    if process.returncode != 0:
        raise RuntimeError(
            "Codex provider invocation failed inside the frozen sandbox"
        )
    if (
        stream_summary["command_count"] != 0
        or stream_summary["file_or_external_tool_events"] != 0
    ):
        raise RuntimeError(
            "Codex provider-only invocation attempted an undeclared tool"
        )
    if not _provider_proxy_observations_stay_fenced(proxy_observations):
        raise RuntimeError("Codex provider traffic escaped the exact host allowlist")
    proposal = _extract_json(final_message)
    if set(proposal) != {"old_string", "new_string", "summary"}:
        raise RuntimeError("Codex proposal did not match the frozen patch schema")
    if not _proposal_is_exact_arithmetic_repair(
        proposal,
        source_text=source_text,
    ):
        raise RuntimeError("Codex proposal was not the bounded arithmetic repair")
    if not isinstance(proposal["summary"], str) or not proposal["summary"].strip():
        raise RuntimeError("Codex proposal omitted its factual summary")
    return {
        "argv": [Path(executable).name, *argv[1:]],
        "exit_code": process.returncode,
        "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
        "stdout_digest": _sha256_bytes(stdout),
        "stderr_digest": _sha256_bytes(stderr),
        "last_message_digest": canonical_digest(final_message),
        "clean_environment_keys": environment_keys,
        "inherited_full_environment": False,
        "provider": CODEX_PROVIDER,
        "model": CODEX_MODEL,
        "billing_mode": "subscription_no_per_request_charge_reported",
        "credential_billing": billing_metadata,
        "workspace_supplied_to_model_process": False,
        "outer_filesystem_sandbox": "macos-seatbelt",
        "provider_proxy": {
            "transport": "loopback-connect-proxy",
            "allowed_hosts": list(CODEX_ALLOWED_PROVIDER_HOSTS),
            "observations": proxy_observations,
            "denied_connections": sum(
                not item.get("allowed") for item in proxy_observations
            ),
        },
        "proposal_digest": canonical_digest(proposal),
        "_proposal": proposal,
        **stream_summary,
    }


def _command_receipt(
    result: ProcessRunResult,
    *,
    started_at: Any,
    finished_at: Any,
) -> CommandReceipt:
    if result.exit_code is None:
        exit_code = 124
    else:
        exit_code = result.exit_code
    return CommandReceipt(
        argv=result.argv,
        cwd=result.cwd,
        exit_code=exit_code,
        output_digest=result.output_digest,
        started_at=started_at,
        finished_at=finished_at,
    )


def _tool_definition(
    *,
    name: str,
    description: str,
    schema: type[BaseModel],
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=schema.model_json_schema(),
    )


async def _execute_lilies_tool(
    *,
    block: ContentBlock,
    tools: DevelopmentWorkspaceTools,
) -> tuple[ContentBlock, dict[str, Any], ProcessRunResult | None]:
    tool_id = block.id
    if not tool_id or not block.name:
        raise RuntimeError("Lilies emitted an unbound development tool call")
    process_result: ProcessRunResult | None = None
    trusted_input = dict(block.input or {})
    trusted_input["usage_id"] = f"lilies-tool:{tool_id}"
    try:
        if block.name == DevelopmentToolName.workspace_search.value:
            request = WorkspaceSearchRequest.model_validate(trusted_input)
            result = await tools.workspace_search(request)
        elif block.name == DevelopmentToolName.workspace_read.value:
            request = WorkspaceReadRequest.model_validate(trusted_input)
            result = await tools.workspace_read(request)
        elif block.name == DevelopmentToolName.git_diff.value:
            request = GitDiffRequest.model_validate(trusted_input)
            result = await tools.git_diff(request)
        elif block.name == DevelopmentToolName.process_run.value:
            request = ProcessRunRequest.model_validate(trusted_input)
            result = await tools.process_run(request)
            process_result = result
        else:
            raise ValueError("unknown development tool")
        payload = result.model_dump(mode="json")
        content = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        transcript = {
            "name": block.name,
            "input_digest": canonical_digest(block.input or {}),
            "result_digest": canonical_digest(payload),
            "is_error": False,
        }
        response = ContentBlock(
            type="tool_result",
            tool_use_id=tool_id,
            content=content,
            is_error=False,
        )
    except Exception as error:
        content = json.dumps(
            {
                "error": type(error).__name__,
                "message": str(error)[:1_000],
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        transcript = {
            "name": block.name,
            "input_digest": canonical_digest(block.input or {}),
            "result_digest": canonical_digest(content),
            "is_error": True,
            "error_type": type(error).__name__,
            "error_message": str(error)[:1_000],
        }
        response = ContentBlock(
            type="tool_result",
            tool_use_id=tool_id,
            content=content,
            is_error=True,
        )
    return response, transcript, process_result


async def _lilies_review_with_tools(
    *,
    settings: LiliesSettings,
    provider: DeepSeekProvider,
    provider_capability: BoundDevelopmentProviderCapability,
    provider_capability_registry: dict[
        str,
        BoundDevelopmentProviderCapability,
    ],
    store: CollaborativeDevelopmentStore,
    assignment_id: Any,
    grant: Any,
    work_item: DevelopmentWorkItem,
    source_result: DevelopmentResult,
    review_snapshot: DevelopmentReviewSnapshotReceipt,
) -> tuple[LiliesReview, dict[str, Any]]:
    provider_capability.require_grant(grant)
    if (
        provider.name != provider_capability.provider
        or settings.model != provider_capability.model
    ):
        raise RuntimeError("Lilies provider transport differs from its capability")
    authority = DevelopmentToolAuthority(
        actor_role=AgentRole.lilies,
        workspace_grant=grant,
        enabled_tools=(
            DevelopmentToolName.workspace_search,
            DevelopmentToolName.workspace_read,
            DevelopmentToolName.git_diff,
            DevelopmentToolName.process_run,
        ),
        max_timeout_seconds=60,
        max_output_bytes=256_000,
        autonomous_handoff=True,
    )
    tools = DevelopmentWorkspaceTools(
        authority,
        assignment_id=assignment_id,
        usage_meter=store,
        metering_required=True,
    )
    definitions = [
        _tool_definition(
            name=DevelopmentToolName.workspace_search.value,
            description="Search only the frozen Lilies review snapshot.",
            schema=WorkspaceSearchRequest,
        ),
        _tool_definition(
            name=DevelopmentToolName.workspace_read.value,
            description="Read a file only from the frozen Lilies review snapshot.",
            schema=WorkspaceReadRequest,
        ),
        _tool_definition(
            name=DevelopmentToolName.git_diff.value,
            description="Inspect the snapshot's Git diff from its frozen baseline.",
            schema=GitDiffRequest,
        ),
        _tool_definition(
            name=DevelopmentToolName.process_run.value,
            description="Run one exact command from the review grant allowlist.",
            schema=ProcessRunRequest,
        ),
    ]
    provider_cost_records: list[dict[str, Any]] = []

    async def collect_bounded_response(
        *,
        request_label: str,
        stream: Any,
    ) -> Any:
        provider_request_id = (
            f"t01g-live-lilies-review:{request_label}:{uuid4().hex}"
        )
        reservation_id = uuid4()
        existing_capability = provider_capability_registry.setdefault(
            provider_request_id,
            provider_capability,
        )
        if existing_capability != provider_capability:
            raise RuntimeError("provider request id was rebound to another capability")
        authorization_payload = provider_capability.cost_authorization_payload(
            provider_request_id=provider_request_id,
            worst_case_cost_usd=1.0,
        )
        authorization = TrustedProviderCostAuthorization(
            reservation_id=reservation_id,
            assignment_id=assignment_id,
            provider=provider.name,
            provider_request_id=provider_request_id,
            model=settings.model,
            worst_case_cost_usd=1.0,
            evidence_digest=canonical_digest(authorization_payload),
            authorized_at=utc_now(),
        )
        if not await store.reserve_trusted_provider_cost(authorization):
            raise RuntimeError("provider cost authorization was replayed")
        response = await collect_model_stream(
            stream,
            model=settings.model,
            timeout_seconds=settings.model_timeout_seconds,
            expose_thinking=False,
            price_estimates_usd_per_million={
                settings.model: {
                    "input_tokens": settings.model_price_input_usd_per_million,
                    "output_tokens": settings.model_price_output_usd_per_million,
                }
            },
        )
        usage = response.usage
        cost_record: dict[str, Any] = {
            "reservation_id": str(reservation_id),
            "provider_request_id": provider_request_id,
            "worst_case_cost_usd": 1.0,
            "cost_source": usage.cost_source,
            "settled": False,
            "provider_capability_digest": (
                provider_capability.capability_digest
            ),
            "dispatch_grant_digest": (
                provider_capability.dispatch_grant_digest
            ),
            "provider_hosts": list(provider_capability.endpoint_hosts),
            "secret_refs": list(provider_capability.secret_refs),
            "credential_identity": (
                provider_capability.credential_identity
            ),
            "authorization_evidence_digest": (
                authorization.evidence_digest
            ),
        }
        if usage.cost_source != "unsupported":
            receipt_payload = provider_capability.cost_receipt_payload(
                provider_request_id=provider_request_id,
                cost_usd=usage.cost_usd,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            receipt = TrustedProviderCostReceipt(
                receipt_id=f"t01g-live-cost:{uuid4().hex}",
                reservation_id=reservation_id,
                assignment_id=assignment_id,
                provider=provider.name,
                provider_request_id=provider_request_id,
                model=settings.model,
                cost_usd=usage.cost_usd,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                evidence_digest=canonical_digest(receipt_payload),
                issued_at=utc_now(),
            )
            if not await store.record_trusted_provider_cost(receipt):
                raise RuntimeError("provider cost receipt was replayed")
            cost_record.update(
                {
                    "settled": True,
                    "actual_cost_usd": usage.cost_usd,
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "receipt_evidence_digest": receipt.evidence_digest,
                }
            )
        provider_cost_records.append(cost_record)
        return response

    system = build_lilies_development_system_prompt(
        workspace=grant.workspace_root,
        tool_names=(item.name for item in definitions),
        assignment_goal=(
            "Independently review Codex's bounded arithmetic repair in an unrelated "
            "plain-Python fixture."
        ),
        task_role="independent reviewer",
        authority_summary=canonical_digest(
            {
                "workspace_id": str(grant.workspace_id),
                "paths": list(grant.allowed_paths),
                "argv": [list(item) for item in grant.allowed_argv],
                "hosts": list(grant.allowed_hosts),
                "side_effects": [
                    effect.value for effect in grant.allowed_side_effects
                ],
                "secret_refs": list(grant.secret_refs),
            }
        ),
    )
    prompt = json.dumps(
        {
            "instruction": (
                "Do not trust Codex's completion claim and do not ask for supplied diff "
                "or test text. Use exactly these three tool calls, with workspace-relative "
                "path and cwd values (never the absolute workspace path): "
                "workspace_read {\"path\":\"src/mathlib.py\"}; "
                "git_diff {\"cwd\":\"src\",\"paths\":[\"mathlib.py\"]}; "
                "process_run "
                f"{{\"cwd\":\"tests\",\"argv\":[{json.dumps(sys.executable)},"
                "\"check.py\"]}}. Do not call workspace_search or any additional tool. "
                "Only after those three tool results, "
                "return JSON with verdict, acceptance_checks, summary, limitations. "
                "Each acceptance check contains criterion, passed, evidence."
            ),
            "acceptance": list(work_item.acceptance),
            "source_result_binding": {
                "result_id": str(source_result.result_id),
                "diff_digest": source_result.diff_digest,
            },
            "review_snapshot_binding": {
                "snapshot_id": str(review_snapshot.review_snapshot_id),
                "receipt_digest": review_snapshot.receipt_digest,
            },
        },
        ensure_ascii=False,
    )
    messages = [
        ChatMessage(
            role="user",
            content=[ContentBlock(type="text", text=prompt)],
        )
    ]
    required_tools = {
        DevelopmentToolName.workspace_read.value,
        DevelopmentToolName.git_diff.value,
        DevelopmentToolName.process_run.value,
    }
    called_tools: set[str] = set()
    successful_tools: set[str] = set()
    transcript: list[dict[str, Any]] = []
    usage_records: list[dict[str, Any]] = []
    successful_test: tuple[ProcessRunResult, Any, Any] | None = None
    final_text = ""

    for iteration in range(1, 9):
        stream = provider.stream(
            model=settings.model,
            system=system,
            messages=messages,
            tools=definitions,
            max_output_tokens=min(settings.max_output_tokens, 4_096),
            thinking_enabled=True,
            effort="high",
            user_id="v04-13-t01g-live-lilies-review",
        )
        response = await collect_bounded_response(
            request_label=f"tool-loop-{iteration}",
            stream=stream,
        )
        usage_records.append(response.usage.model_dump(mode="json"))
        messages.append(ChatMessage(role="assistant", content=response.blocks))
        calls = [
            block for block in response.blocks if block.type == "tool_use"
        ]
        if calls:
            results: list[ContentBlock] = []
            for block in calls:
                started_at = utc_now()
                tool_result, record, process_result = await _execute_lilies_tool(
                    block=block,
                    tools=tools,
                )
                finished_at = utc_now()
                record["iteration"] = iteration
                transcript.append(record)
                if block.name:
                    called_tools.add(block.name)
                    if not record["is_error"]:
                        successful_tools.add(block.name)
                if (
                    block.name == DevelopmentToolName.process_run.value
                    and process_result is not None
                    and process_result.exit_code == 0
                ):
                    successful_test = (
                        process_result,
                        started_at,
                        finished_at,
                    )
                results.append(tool_result)
            messages.append(ChatMessage(role="user", content=results))
            if (
                required_tools.issubset(successful_tools)
                and successful_test is not None
            ):
                break
            continue

        final_text = "".join(
            block.text or ""
            for block in response.blocks
            if block.type == "text"
        )
        if not required_tools.issubset(successful_tools):
            missing = sorted(required_tools - successful_tools)
            messages.append(
                ChatMessage(
                    role="user",
                    content=[
                        ContentBlock(
                            type="text",
                            text=(
                                "The review is incomplete. Before a verdict, call these "
                                f"independent tools: {', '.join(missing)}."
                            ),
                        )
                    ],
                )
            )
            continue
        break
    else:
        raise RuntimeError("Lilies did not complete the bounded review tool loop")

    if not required_tools.issubset(successful_tools):
        raise RuntimeError("Lilies omitted a mandatory independent review tool")
    if successful_test is None:
        raise RuntimeError("Lilies did not produce a zero-exit acceptance test receipt")
    if not final_text.strip():
        messages.append(
            ChatMessage(
                role="user",
                content=[
                    ContentBlock(
                        type="text",
                        text=(
                            "The mandatory independent tool checks are complete. "
                            "Using only the observed tool results already in this "
                            "conversation, return the requested review JSON now. "
                            "Do not request or invent another tool call."
                        ),
                    )
                ],
            )
        )
        final_stream = provider.stream(
            model=settings.model,
            system=system,
            messages=messages,
            tools=[],
            max_output_tokens=min(settings.max_output_tokens, 4_096),
            thinking_enabled=True,
            effort="high",
            user_id="v04-13-t01g-live-lilies-review-final",
        )
        final_response = await collect_bounded_response(
            request_label="final",
            stream=final_stream,
        )
        usage_records.append(final_response.usage.model_dump(mode="json"))
        if any(
            block.type == "tool_use" for block in final_response.blocks
        ):
            raise RuntimeError(
                "Lilies attempted a tool call after the bounded review phase"
            )
        final_text = "".join(
            block.text or ""
            for block in final_response.blocks
            if block.type == "text"
        )

    payload = _sanitize_evidence_text(
        _extract_json(final_text),
        replacements=(
            (grant.workspace_root, "<lilies-review-workspace>"),
            (sys.executable, "<python>"),
            (str(ROOT), "<repository-root>"),
        ),
    )
    normalizations: list[str] = []
    normalized_verdict = _normalized_live_review_verdict(payload.get("verdict"))
    if normalized_verdict is not None:
        if normalized_verdict != payload["verdict"]:
            normalizations.append("verdict_enum_alias")
        payload["verdict"] = normalized_verdict
    if isinstance(payload.get("limitations"), str):
        payload["limitations"] = [payload["limitations"]]
        normalizations.append("limitations_string_to_list")
    raw_acceptance_checks = payload.get("acceptance_checks")
    if (
        not isinstance(raw_acceptance_checks, list)
        or len(raw_acceptance_checks) != len(work_item.acceptance)
    ):
        raise RuntimeError(
            "Lilies review did not return one check for every frozen criterion"
        )
    model_acceptance_checks = json.loads(
        json.dumps(raw_acceptance_checks, ensure_ascii=False)
    )
    for index, frozen_criterion in enumerate(work_item.acceptance):
        check = raw_acceptance_checks[index]
        if not isinstance(check, dict):
            raise RuntimeError("Lilies review returned a non-object acceptance check")
        if check.get("criterion") != frozen_criterion:
            check["criterion"] = frozen_criterion
            normalizations.append(f"acceptance_criterion_{index + 1}_frozen")
    decision = LiveLiliesDecision.model_validate(payload)
    if decision.verdict != "accepted":
        raise RuntimeError("Lilies rejected the real Codex result")

    process_result, started_at, finished_at = successful_test
    command = _command_receipt(
        process_result,
        started_at=started_at,
        finished_at=finished_at,
    )
    evidence_refs = tuple(
        dict.fromkeys(
            (
                review_snapshot.receipt_digest,
                source_result.diff_digest,
                command.output_digest,
            )
        )
    )
    review = LiliesReview(
        review_id=uuid4(),
        assignment_id=work_item.assignment_id,
        work_item_id=work_item.work_item_id,
        result_id=source_result.result_id,
        verdict=ReviewVerdict.accepted,
        acceptance_checks=tuple(
            AcceptanceCheck(
                criterion=item.criterion,
                passed=item.passed,
                evidence_refs=(command.output_digest,),
            )
            for item in decision.acceptance_checks
        ),
        verification_commands=(command,),
        evidence_refs=evidence_refs,
        created_at=utc_now(),
    )
    return review, {
        "review": decision.model_dump(mode="json"),
        "frozen_acceptance": list(work_item.acceptance),
        "model_acceptance_checks": model_acceptance_checks,
        "persisted_review_id": str(review.review_id),
        "provider": provider.name,
        "model": settings.model,
        "provider_authority": provider_capability.public_evidence(),
        "usage": usage_records,
        "provider_cost_control": provider_cost_records,
        "tool_calls": transcript,
        "called_tool_names": sorted(called_tools),
        "successful_tool_names": sorted(successful_tools),
        "denied_tool_calls": sum(item["is_error"] for item in transcript),
        "mandatory_tool_names": sorted(required_tools),
        "response_digest": canonical_digest(final_text),
        "normalizations": normalizations,
        "review_snapshot_id": str(review_snapshot.review_snapshot_id),
        "review_snapshot_receipt_digest": review_snapshot.receipt_digest,
        "independent_snapshot": True,
    }


async def run(*, timeout_seconds: int) -> dict[str, Any]:
    codex = shutil.which("codex")
    if codex is None:
        raise RuntimeError("Codex CLI is unavailable")
    settings = LiliesSettings()
    if not settings.deepseek_api_key:
        raise RuntimeError("Lilies model credential is unavailable")
    deepseek_host = normalized_provider_endpoint_host(
        settings.deepseek_base_url
    )
    source_revision = qualification_source_revision(ROOT)
    if source_revision == "unavailable":
        raise RuntimeError("qualification source revision is unavailable")

    with tempfile.TemporaryDirectory(
        prefix="lilies-v04-13-live-handoff-"
    ) as raw:
        root = Path(raw).resolve()
        source, baseline = _fixture(root)
        assignment_id = uuid4()
        workspace_state = root / "workspace-state"
        codex_runtime = root / "codex-runtime"
        codex_model_workspace = codex_runtime / "model-workspace"
        codex_model_workspace.mkdir(parents=True, mode=0o700)
        codex_output_schema = codex_runtime / "patch-schema.json"
        codex_output_schema.write_text(
            json.dumps(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "summary": {"type": "string", "minLength": 1},
                    },
                    "required": ["old_string", "new_string", "summary"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        codex_output_schema.chmod(0o600)
        codex = str(Path(codex).resolve(strict=True))
        codex_argv = (
            codex,
            "-a",
            "never",
            "exec",
            "-m",
            CODEX_MODEL,
            "-C",
            str(codex_model_workspace),
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--output-schema",
            str(codex_output_schema),
            "--json",
            "-",
        )
        test_argv = (sys.executable, "check.py")
        broker = DevelopmentWorkspaceBroker(workspace_state)
        prepared = broker.prepare(
            source_repository=source,
            assignment_id=assignment_id,
            baseline_revision=baseline,
            specs=(
                DevelopmentWorkspaceSpec(
                    agent_role=AgentRole.lilies,
                    allowed_paths=("src", "tests"),
                    allowed_argv=(test_argv,),
                    allowed_hosts=(deepseek_host,),
                    allowed_side_effects=(
                        SideEffect.process_execute,
                        SideEffect.network_access,
                    ),
                    secret_refs=(DEEPSEEK_SECRET_REF,),
                ),
                DevelopmentWorkspaceSpec(
                    agent_role=AgentRole.codex,
                    allowed_paths=("src", "tests"),
                    allowed_argv=(codex_argv, test_argv),
                    allowed_hosts=CODEX_ALLOWED_PROVIDER_HOSTS,
                    allowed_side_effects=(
                        SideEffect.workspace_write,
                        SideEffect.process_execute,
                        SideEffect.network_access,
                    ),
                    secret_refs=(CODEX_SECRET_REF,),
                ),
            ),
        )
        grants = {grant.agent_role: grant for grant in prepared.grants}
        created = utc_now()
        assignment = DevelopmentAssignment(
            assignment_id=assignment_id,
            goal=(
                "Repair addition in an unrelated plain-Python repository, then "
                "independently review the exact result."
            ),
            software_id="t01g-live-unrelated-python-fixture",
            baseline_commit=baseline,
            agent_roles=(
                AgentRoleGrant(
                    agent_role=AgentRole.lilies,
                    task_roles=(DevelopmentTaskRole.reviewer,),
                ),
                AgentRoleGrant(
                    agent_role=AgentRole.codex,
                    task_roles=(DevelopmentTaskRole.implementer,),
                ),
            ),
            workspace_grants=prepared.grants,
            budget=DevelopmentBudget(
                max_work_items=1,
                max_commands=10,
                max_tool_calls=20,
                max_wall_seconds=max(timeout_seconds * 3, 900),
                max_cost_usd=10,
            ),
            deadline=created
            + timedelta(seconds=max(timeout_seconds * 3, 900)),
            approval_mode=ApprovalMode.auto_forward,
            execution_mode=ExecutionMode.autonomous,
            created_at=created,
            updated_at=created,
        )
        item = DevelopmentWorkItem(
            work_item_id=uuid4(),
            assignment_id=assignment_id,
            kind=WorkItemKind.bug,
            objective=(
                "Change only src/mathlib.py so the frozen addition check passes."
            ),
            acceptance=(
                "The implementation returns arithmetic addition, not subtraction.",
                "The frozen two-case tests/check.py command exits with status 0.",
                "Only src/mathlib.py differs from the frozen baseline.",
            ),
            assigned_role=AgentRole.codex,
            created_at=created,
            updated_at=created,
        )
        database_path = root / "data" / "collaborative-development.db"
        journal_path = root / "data" / "dispatch.db"
        trusted_provider_models = {
            "deepseek": settings.model,
            CODEX_PROVIDER: CODEX_MODEL,
        }
        trusted_provider_capabilities: dict[
            str,
            BoundDevelopmentProviderCapability,
        ] = {}

        def trusted_cost_authorization(
            candidate: TrustedProviderCostAuthorization,
        ) -> bool:
            capability = trusted_provider_capabilities.get(
                candidate.provider_request_id
            )
            if capability is None:
                return False
            expected_payload = capability.cost_authorization_payload(
                provider_request_id=candidate.provider_request_id,
                worst_case_cost_usd=candidate.worst_case_cost_usd,
            )
            expected = canonical_digest(expected_payload)
            return (
                candidate.assignment_id == assignment_id
                and candidate.provider == capability.provider
                and candidate.model == capability.model
                and trusted_provider_models.get(candidate.provider)
                == candidate.model
                and candidate.worst_case_cost_usd == 1.0
                and candidate.evidence_digest == expected
            )

        def trusted_cost_receipt(
            candidate: TrustedProviderCostReceipt,
        ) -> bool:
            capability = trusted_provider_capabilities.get(
                candidate.provider_request_id
            )
            if capability is None:
                return False
            expected_payload = capability.cost_receipt_payload(
                provider_request_id=candidate.provider_request_id,
                cost_usd=candidate.cost_usd,
                input_tokens=candidate.input_tokens,
                output_tokens=candidate.output_tokens,
            )
            expected = canonical_digest(expected_payload)
            return (
                candidate.assignment_id == assignment_id
                and candidate.provider == capability.provider
                and candidate.model == capability.model
                and trusted_provider_models.get(candidate.provider)
                == candidate.model
                and candidate.evidence_digest == expected
            )

        development_store = CollaborativeDevelopmentStore(
            database_path,
            trusted_provider_cost_authorizer=trusted_cost_authorization,
            trusted_provider_receipt_verifier=trusted_cost_receipt,
        )
        service = CollaborativeDevelopmentService(
            store=development_store,
            enabled=True,
            autonomous_enabled=True,
        )
        await service.initialize()
        owner = DevelopmentPrincipal(
            actor_role="user",
            actor_id="t01g-live-owner",
        )
        stored_assignment = await service.create_assignment(
            principal=owner,
            assignment=assignment,
            idempotency_key="live-assignment-create-0001",
        )
        stored_item = await service.create_work_item(
            principal=owner,
            item=item,
            idempotency_key="live-work-item-create-0001",
        )

        codex_evidence: dict[str, Any] = {}
        lilies_evidence: dict[str, Any] = {}
        dispatched_grants: dict[AgentRole, Any] = {}

        def codex_handler(
            *,
            context: RoleBoundDispatchContext,
        ) -> AutonomousHandlerCompletion:
            try:
                grant = context.workspace_grant
                lease = context.lease
                work_item = context.work_item
                cancel_event = context.cancel_event
                if (
                    grant.agent_role != AgentRole.codex
                    or lease is None
                    or cancel_event is None
                ):
                    raise RuntimeError(
                        "Codex handler received the wrong role projection"
                    )
                if grant != grants[AgentRole.codex]:
                    raise RuntimeError(
                        "Codex handler authority changed before invocation"
                    )
                dispatched_grants[AgentRole.codex] = grant
                invocation = tuple(grant.allowed_argv[0])
                if invocation != codex_argv:
                    raise RuntimeError(
                        "Codex invocation is not the frozen exact argv"
                    )
                codex_provider_capability = (
                    BoundDevelopmentProviderCapability.bind_exact(
                        assignment_id=stored_assignment.assignment_id,
                        expected_role=AgentRole.codex,
                        grant=grant,
                        provider=CODEX_PROVIDER,
                        model=CODEX_MODEL,
                        expected_hosts=CODEX_ALLOWED_PROVIDER_HOSTS,
                        expected_secret_refs=(CODEX_SECRET_REF,),
                        expected_side_effects=(
                            SideEffect.workspace_write,
                            SideEffect.process_execute,
                            SideEffect.network_access,
                        ),
                        credential_identity=(
                            CODEX_SUBSCRIPTION_IDENTITY
                        ),
                    )
                )
                tool_authority = DevelopmentToolAuthority(
                    actor_role=AgentRole.codex,
                    workspace_grant=grant,
                    enabled_tools=(
                        DevelopmentToolName.workspace_read,
                        DevelopmentToolName.workspace_patch,
                        DevelopmentToolName.git_diff,
                        DevelopmentToolName.process_run,
                    ),
                    max_timeout_seconds=60,
                    max_output_bytes=256_000,
                    autonomous_handoff=True,
                )
                role_tools = DevelopmentWorkspaceTools(
                    tool_authority,
                    assignment_id=stored_assignment.assignment_id,
                    usage_meter=development_store,
                    metering_required=True,
                )
                source_read = asyncio.run(
                    role_tools.workspace_read(
                        WorkspaceReadRequest(
                            path="src/mathlib.py",
                            usage_id="codex-source-read-0001",
                        )
                    )
                )
                provider_request_id = (
                    f"t01g-live-codex-provider:{uuid4().hex}"
                )
                provider_reservation_id = uuid4()
                existing_capability = (
                    trusted_provider_capabilities.setdefault(
                        provider_request_id,
                        codex_provider_capability,
                    )
                )
                if existing_capability != codex_provider_capability:
                    raise RuntimeError(
                        "Codex provider request id was rebound"
                    )
                provider_authorization_payload = (
                    codex_provider_capability.cost_authorization_payload(
                        provider_request_id=provider_request_id,
                        worst_case_cost_usd=1.0,
                    )
                )
                provider_authorization = TrustedProviderCostAuthorization(
                    reservation_id=provider_reservation_id,
                    assignment_id=stored_assignment.assignment_id,
                    provider=CODEX_PROVIDER,
                    provider_request_id=provider_request_id,
                    model=CODEX_MODEL,
                    worst_case_cost_usd=1.0,
                    evidence_digest=canonical_digest(
                        provider_authorization_payload
                    ),
                    authorized_at=utc_now(),
                )
                if not asyncio.run(
                    development_store.reserve_trusted_provider_cost(
                        provider_authorization
                    )
                ):
                    raise RuntimeError(
                        "Codex provider authorization was already reserved"
                    )
                codex_usage_id = "codex-agent-process-0001"
                codex_request_digest = canonical_digest(
                    {
                        "tool": "process_run",
                        "workspace_id": str(grant.workspace_id),
                        "grant_revision": grant.grant_revision,
                        "argv": list(invocation),
                        "cwd": ".",
                    }
                )
                acquired = asyncio.run(
                    development_store.reserve_development_tool_usage(
                        assignment_id=stored_assignment.assignment_id,
                        actor_role=AgentRole.codex,
                        usage_id=codex_usage_id,
                        tool_name="process_run",
                        request_digest=codex_request_digest,
                        command_argv=invocation,
                        command_cwd=".",
                    )
                )
                if not acquired:
                    raise RuntimeError(
                        "Codex agent process usage was already reserved"
                    )
                implementation = _run_codex(
                    executable=codex,
                    argv=invocation,
                    model_workspace=codex_model_workspace,
                    runtime_root=codex_runtime,
                    source_text=source_read.content,
                    timeout_seconds=timeout_seconds,
                    cancel_event=cancel_event,
                )
                proposal = implementation.pop("_proposal")
                codex_usage = implementation["usage"]
                provider_receipt_payload = (
                    codex_provider_capability.cost_receipt_payload(
                        provider_request_id=provider_request_id,
                        cost_usd=0.0,
                        input_tokens=codex_usage["input_tokens"],
                        output_tokens=codex_usage["output_tokens"],
                    )
                )
                provider_receipt = TrustedProviderCostReceipt(
                    receipt_id=f"t01g-live-cost:{uuid4().hex}",
                    reservation_id=provider_reservation_id,
                    assignment_id=stored_assignment.assignment_id,
                    provider=CODEX_PROVIDER,
                    provider_request_id=provider_request_id,
                    model=CODEX_MODEL,
                    cost_usd=0.0,
                    input_tokens=codex_usage["input_tokens"],
                    output_tokens=codex_usage["output_tokens"],
                    evidence_digest=canonical_digest(
                        provider_receipt_payload
                    ),
                    issued_at=utc_now(),
                )
                if not asyncio.run(
                    development_store.record_trusted_provider_cost(
                        provider_receipt
                    )
                ):
                    raise RuntimeError("Codex provider receipt was replayed")
                implementation["provider_cost"] = {
                    "reservation_id": str(provider_reservation_id),
                    "provider_request_id": provider_request_id,
                    "worst_case_cost_usd": 1.0,
                    "actual_cost_usd": 0.0,
                    "cost_source": (
                        "Codex subscription transport reports token usage but "
                        "no per-request monetary charge"
                    ),
                    "provider_capability_digest": (
                        codex_provider_capability.capability_digest
                    ),
                    "dispatch_grant_digest": (
                        codex_provider_capability.dispatch_grant_digest
                    ),
                    "provider_hosts": list(
                        codex_provider_capability.endpoint_hosts
                    ),
                    "secret_refs": list(
                        codex_provider_capability.secret_refs
                    ),
                    "credential_identity": (
                        codex_provider_capability.credential_identity
                    ),
                    "authorization_evidence_digest": (
                        provider_authorization.evidence_digest
                    ),
                    "receipt_evidence_digest": (
                        provider_receipt.evidence_digest
                    ),
                    "settled": True,
                }
                implementation["provider_authority"] = (
                    codex_provider_capability.public_evidence()
                )
                asyncio.run(
                    development_store.complete_development_tool_usage(
                        assignment_id=stored_assignment.assignment_id,
                        actor_role=AgentRole.codex,
                        usage_id=codex_usage_id,
                        request_digest=codex_request_digest,
                        response_digest=canonical_digest(implementation),
                        output_digest=canonical_digest(
                            {
                                "stdout_digest": implementation["stdout_digest"],
                                "stderr_digest": implementation["stderr_digest"],
                            }
                        ),
                    )
                )
                if implementation["exit_code"] != 0:
                    raise RuntimeError("Codex implementer failed")

                async def apply_and_verify() -> tuple[
                    Any,
                    Any,
                    ProcessRunResult,
                    Any,
                    Any,
                ]:
                    patch = await role_tools.workspace_patch(
                        WorkspacePatchRequest(
                            path="src/mathlib.py",
                            old_string=proposal["old_string"],
                            new_string=proposal["new_string"],
                            usage_id="codex-proposal-patch-0001",
                        )
                    )
                    observed_diff = await role_tools.git_diff(
                        GitDiffRequest(
                            cwd="src",
                            paths=("mathlib.py",),
                            usage_id="codex-result-git-diff-0001",
                        )
                    )
                    started_at = utc_now()
                    test = await role_tools.process_run(
                        ProcessRunRequest(
                            argv=test_argv,
                            cwd="tests",
                            timeout_seconds=30,
                            usage_id="codex-result-test-0001",
                        )
                    )
                    finished_at = utc_now()
                    return (
                        patch,
                        observed_diff,
                        test,
                        started_at,
                        finished_at,
                    )

                patch, observed_diff, test, started_at, finished_at = (
                    asyncio.run(apply_and_verify())
                )
                workspace = Path(grant.workspace_root)
                status_lines = _git(
                    workspace,
                    "status",
                    "--porcelain",
                ).stdout.splitlines()
                if status_lines != [" M src/mathlib.py"]:
                    raise RuntimeError(
                        f"Codex changed an unauthorized file set: {status_lines}"
                    )
                diff = _git(
                    workspace,
                    "diff",
                    "--",
                    "src/mathlib.py",
                ).stdout
                if not diff.strip():
                    raise RuntimeError("Codex produced no source diff")
                if test.exit_code != 0:
                    raise RuntimeError(
                        "Codex result failed the exact granted acceptance command"
                    )
                diff_digest = broker.calculate_diff_digest(
                    workspace_root=workspace,
                    baseline_commit=stored_assignment.baseline_commit,
                )
                command = _command_receipt(
                    test,
                    started_at=started_at,
                    finished_at=finished_at,
                )
                result = DevelopmentResult(
                    result_id=uuid4(),
                    assignment_id=stored_assignment.assignment_id,
                    work_item_id=work_item.work_item_id,
                    lease_id=lease.lease_id,
                    agent_role=AgentRole.codex,
                    baseline_commit=stored_assignment.baseline_commit,
                    diff_digest=diff_digest,
                    commands=(command,),
                    tests=(
                        TestReceipt(
                            name="frozen two-case addition check",
                            command_digest=canonical_digest(list(test.argv)),
                            exit_code=0,
                            passed=True,
                            output_digest=test.output_digest,
                        ),
                    ),
                    evidence_refs=tuple(
                        dict.fromkeys(
                            (
                                diff_digest,
                                patch.content_digest,
                                observed_diff.output_digest,
                                test.output_digest,
                            )
                        )
                    ),
                    reproduction_steps=(
                        "Inspect the frozen Git diff for src/mathlib.py.",
                        f"From tests, run {sys.executable} check.py.",
                    ),
                    created_at=utc_now(),
                )
                codex_evidence.update(
                    {
                        **implementation,
                        "result_id": str(result.result_id),
                        "source_read_digest": canonical_digest(source_read),
                        "proposal_digest": canonical_digest(proposal),
                        "trusted_patch_digest": patch.content_digest,
                        "changed_files": ["src/mathlib.py"],
                        "diff": diff,
                        "diff_digest": canonical_digest(diff),
                        "broker_diff_digest": diff_digest,
                        "role_grant_digest": canonical_digest(grant),
                        "other_role_grant_visible_to_model": False,
                        "test": {
                            "argv": [Path(sys.executable).name, "check.py"],
                            "cwd": "tests",
                            "exit_code": test.exit_code,
                            "stdout_digest": canonical_digest(test.stdout),
                            "stderr_digest": canonical_digest(test.stderr),
                            "output_digest": test.output_digest,
                        },
                    },
                )
                return AutonomousHandlerCompletion(
                    outcome=DispatchOutcome(
                        status=DispatchOutcomeStatus.delivered,
                        detail=(
                            "Codex produced a role-bound DevelopmentResult with a "
                            "real diff and zero-exit test."
                        ),
                        evidence_refs=result.evidence_refs,
                    ),
                    development_result=result,
                )
            except Exception as error:
                codex_evidence["handler_error"] = {
                    "type": type(error).__name__,
                    "message": str(error)[:1_000],
                }
                raise

        async def lilies_handler(
            *,
            context: RoleBoundDispatchContext,
        ) -> AutonomousHandlerCompletion:
            try:
                grant = context.workspace_grant
                source_result = context.source_result
                review_snapshot = context.review_snapshot
                work_item = context.work_item
                if (
                    grant.agent_role != AgentRole.lilies
                    or source_result is None
                    or review_snapshot is None
                ):
                    raise RuntimeError(
                        "Lilies handler received an incomplete review handoff"
                    )
                if Path(grant.workspace_root) != Path(
                    review_snapshot.review_workspace_root
                ):
                    raise RuntimeError(
                        "Lilies did not receive the frozen review snapshot"
                    )
                if (
                    grant.allowed_hosts != (deepseek_host,)
                    or grant.secret_refs != (DEEPSEEK_SECRET_REF,)
                    or set(grant.allowed_side_effects)
                    != {
                        SideEffect.process_execute,
                        SideEffect.network_access,
                    }
                ):
                    raise RuntimeError(
                        "Lilies review grant does not exactly bind provider authority"
                    )
                dispatched_grants[AgentRole.lilies] = grant
                provider_capability = (
                    BoundDevelopmentProviderCapability.bind(
                        assignment_id=stored_assignment.assignment_id,
                        expected_role=AgentRole.lilies,
                        grant=grant,
                        provider="deepseek",
                        model=settings.model,
                        endpoint_url=settings.deepseek_base_url,
                        secret_ref=DEEPSEEK_SECRET_REF,
                        credential_identity=(
                            DEEPSEEK_CREDENTIAL_IDENTITY
                        ),
                    )
                )
                provider = DeepSeekProvider(
                    settings.deepseek_api_key,
                    settings.deepseek_base_url,
                    timeout_seconds=settings.model_timeout_seconds,
                )
                review, evidence = await _lilies_review_with_tools(
                    settings=settings,
                    provider=provider,
                    provider_capability=provider_capability,
                    provider_capability_registry=(
                        trusted_provider_capabilities
                    ),
                    store=development_store,
                    assignment_id=stored_assignment.assignment_id,
                    grant=grant,
                    work_item=work_item,
                    source_result=source_result,
                    review_snapshot=review_snapshot,
                )
                lilies_evidence.update(evidence)
                return AutonomousHandlerCompletion(
                    outcome=DispatchOutcome(
                        status=DispatchOutcomeStatus.delivered,
                        detail=(
                            "Lilies independently inspected the review snapshot and "
                            "submitted a bound accepted review."
                        ),
                        evidence_refs=review.evidence_refs,
                    ),
                    lilies_review=review,
                )
            except Exception as error:
                lilies_evidence["handler_error"] = {
                    "type": type(error).__name__,
                    "message": str(error)[:1_000],
                }
                raise

        lifecycle = AutonomousDevelopmentLifecycleBridge(
            service=service,
            workspace_broker=broker,
            lease_ttl_seconds=min(timeout_seconds, 600),
            cancellation_poll_seconds=0.05,
        )
        first_batch = await run_dispatch_worker(
            database_path=database_path,
            journal_path=journal_path,
            handlers={AgentRole.codex: codex_handler},
            once=True,
            poll_interval_seconds=0.05,
            limit=10,
            claim_ttl_seconds=min(timeout_seconds, 600),
            dispatcher_id="t01g-live-dispatcher",
            lifecycle_bridge=lifecycle,
        )
        if [record.status for record in first_batch.records] != [
            DispatchOutcomeStatus.delivered
        ]:
            raise RuntimeError(
                "Codex lifecycle dispatch did not deliver: "
                + json.dumps(
                    [
                        {
                            "status": record.status.value,
                            "detail": record.detail,
                        }
                        for record in first_batch.records
                    ],
                    ensure_ascii=False,
                )
                + " handler="
                + json.dumps(codex_evidence.get("handler_error"), ensure_ascii=False)
            )
        ready = await service.store.get_work_item(stored_item.work_item_id)
        if ready.status != WorkItemStatus.ready_for_lilies_review:
            raise RuntimeError("Codex result did not reach Lilies review")

        second_batch = await run_dispatch_worker(
            database_path=database_path,
            journal_path=journal_path,
            handlers={AgentRole.lilies: lilies_handler},
            once=True,
            poll_interval_seconds=0.05,
            limit=10,
            claim_ttl_seconds=min(timeout_seconds, 600),
            dispatcher_id="t01g-live-dispatcher",
            lifecycle_bridge=lifecycle,
        )
        if [record.status for record in second_batch.records] != [
            DispatchOutcomeStatus.delivered
        ]:
            raise RuntimeError(
                "Lilies lifecycle dispatch did not deliver: "
                + json.dumps(
                    [
                        {
                            "status": record.status.value,
                            "detail": record.detail,
                        }
                        for record in second_batch.records
                    ],
                    ensure_ascii=False,
                )
                + " handler="
                + json.dumps(lilies_evidence.get("handler_error"), ensure_ascii=False)
            )
        accepted = await service.store.get_work_item(stored_item.work_item_id)
        if accepted.status != WorkItemStatus.accepted:
            raise RuntimeError("real handoff did not reach accepted")
        closed = await service.close_work_item(
            principal=owner,
            work_item_id=accepted.work_item_id,
            expected_revision=accepted.revision,
            idempotency_key="live-work-item-close-0001",
        )
        stopped = await service.stop_assignment(
            principal=owner,
            assignment_id=stored_assignment.assignment_id,
            expected_revision=stored_assignment.revision,
            idempotency_key="live-assignment-stop-0001",
        )
        archived = await service.archive_assignment(
            principal=owner,
            assignment_id=stored_assignment.assignment_id,
            expected_revision=stopped.revision,
            idempotency_key="live-assignment-archive-0001",
        )

        journal = CollaborativeDevelopmentDispatchJournal(journal_path)
        journal.initialize()
        history_before_restart = journal.history(stored_assignment.assignment_id)
        restarted_store = CollaborativeDevelopmentStore(database_path)
        await restarted_store.initialize()
        restarted_journal = CollaborativeDevelopmentDispatchJournal(journal_path)
        restarted_journal.initialize()
        history_after_restart = restarted_journal.history(
            stored_assignment.assignment_id
        )
        if history_before_restart != history_after_restart:
            raise RuntimeError("dispatch history changed across restart")
        if set(dispatched_grants) != {AgentRole.codex, AgentRole.lilies}:
            raise RuntimeError("live handoff did not retain both dispatched grants")
        if (
            await restarted_store.get_assignment(stored_assignment.assignment_id)
        ).status.value != "archived":
            raise RuntimeError("archived assignment did not survive restart")
        events = await restarted_store.read_events(
            stored_assignment.assignment_id,
            after=0,
            limit=1_000,
        )
        tool_usage = await restarted_store.list_development_tool_usage(
            stored_assignment.assignment_id
        )
        if (
            len(tool_usage) != 8
            or sum(record.tool_calls for record in tool_usage) != 8
            or sum(record.commands for record in tool_usage) != 5
            or any(record.status != "completed" for record in tool_usage)
        ):
            raise RuntimeError(
                "real handoff did not retain the expected completed tool/command ledger"
            )
        provider_costs = (
            await restarted_store.list_trusted_provider_cost_reservations(
                stored_assignment.assignment_id
            )
        )
        if not provider_costs or any(
            reservation.status != "settled"
            for reservation in provider_costs
        ):
            raise RuntimeError(
                "real Lilies provider calls lack trusted preauthorization and settlement"
            )
        if {
            reservation.cost_cap.provider for reservation in provider_costs
        } != {"deepseek", CODEX_PROVIDER}:
            raise RuntimeError(
                "real handoff did not retain both Codex and Lilies provider ledgers"
            )
        if {
            reservation.cost_cap.provider_request_id
            for reservation in provider_costs
        } != set(trusted_provider_capabilities):
            raise RuntimeError(
                "provider cost ledger is not fully bound to dispatch capabilities"
            )
        event_types = [event.event_type for event in events]
        required_event_types = {
            "assignment.created",
            "work_item.created",
            "work_item.leased",
            "work_item.working",
            "work_item.result_submitted",
            "work_item.accepted",
            "work_item.closed",
            "assignment.stopped",
            "assignment.archived",
        }
        if not required_event_types.issubset(event_types):
            raise RuntimeError("durable event history omitted a lifecycle state")
        if _git(source, "status", "--porcelain").stdout:
            raise RuntimeError("source fixture changed outside broker workspaces")
        original_grants_unchanged = (
            (
                await restarted_store.get_assignment(
                    stored_assignment.assignment_id
                )
            ).workspace_grants
            == stored_assignment.workspace_grants
        )
        if not original_grants_unchanged:
            raise RuntimeError("autonomous handoff widened its original grants")
        original_codex_grant = grants[AgentRole.codex]
        original_lilies_grant = grants[AgentRole.lilies]
        effective_codex_grant = dispatched_grants[AgentRole.codex]
        effective_lilies_grant = dispatched_grants[AgentRole.lilies]
        if effective_codex_grant != original_codex_grant:
            raise RuntimeError("Codex handler did not receive the original exact grant")
        if (
            str(effective_lilies_grant.workspace_id)
            != lilies_evidence.get("review_snapshot_id")
            or effective_lilies_grant.workspace_id
            == original_lilies_grant.workspace_id
            or effective_lilies_grant.baseline_commit
            != original_lilies_grant.baseline_commit
            or effective_lilies_grant.grant_revision
            != original_lilies_grant.grant_revision
            or not set(effective_lilies_grant.allowed_paths).issubset(
                original_lilies_grant.allowed_paths
            )
            or not set(effective_lilies_grant.allowed_argv).issubset(
                original_lilies_grant.allowed_argv
            )
            or not set(effective_lilies_grant.allowed_hosts).issubset(
                original_lilies_grant.allowed_hosts
            )
            or not set(effective_lilies_grant.allowed_side_effects).issubset(
                original_lilies_grant.allowed_side_effects
            )
            or not set(effective_lilies_grant.secret_refs).issubset(
                original_lilies_grant.secret_refs
            )
            or SideEffect.workspace_write
            in effective_lilies_grant.allowed_side_effects
            or SideEffect.external_mutation
            in effective_lilies_grant.allowed_side_effects
        ):
            raise RuntimeError(
                "Lilies handler authority is not a bound, narrower review snapshot"
            )
        provider_authorities = {
            AgentRole.codex: codex_evidence.get("provider_authority"),
            AgentRole.lilies: lilies_evidence.get("provider_authority"),
        }
        for role, effective_grant in dispatched_grants.items():
            provider_authority = provider_authorities[role]
            if (
                not isinstance(provider_authority, dict)
                or provider_authority.get("dispatch_grant_digest")
                != canonical_digest(effective_grant)
                or provider_authority.get("workspace_id")
                != str(effective_grant.workspace_id)
            ):
                raise RuntimeError(
                    f"{role.value} provider authority differs from its handler grant"
                )
        if qualification_source_revision(ROOT) != source_revision:
            raise RuntimeError(
                "qualification source changed during the live handoff"
            )

        history_payload = [
            record.model_dump(mode="json") for record in history_after_restart
        ]

        def grant_evidence(
            role: AgentRole,
            grant: Any,
            *,
            provider_authority: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            evidence: dict[str, Any] = {
                "grant_digest": canonical_digest(grant),
                "workspace_id": str(grant.workspace_id),
                "workspace": f"<{role.value}-workspace>",
                "baseline_commit": grant.baseline_commit,
                "grant_revision": grant.grant_revision,
                "allowed_paths": list(grant.allowed_paths),
                "allowed_argv": [
                    [
                        Path(argv[0]).name,
                        *(
                            "<role-workspace>"
                            if value == grant.workspace_root
                            else value
                            for value in argv[1:]
                        ),
                    ]
                    for argv in grant.allowed_argv
                ],
                "allowed_hosts": list(grant.allowed_hosts),
                "allowed_side_effects": [
                    item.value for item in grant.allowed_side_effects
                ],
                "secret_refs": list(grant.secret_refs),
            }
            if provider_authority is not None:
                evidence["provider_capability_digest"] = (
                    provider_authority["capability_digest"]
                )
                evidence["provider_dispatch_grant_digest"] = (
                    provider_authority["dispatch_grant_digest"]
                )
            return evidence

        evidence = {
            "schema_version": "2.0",
            "stage_task_id": "V04-13-T01G",
            "source_revision": source_revision,
            "enterprise_denominator": False,
            "assignment_id": str(stored_assignment.assignment_id),
            "assignment_status": archived.status.value,
            "work_item_status": closed.status.value,
            "software_fixture": {
                "kind": "unrelated_plain_python_git_repository",
                "baseline_commit": baseline,
                "source_unchanged": True,
            },
            "authority": {
                role.value: grant_evidence(role, grant)
                for role, grant in grants.items()
            },
            "effective_handler_authority": {
                role.value: grant_evidence(
                    role,
                    grant,
                    provider_authority=provider_authorities[role],
                )
                for role, grant in dispatched_grants.items()
            },
            "codex_implementation": codex_evidence,
            "lilies_review": lilies_evidence,
            "provider_cost_control": [
                {
                    "reservation_id": str(
                        reservation.cost_cap.reservation_id
                    ),
                    "provider_request_id": (
                        reservation.cost_cap.provider_request_id
                    ),
                    "provider": reservation.cost_cap.provider,
                    "model": reservation.cost_cap.model,
                    "worst_case_cost_usd": (
                        reservation.cost_cap.worst_case_cost_usd
                    ),
                    "authorization_evidence_digest": (
                        reservation.cost_cap.evidence_digest
                    ),
                    "actual_cost_usd": (
                        reservation.receipt.cost_usd
                        if reservation.receipt is not None
                        else None
                    ),
                    "input_tokens": (
                        reservation.receipt.input_tokens
                        if reservation.receipt is not None
                        else None
                    ),
                    "output_tokens": (
                        reservation.receipt.output_tokens
                        if reservation.receipt is not None
                        else None
                    ),
                    "receipt_evidence_digest": (
                        reservation.receipt.evidence_digest
                        if reservation.receipt is not None
                        else None
                    ),
                    "provider_capability_digest": (
                        trusted_provider_capabilities[
                            reservation.cost_cap.provider_request_id
                        ].capability_digest
                    ),
                    "dispatch_grant_digest": (
                        trusted_provider_capabilities[
                            reservation.cost_cap.provider_request_id
                        ].dispatch_grant_digest
                    ),
                    "provider_hosts": list(
                        trusted_provider_capabilities[
                            reservation.cost_cap.provider_request_id
                        ].endpoint_hosts
                    ),
                    "secret_refs": list(
                        trusted_provider_capabilities[
                            reservation.cost_cap.provider_request_id
                        ].secret_refs
                    ),
                    "provider_side_effects": [
                        effect.value
                        for effect in trusted_provider_capabilities[
                            reservation.cost_cap.provider_request_id
                        ].allowed_side_effects
                    ],
                    "credential_identity": (
                        trusted_provider_capabilities[
                            reservation.cost_cap.provider_request_id
                        ].credential_identity
                    ),
                    "settled": reservation.status == "settled",
                }
                for reservation in provider_costs
            ],
            "budget_ledger": {
                "tool_calls": sum(record.tool_calls for record in tool_usage),
                "commands": sum(record.commands for record in tool_usage),
                "completed_records": len(tool_usage),
                "by_role": {
                    role.value: {
                        "tool_calls": sum(
                            record.tool_calls
                            for record in tool_usage
                            if record.actor_role == role
                        ),
                        "commands": sum(
                            record.commands
                            for record in tool_usage
                            if record.actor_role == role
                        ),
                    }
                    for role in (AgentRole.codex, AgentRole.lilies)
                },
                "provider_reservations": len(provider_costs),
                "provider_settled": sum(
                    reservation.status == "settled"
                    for reservation in provider_costs
                ),
                "within_assignment_budget": (
                    sum(record.tool_calls for record in tool_usage)
                    <= stored_assignment.budget.max_tool_calls
                    and sum(record.commands for record in tool_usage)
                    <= stored_assignment.budget.max_commands
                    and sum(
                        (
                            reservation.receipt.cost_usd
                            if reservation.receipt is not None
                            else reservation.cost_cap.worst_case_cost_usd
                        )
                        for reservation in provider_costs
                    )
                    <= stored_assignment.budget.max_cost_usd
                ),
            },
            "actual_lifecycle": {
                "events": event_types,
                "required_events_present": True,
                "dispatch_history": history_payload,
                "dispatch_history_restart_equal": True,
                "original_grants_unchanged": original_grants_unchanged,
                "independent_review_snapshot": lilies_evidence.get(
                    "independent_snapshot"
                )
                is True,
            },
        }
        evidence = _sanitize_evidence_text(
            evidence,
            replacements=tuple(
                sorted(
                    (
                        (str(root), "<run-root>"),
                        (str(ROOT), "<repository-root>"),
                        (str(Path.home()), "<home>"),
                        (sys.executable, "<python>"),
                        (codex, "<codex-cli>"),
                    ),
                    key=lambda item: len(item[0]),
                    reverse=True,
                )
            ),
        )
        completed_evidence = {
            **evidence,
            "status": "passed",
        }
        record = {
            **completed_evidence,
            "evidence_digest": canonical_digest(completed_evidence),
        }
        wrapper = {
            "kind": "bounded_live_lilies_codex_handoff",
            "stage_task_id": "V04-13-T01G",
            "source_revision": source_revision,
            "enterprise_denominator": False,
            "status": "passed",
            "record": record,
        }
        return {
            **wrapper,
            "evidence_digest": canonical_digest(wrapper),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run one real bounded Codex-to-Lilies handoff through the durable "
            "collaborative-development worker."
        )
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    try:
        evidence = asyncio.run(run(timeout_seconds=args.timeout_seconds))
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "type": type(error).__name__,
                        "message": str(error)[:1_000],
                    },
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        destination = args.output.resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered, encoding="utf-8")
        destination.chmod(0o600)
        print(
            json.dumps(
                {
                    "status": evidence["status"],
                    "output": str(destination),
                    "evidence_digest": evidence["evidence_digest"],
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
