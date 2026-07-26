from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
import uvicorn

from .lilies_client import (
    ALL_LOCAL_SCOPES,
    PLATFORM_PAIRING_SCOPES,
    LiliesClient,
    LiliesClientError,
)
from .lilies_config import LiliesSettings
from .lilies_daemon import (
    daemon_record_is_live,
    is_loopback_address,
    read_daemon_info,
    remove_daemon_info,
    write_daemon_info,
)


SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "token",
)
TERMINAL_TURN_EVENTS = frozenset(
    {"turn.completed", "turn.failed", "turn.cancelled", "turn.finished"}
)
HELP_TEXT = """Commands:
  /help                 show this help
  /status               show the current session
  /sessions             list local sessions
  /resume               explicitly resume an interrupted/error session
  /cancel               cancel the active turn or assignment
  /inspect <event-id>   show a redacted event received by this CLI
  /exit                 detach without stopping the daemon
"""


class LiliesDevelopmentCliError(RuntimeError):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lilies",
        description="Run and talk to the standalone local Lilies agent.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="private Lilies state directory (default: LILIES_DATA_DIR or ~/.lilies)",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="workspace root owned by the local agent",
    )
    parser.add_argument("--version", action="version", version="Lilies 0.4.13")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="start the local daemon")
    serve.add_argument("--host", default=None, help="listen address (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=None, help="listen port (default: 8765)")
    serve.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="acknowledge the risk of exposing the local daemon",
    )
    serve.add_argument("--log-level", default="info", choices=("debug", "info", "warning", "error"))

    chat = subparsers.add_parser("chat", help="create or resume an interactive session")
    chat.add_argument("prompt", nargs="*", help="optional first message")
    chat.add_argument("--session", help="resume an existing session; never creates that ID")
    chat.add_argument("--no-start", action="store_true", help="do not auto-start a missing daemon")
    chat.add_argument("--start-timeout", type=float, default=10.0, metavar="SECONDS")

    subparsers.add_parser("sessions", help="list sessions")

    attach = subparsers.add_parser("attach", help="attach to an existing session")
    attach.add_argument("session_id")

    subparsers.add_parser("status", help="show daemon status")

    develop = subparsers.add_parser(
        "develop",
        help="create and control a platform-neutral software collaboration",
    )
    develop.add_argument(
        "--base-url",
        default=os.environ.get(
            "LILIES_COLLABORATIVE_DEVELOPMENT_BASE_URL",
            "http://127.0.0.1:8780",
        ),
    )
    develop.add_argument("--token-file", type=Path)
    develop.add_argument(
        "--assignment-file",
        help="DevelopmentAssignment JSON; omit only for a nested control command",
    )
    develop.add_argument(
        "--idempotency-key",
        help="idempotency key for assignment creation",
    )
    develop_commands = develop.add_subparsers(dest="develop_command")

    develop_status = develop_commands.add_parser(
        "status",
        help="show assignment status and pending authority requests",
    )
    develop_status.add_argument("assignment_id", type=UUID)

    develop_approve = develop_commands.add_parser(
        "approve",
        help="dispatch a manual work item or decide a durable authority request",
    )
    develop_approve.add_argument("assignment_id", type=UUID)
    approval_target = develop_approve.add_mutually_exclusive_group()
    approval_target.add_argument("--work-item", type=UUID)
    approval_target.add_argument("--authority-request", type=UUID)
    develop_approve.add_argument(
        "--decision",
        choices=("approve", "reject"),
        default="approve",
    )
    develop_approve.add_argument(
        "--status",
        choices=("pending", "approved", "rejected", "all"),
        default="pending",
        help="filter when listing authority requests",
    )
    develop_approve.add_argument("--expected-revision", type=int)
    develop_approve.add_argument(
        "--grant-file",
        help="complete replacement WorkspaceGrant JSON for authority approval",
    )
    develop_approve.add_argument(
        "--budget-file",
        help="replacement DevelopmentBudget JSON when the request includes budget",
    )
    develop_approve.add_argument("--reason")
    develop_approve.add_argument("--idempotency-key")

    develop_stop = develop_commands.add_parser(
        "stop",
        help="stop an assignment and revoke its role credentials",
    )
    develop_stop.add_argument("assignment_id", type=UUID)
    develop_stop.add_argument("--expected-revision", type=int, required=True)
    develop_stop.add_argument("--idempotency-key", required=True)

    pair = subparsers.add_parser(
        "pair",
        help="create a ten-minute one-time code for a local platform client",
    )
    pair.add_argument(
        "--scope",
        action="append",
        choices=sorted(ALL_LOCAL_SCOPES),
        dest="scopes",
        help="allowed client scope; repeat to grant more than one",
    )

    subparsers.add_parser("stop", help="request an orderly daemon shutdown")
    return parser


def _settings_from_args(args: argparse.Namespace) -> LiliesSettings:
    values: dict[str, Any] = {}
    if args.data_dir is not None:
        values["data_dir"] = args.data_dir
    if args.workspace_root is not None:
        values["workspace_root"] = args.workspace_root
    if getattr(args, "host", None) is not None:
        values["host"] = args.host
    if getattr(args, "port", None) is not None:
        values["port"] = args.port
    settings = LiliesSettings(**values)
    settings.prepare()
    return settings


def _redact_text(value: str) -> str:
    lowered = value.casefold()
    if "hidden_oracle" in lowered or "/hidden/" in lowered or "\\hidden\\" in lowered:
        return "[redacted hidden verifier path]"
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", value)
    return re.sub(
        r"(?i)\b(authorization|cookie|password|secret|token|access[_-]?token|"
        r"refresh[_-]?token|api[_-]?key|pairing[_-]?code)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        value,
    )


def _redact(value: Any, *, key: str = "") -> Any:
    lowered_key = key.casefold()
    if any(part in lowered_key for part in SENSITIVE_KEY_PARTS) or lowered_key in {
        "api_key",
        "pairing_code",
        "private_key",
    }:
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _print_json(value: Any, *, stream: Any = None) -> None:
    destination = stream or sys.stdout
    rendered = json.dumps(_redact(value), ensure_ascii=False, indent=2, sort_keys=True)
    if len(rendered) > 20_000:
        rendered = f"{rendered[:20_000]}\n... [truncated]"
    print(rendered, file=destination)


def _read_development_token(path: Path | None) -> str:
    if path is None:
        value = os.environ.get("LILIES_COLLABORATIVE_DEVELOPMENT_TOKEN", "").strip()
        if len(value) < 32:
            raise LiliesDevelopmentCliError(
                "set LILIES_COLLABORATIVE_DEVELOPMENT_TOKEN or use --token-file"
            )
        return value
    if path.is_symlink():
        raise LiliesDevelopmentCliError("development token file must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise LiliesDevelopmentCliError(
            "development token file is not readable"
        ) from error
    try:
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
                raise LiliesDevelopmentCliError(
                    "development token file must be a regular file with mode 0600"
                )
            value = handle.read(4_097).strip()
    except (OSError, UnicodeError) as error:
        raise LiliesDevelopmentCliError(
            "development token file is not readable"
        ) from error
    if not 32 <= len(value) <= 4_096:
        raise LiliesDevelopmentCliError(
            "development token must contain between 32 and 4096 characters"
        )
    return value


def _read_development_json(
    path: str,
    *,
    label: str = "development JSON",
) -> dict[str, Any]:
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise LiliesDevelopmentCliError(f"{label} is not readable") from error
    if len(raw) > 2 * 1024 * 1024:
        raise LiliesDevelopmentCliError(f"{label} exceeds 2 MiB")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LiliesDevelopmentCliError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise LiliesDevelopmentCliError(f"{label} must be an object")
    return value


class _DevelopmentClient:
    def __init__(self, *, base_url: str, access_token: str) -> None:
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise LiliesDevelopmentCliError(
                "development base URL must be a plain http(s) origin"
            )
        host = parsed.host
        assert host is not None
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if parsed.scheme == "http" and not loopback:
            raise LiliesDevelopmentCliError(
                "plaintext development HTTP is allowed only for a loopback origin"
            )
        self.base_url = str(parsed).rstrip("/")
        self.access_token = access_token

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    **({"Content-Type": "application/json"} if body is not None else {}),
                },
                json=body,
                timeout=30,
            )
        except httpx.HTTPError as error:
            raise LiliesDevelopmentCliError(
                "collaborative development API is unavailable"
            ) from error
        try:
            payload = response.json()
        except ValueError:
            payload = {"status": response.status_code}
        if response.is_error:
            detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
            if isinstance(detail, dict):
                code = str(detail.get("code", "request_failed"))
                message = str(detail.get("message", "request failed"))
            else:
                code = "request_failed"
                message = "request failed"
            raise LiliesDevelopmentCliError(
                f"{code}: {message} (HTTP {response.status_code})"
            )
        return payload


def _required_development_argument(
    value: Any,
    *,
    option: str,
) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise LiliesDevelopmentCliError(f"{option} is required")
    return value


def _run_develop(args: argparse.Namespace) -> int:
    client = _DevelopmentClient(
        base_url=args.base_url,
        access_token=_read_development_token(args.token_file),
    )
    command = args.develop_command
    if command is None:
        assignment_file = _required_development_argument(
            args.assignment_file,
            option="--assignment-file",
        )
        idempotency_key = _required_development_argument(
            args.idempotency_key,
            option="--idempotency-key",
        )
        result = client.request(
            "POST",
            "/api/v1/collaborative-development/assignments",
            {
                "idempotency_key": idempotency_key,
                "assignment": _read_development_json(
                    assignment_file,
                    label="development assignment JSON",
                ),
            },
        )
        _print_json(result)
        return 0

    assignment_id = args.assignment_id
    assignment_path = (
        f"/api/v1/collaborative-development/assignments/{assignment_id}"
    )
    if command == "status":
        status = client.request("GET", f"{assignment_path}/status")
        authority = client.request(
            "GET",
            f"{assignment_path}/authority-requests?"
            + urlencode({"status": "pending"}),
        )
        if isinstance(status, dict):
            status = {
                **status,
                "pending_authority_requests": authority.get("requests", [])
                if isinstance(authority, dict)
                else [],
            }
        _print_json(status)
        return 0

    if command == "approve":
        if args.work_item is None and args.authority_request is None:
            result = client.request(
                "GET",
                f"{assignment_path}/authority-requests?"
                + urlencode({"status": args.status}),
            )
            _print_json(result)
            return 0
        idempotency_key = _required_development_argument(
            args.idempotency_key,
            option="--idempotency-key",
        )
        if args.authority_request is not None:
            reason = _required_development_argument(args.reason, option="--reason")
            body: dict[str, Any] = {
                "idempotency_key": idempotency_key,
                "reason": reason,
            }
            if args.decision == "approve":
                expected_revision = _required_development_argument(
                    args.expected_revision,
                    option="--expected-revision",
                )
                grant_file = _required_development_argument(
                    args.grant_file,
                    option="--grant-file",
                )
                body.update(
                    {
                        "expected_assignment_revision": expected_revision,
                        "replacement_grant": _read_development_json(
                            grant_file,
                            label="replacement grant JSON",
                        ),
                    }
                )
                if args.budget_file is not None:
                    body["replacement_budget"] = _read_development_json(
                        args.budget_file,
                        label="replacement budget JSON",
                    )
            elif (
                args.expected_revision is not None
                or args.grant_file is not None
                or args.budget_file is not None
            ):
                raise LiliesDevelopmentCliError(
                    "authority rejection cannot carry a grant, budget, or revision"
                )
            result = client.request(
                "POST",
                f"{assignment_path}/authority-requests/"
                f"{args.authority_request}/{args.decision}",
                body,
            )
            _print_json(result)
            return 0
        expected_revision = _required_development_argument(
            args.expected_revision,
            option="--expected-revision",
        )
        items = client.request("GET", f"{assignment_path}/work-items")
        if not isinstance(items, list) or not any(
            str(item.get("work_item_id")) == str(args.work_item)
            for item in items
            if isinstance(item, dict)
        ):
            raise LiliesDevelopmentCliError(
                "work item does not belong to the selected assignment"
            )
        result = client.request(
            "POST",
            f"/api/v1/collaborative-development/work-items/"
            f"{args.work_item}/dispatch",
            {
                "idempotency_key": idempotency_key,
                "expected_revision": expected_revision,
            },
        )
        _print_json(result)
        return 0

    if command == "stop":
        result = client.request(
            "POST",
            f"{assignment_path}/stop",
            {
                "idempotency_key": args.idempotency_key,
                "expected_revision": args.expected_revision,
            },
        )
        _print_json(result)
        return 0
    raise AssertionError(f"unhandled develop command: {command}")


def _session_id(value: dict[str, Any]) -> str:
    candidate = value.get("session_id", value.get("id"))
    if not isinstance(candidate, str) or not candidate:
        raise LiliesClientError("daemon returned a session without a session_id")
    return candidate


def _client_for(settings: LiliesSettings) -> LiliesClient:
    return LiliesClient(settings)


def _daemon_health(client: LiliesClient) -> dict[str, Any]:
    try:
        return client.health()
    except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError) as error:
        raise LiliesClientError("local Lilies daemon is not running") from error


def _start_background_daemon(settings: LiliesSettings, *, timeout: float) -> None:
    if timeout <= 0:
        raise LiliesClientError("--start-timeout must be greater than zero")
    if settings.daemon_file.exists() and not daemon_record_is_live(settings):
        settings.daemon_file.unlink(missing_ok=True)

    host = settings.host if is_loopback_address(settings.host) else "127.0.0.1"
    command = [
        sys.executable,
        "-m",
        "agent_platform.lilies_cli",
        "--data-dir",
        str(settings.data_dir),
    ]
    if settings.workspace_root is not None:
        command.extend(("--workspace-root", str(settings.workspace_root)))
    command.extend(("serve", "--host", host, "--port", str(settings.port)))
    process = subprocess.Popen(  # noqa: S603 - arguments are fixed and never use a shell
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + timeout
    client = _client_for(settings)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LiliesClientError(
                "background daemon exited during startup; run `lilies serve` to inspect the error"
            )
        try:
            client.health()
            return
        except (LiliesClientError, FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError):
            time.sleep(0.05)
    process.terminate()
    raise LiliesClientError("timed out waiting for the local Lilies daemon")


def _connect_for_chat(
    settings: LiliesSettings,
    *,
    no_start: bool,
    start_timeout: float,
) -> LiliesClient:
    client = _client_for(settings)
    try:
        _daemon_health(client)
        return client
    except LiliesClientError:
        if no_start:
            raise LiliesClientError(
                "local Lilies daemon is not running (--no-start was specified)"
            ) from None
        if settings.daemon_file.exists() and daemon_record_is_live(settings):
            raise LiliesClientError("local Lilies daemon exists but its health endpoint is unavailable")
    _start_background_daemon(settings, timeout=start_timeout)
    _daemon_health(client)
    return client


def _run_serve(settings: LiliesSettings, args: argparse.Namespace) -> int:
    if not is_loopback_address(settings.host):
        if not args.allow_non_loopback:
            raise LiliesClientError(
                "refusing a non-loopback bind; pass --allow-non-loopback to acknowledge the risk"
            )
        print(
            "WARNING: the Lilies daemon is exposed beyond this machine; local scopes are not "
            "enterprise IAM.",
            file=sys.stderr,
        )
    if daemon_record_is_live(settings):
        daemon = read_daemon_info(settings)
        raise LiliesClientError(f"a Lilies daemon is already running at {daemon['address']}")
    settings.daemon_file.unlink(missing_ok=True)

    from .lilies_api import create_lilies_app

    app = create_lilies_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        access_log=False,
    )
    server = uvicorn.Server(config)

    def request_daemon_stop() -> None:
        server.should_exit = True

    app.state.request_daemon_stop = request_daemon_stop
    record = write_daemon_info(settings)
    print(
        f"Lilies daemon listening at {record['address']} "
        f"({record['daemon_fingerprint'][:23]}…)",
        file=sys.stderr,
    )
    try:
        server.run()
    finally:
        remove_daemon_info(settings, expected_pid=os.getpid())
    return 0


def _print_sessions(sessions: list[dict[str, Any]]) -> None:
    if not sessions:
        print("No sessions.")
        return
    print("SESSION                               STATE                 ASSIGNMENT   UPDATED")
    for session in sessions:
        session_id = str(session.get("session_id", session.get("id", "?")))
        status = str(session.get("status", "unknown"))
        assignment = str(session.get("assignment_id") or "-")
        updated_at = str(session.get("updated_at", "-"))
        print(f"{session_id[:36]:36}  {status[:20]:20}  {assignment[:12]:12} {updated_at[:25]}")


def _status_line(status: dict[str, Any]) -> str:
    model = status.get("model", status.get("model_profile", "unknown"))
    platform = _platform_status(status)
    active = status.get(
        "active_tasks",
        status.get("active_sessions", status.get("active_session_count", 0)),
    )
    return f"daemon: running   model: {model}   platform: {platform}   active: {active}"


def _platform_status(status: dict[str, Any]) -> str:
    if "platform_paired" in status:
        return "connected" if status["platform_paired"] else "disconnected"
    return str(status.get("platform", status.get("platform_status", "disconnected")))


def _event_payload(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    kind = str(event.get("type", event.get("kind", "event")))
    data = event.get("data", {})
    if not isinstance(data, dict):
        data = {"value": data}
    return kind, data


def _prompt_permission(
    client: LiliesClient,
    session_id: str,
    data: dict[str, Any],
    *,
    input_fn: Callable[[str], str],
) -> None:
    request = data.get("permission_request", data)
    if not isinstance(request, dict):
        print("permission requested (details unavailable)")
        return
    request_id = request.get("request_id")
    if not isinstance(request_id, str):
        print("permission requested (request ID unavailable)")
        return
    tool = request.get("tool_name", request.get("tool", "unknown tool"))
    input_digest = request.get("input_digest")
    if not isinstance(input_digest, str):
        print("permission requested (input digest unavailable)")
        return
    print(f"\npermission required: {tool}")
    redacted_input = request.get("redacted_input", request.get("input_summary"))
    if redacted_input:
        _print_json(redacted_input)
    while True:
        try:
            answer = input_fn("Allow? [allow/deny]: ").strip().casefold()
        except EOFError:
            answer = "deny"
            print("input closed; denying permission")
        if answer in {"allow", "a", "yes", "y"}:
            behavior = "allow"
            break
        if answer in {"deny", "d", "no", "n"}:
            behavior = "deny"
            break
        print("Enter allow or deny.")
    client.resolve_permission(
        session_id,
        request_id,
        behavior=behavior,
        expected_input_digest=input_digest,
    )
    print(f"permission {behavior}ed")


def _render_event(
    client: LiliesClient,
    session_id: str,
    event: dict[str, Any],
    *,
    input_fn: Callable[[str], str],
) -> None:
    kind, data = _event_payload(event)
    if kind in {"model.text.delta", "agent.text.delta"}:
        print(_redact_text(str(data.get("text", ""))), end="", flush=True)
    elif kind == "tool.started":
        print(f"\n[tool] {data.get('tool', 'unknown')} …")
    elif kind in {"tool.completed", "tool.failed"}:
        outcome = "done" if kind.endswith("completed") else "failed"
        print(f"[tool] {data.get('tool', 'unknown')}: {outcome}")
    elif kind in {"permission.requested", "permission_required"}:
        _prompt_permission(client, session_id, data, input_fn=input_fn)
    elif kind == "context.compaction.completed":
        print("\n[context compacted]")
    elif kind == "turn.completed":
        usage = data.get("usage", {})
        cost = usage.get("cost_usd") if isinstance(usage, dict) else None
        suffix = f" cost=${float(cost):.4f}" if isinstance(cost, int | float) else ""
        print(f"\n[turn completed{suffix}]")
    elif kind == "turn.failed":
        print(f"\n[turn failed: {_redact_text(str(data.get('error', 'unknown error')))}]")
    elif kind == "turn.cancelled":
        print("\n[turn cancelled]")
    elif kind == "turn.finished":
        status = str(data.get("status", "finished"))
        error = data.get("error")
        suffix = f": {_redact_text(str(error))}" if error else ""
        print(f"\n[turn {status}{suffix}]")


def _consume_turn_events(
    client: LiliesClient,
    session_id: str,
    *,
    after: int,
    event_cache: dict[int, dict[str, Any]],
    input_fn: Callable[[str], str],
) -> int:
    cursor = after
    for event in client.iter_events(session_id, after=after):
        event_id = event.get("id")
        if isinstance(event_id, int):
            cursor = max(cursor, event_id)
            event_cache[event_id] = _redact(event)
        _render_event(client, session_id, event, input_fn=input_fn)
        if isinstance(event_id, int):
            client.ack(session_id, event_id)
        kind, _ = _event_payload(event)
        if kind in TERMINAL_TURN_EVENTS:
            break
    return cursor


def _print_session_header(client: LiliesClient, session: dict[str, Any]) -> None:
    session_id = _session_id(session)
    status = session.get("status", "unknown")
    try:
        daemon_status = client.status()
    except LiliesClientError:
        daemon_status = {}
    model = daemon_status.get("model", daemon_status.get("model_profile", "unknown"))
    platform = _platform_status(daemon_status)
    print("莉莉丝")
    print(f"session: {session_id[:12]}   model: {model}   platform: {platform}")
    print(f"state: {status}\n")


def _handle_interactive_command(
    command: str,
    *,
    client: LiliesClient,
    session_id: str,
    event_cache: dict[int, dict[str, Any]],
    cursor: int,
    input_fn: Callable[[str], str],
) -> tuple[bool, int]:
    name, _, argument = command.partition(" ")
    if name == "/exit":
        return False, cursor
    if name == "/help":
        print(HELP_TEXT, end="")
    elif name == "/status":
        _print_json(client.get_session(session_id))
    elif name == "/sessions":
        _print_sessions(client.sessions())
    elif name == "/resume":
        previous_status = client.get_session(session_id).get("status")
        client.resume(session_id)
        print("resume requested")
        if previous_status in {"interrupted", "error"}:
            cursor = _consume_turn_events(
                client,
                session_id,
                after=cursor,
                event_cache=event_cache,
                input_fn=input_fn,
            )
    elif name == "/cancel":
        client.cancel(session_id)
        print("cancel requested")
    elif name == "/inspect":
        try:
            event_id = int(argument.strip())
        except ValueError:
            print("usage: /inspect <event-id>")
        else:
            event = event_cache.get(event_id)
            if event is None:
                print("event is not in this CLI's received-event cache")
            else:
                _print_json(event)
    else:
        print("unknown command; use /help")
    return True, cursor


def _interactive_session(
    client: LiliesClient,
    session: dict[str, Any],
    *,
    first_message: str | None = None,
    input_fn: Callable[[str], str] = input,
) -> int:
    session_id = _session_id(session)
    _print_session_header(client, session)
    event_cache: dict[int, dict[str, Any]] = {}
    cursor = int(session.get("event_cursor", session.get("last_event_cursor", 0)) or 0)
    pending = first_message
    while True:
        if pending is None:
            try:
                pending = input_fn("> ").strip()
            except EOFError:
                print()
                return 0
        if not pending:
            pending = None
            continue
        if pending.startswith("/"):
            keep_running, cursor = _handle_interactive_command(
                pending,
                client=client,
                session_id=session_id,
                event_cache=event_cache,
                cursor=cursor,
                input_fn=input_fn,
            )
            pending = None
            if not keep_running:
                return 0
            continue
        operation = client.send_message(session_id, pending)
        operation_cursor = operation.get("event_cursor", cursor)
        if isinstance(operation_cursor, int):
            cursor = min(cursor, operation_cursor - 1) if operation_cursor > 0 else cursor
        cursor = _consume_turn_events(
            client,
            session_id,
            after=cursor,
            event_cache=event_cache,
            input_fn=input_fn,
        )
        pending = None


def _run_chat(settings: LiliesSettings, args: argparse.Namespace) -> int:
    client = _connect_for_chat(
        settings,
        no_start=args.no_start,
        start_timeout=args.start_timeout,
    )
    if args.session:
        try:
            session = client.get_session(args.session)
        except LiliesClientError as error:
            raise LiliesClientError(
                f"session {args.session!r} does not exist or is not accessible; no session was created"
            ) from error
    else:
        session = client.create_session()
    first_message = " ".join(args.prompt).strip() or None
    return _interactive_session(client, session, first_message=first_message)


def _run_attach(settings: LiliesSettings, session_id: str) -> int:
    client = _client_for(settings)
    _daemon_health(client)
    try:
        session = client.get_session(session_id)
    except LiliesClientError as error:
        raise LiliesClientError(f"session {session_id!r} does not exist or is not accessible") from error
    return _interactive_session(client, session)


def _dispatch(args: argparse.Namespace, settings: LiliesSettings) -> int:
    if args.command == "serve":
        return _run_serve(settings, args)
    if args.command == "chat":
        return _run_chat(settings, args)
    if args.command == "attach":
        return _run_attach(settings, args.session_id)

    client = _client_for(settings)
    if args.command == "status":
        try:
            health = _daemon_health(client)
        except LiliesClientError:
            print("daemon: stopped")
            return 1
        status = client.status()
        print(_status_line(status))
        fingerprint = read_daemon_info(settings)["daemon_fingerprint"]
        print(f"fingerprint: {fingerprint}")
        schema = health.get("schema_version")
        if schema:
            print(f"schema: {schema}")
        return 0
    _daemon_health(client)
    if args.command == "sessions":
        _print_sessions(client.sessions())
        return 0
    if args.command == "pair":
        requested_scopes = args.scopes or PLATFORM_PAIRING_SCOPES
        pairing = client.create_pairing_code(requested_scopes)
        print("One-time Lilies pairing code (expires in ten minutes):")
        print(f"code: {pairing['pairing_code']}")
        print(f"fingerprint: {pairing['daemon_fingerprint']}")
        scopes = pairing.get("allowed_scopes", [])
        if scopes:
            print(f"scopes: {', '.join(map(str, scopes))}")
        expires_at = pairing.get("expires_at")
        if expires_at:
            print(f"expires: {expires_at}")
        return 0
    if args.command == "stop":
        result = client.stop()
        print(f"stop requested: {result.get('status', 'accepted')}")
        return 0
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "develop":
            return _run_develop(args)
        settings = _settings_from_args(args)
        return _dispatch(args, settings)
    except KeyboardInterrupt:
        print("\ndetached; daemon remains running", file=sys.stderr)
        return 130
    except (
        LiliesClientError,
        LiliesDevelopmentCliError,
        FileNotFoundError,
        PermissionError,
        ValueError,
    ) as error:
        print(f"lilies: {_redact_text(str(error))}", file=sys.stderr)
        return 1


# Migration-only command helpers remain importable for historical tests and
# archive replay.  The executable module entry was intentionally retired in
# T01K; production launchers must invoke the installed sibling
# ``lilies_agent.cli`` distribution instead.
if __name__ == "__main__":
    print(
        "agent_platform.lilies_cli is retired; run the standalone "
        "lilies-local-agent distribution instead",
        file=sys.stderr,
    )
    raise SystemExit(2)
