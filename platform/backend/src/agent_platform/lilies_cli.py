from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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
        settings = _settings_from_args(args)
        return _dispatch(args, settings)
    except KeyboardInterrupt:
        print("\ndetached; daemon remains running", file=sys.stderr)
        return 130
    except (LiliesClientError, FileNotFoundError, PermissionError, ValueError) as error:
        print(f"lilies: {_redact_text(str(error))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
