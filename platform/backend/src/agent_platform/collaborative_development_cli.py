"""Standalone CLI for reusable Lilies/Codex collaborative development."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import signal
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import uvicorn

from .collaborative_development_api import (
    create_standalone_collaborative_development_app,
)
from .collaborative_development_auth import DevelopmentCredentialIssuer
from .collaborative_development_models import AgentRole
from .collaborative_development_service import CollaborativeDevelopmentService
from .collaborative_development_storage import CollaborativeDevelopmentStore
from .collaborative_development_worker import (
    AutonomousDevelopmentLifecycleBridge,
    ExternalJsonArgvDispatchHandler,
    WorkerBatch,
    run_dispatch_worker,
)
from .development_workspace_broker import DevelopmentWorkspaceBroker


_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_ARGV_JSON_BYTES = 64 * 1024


class CollaborativeDevelopmentCliError(RuntimeError):
    pass


def _environment_flag(name: str, *, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CollaborativeDevelopmentCliError(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off"
    )


def _add_revision_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--idempotency-key", required=True)


def _add_work_revision_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-work-item-revision", type=int, required=True)
    parser.add_argument("--idempotency-key", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lilies-collab",
        description=(
            "Run role-scoped Lilies/Codex software collaboration without the "
            "workflow platform."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get(
            "LILIES_COLLABORATIVE_DEVELOPMENT_BASE_URL",
            "http://127.0.0.1:8780",
        ),
    )
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--version", action="version", version="Lilies Collab 0.4.13")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="start only the collaboration API")
    serve.add_argument("--data-dir", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8780)
    serve.add_argument("--owner-token-file", type=Path, required=True)
    serve.add_argument("--signing-key-file", type=Path, required=True)

    worker = subparsers.add_parser(
        "worker",
        help="run the platform-neutral durable collaboration dispatcher",
    )
    worker.add_argument("--data-dir", type=Path, required=True)
    worker.add_argument("--journal-file", type=Path)
    worker.add_argument("--workspace-state-dir", type=Path)
    worker_mode = worker.add_mutually_exclusive_group(required=True)
    worker_mode.add_argument("--once", action="store_true")
    worker_mode.add_argument("--continuous", action="store_true")
    worker.add_argument("--lilies-handler-argv-file", type=Path)
    worker.add_argument("--codex-handler-argv-file", type=Path)
    worker.add_argument("--adapter-timeout-seconds", type=int, default=300)
    worker.add_argument("--adapter-retry-seconds", type=int, default=30)
    worker.add_argument("--poll-interval-seconds", type=float, default=1)
    worker.add_argument("--limit", type=int, default=100)
    worker.add_argument("--claim-ttl-seconds", type=int, default=900)
    worker.add_argument("--lease-ttl-seconds", type=int, default=300)
    worker.add_argument("--dispatcher-id")

    create = subparsers.add_parser("create", help="create a DevelopmentAssignment")
    create.add_argument("--assignment-file", required=True)
    create.add_argument("--idempotency-key", required=True)

    show = subparsers.add_parser("show", help="show an assignment")
    show.add_argument("assignment_id", type=UUID)

    status = subparsers.add_parser("status", help="show assignment status")
    status.add_argument("assignment_id", type=UUID)

    mode = subparsers.add_parser("mode", help="switch manual/autonomous dispatch")
    mode.add_argument("assignment_id", type=UUID)
    mode.add_argument("--mode", choices=("manual_dispatch", "autonomous"), required=True)
    _add_revision_arguments(mode)

    approval = subparsers.add_parser("approval", help="switch handoff approval mode")
    approval.add_argument("assignment_id", type=UUID)
    approval.add_argument("--mode", choices=("manual", "auto_forward"), required=True)
    _add_revision_arguments(approval)

    for command in ("stop", "archive"):
        action = subparsers.add_parser(command, help=f"{command} an assignment")
        action.add_argument("assignment_id", type=UUID)
        _add_revision_arguments(action)

    work_create = subparsers.add_parser("work-create", help="create a work item")
    work_create.add_argument("assignment_id", type=UUID)
    work_create.add_argument("--work-item-file", required=True)
    work_create.add_argument("--idempotency-key", required=True)

    work_list = subparsers.add_parser("work-list", help="list assignment work items")
    work_list.add_argument("assignment_id", type=UUID)

    dispatch = subparsers.add_parser("dispatch", help="approve and dispatch a work item")
    dispatch.add_argument("work_item_id", type=UUID)
    _add_revision_arguments(dispatch)

    lease = subparsers.add_parser("lease", help="acquire an assigned work item")
    lease.add_argument("work_item_id", type=UUID)
    lease.add_argument("--ttl-seconds", type=int, default=900)
    _add_revision_arguments(lease)

    start = subparsers.add_parser("start", help="start a leased work item")
    start.add_argument("lease_id", type=UUID)
    _add_work_revision_arguments(start)

    result = subparsers.add_parser("result", help="submit a DevelopmentResult")
    result.add_argument("work_item_id", type=UUID)
    result.add_argument("--result-file", required=True)
    _add_work_revision_arguments(result)

    result_show = subparsers.add_parser(
        "result-show",
        help="read one role-scoped DevelopmentResult",
    )
    result_show.add_argument("result_id", type=UUID)

    review_prepare = subparsers.add_parser(
        "review-prepare",
        help="materialize an immutable result snapshot for Lilies review",
    )
    review_prepare.add_argument("result_id", type=UUID)
    review_prepare.add_argument("--idempotency-key", required=True)

    review = subparsers.add_parser("review", help="submit a LiliesReview")
    review.add_argument("work_item_id", type=UUID)
    review.add_argument("--review-file", required=True)
    _add_work_revision_arguments(review)

    review_reconciliations = subparsers.add_parser(
        "review-reconciliations",
        help="list Lilies reviews stopped after an unknown handler outcome",
    )
    review_reconciliations.add_argument("assignment_id", type=UUID)

    review_requeue = subparsers.add_parser(
        "review-requeue",
        help="explicitly requeue one unknown-outcome Lilies review",
    )
    review_requeue.add_argument("assignment_id", type=UUID)
    review_requeue.add_argument("outbox_id", type=UUID)
    review_requeue.add_argument(
        "--expected-work-item-revision",
        type=int,
        required=True,
    )
    review_requeue.add_argument(
        "--expected-failed-attempt",
        type=int,
        required=True,
    )
    review_requeue.add_argument("--reason", required=True)
    review_requeue.add_argument("--idempotency-key", required=True)
    review_requeue.add_argument(
        "--confirm-unknown-outcome",
        action="store_true",
        required=True,
        help=(
            "acknowledge that the previous review may have had side effects "
            "and authorize a new attempt"
        ),
    )

    close = subparsers.add_parser("close", help="close an accepted work item")
    close.add_argument("work_item_id", type=UUID)
    _add_revision_arguments(close)

    events = subparsers.add_parser("events", help="read durable assignment events")
    events.add_argument("assignment_id", type=UUID)
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--limit", type=int, default=1_000)

    ack = subparsers.add_parser("ack", help="persist a reader cursor")
    ack.add_argument("assignment_id", type=UUID)
    ack.add_argument("--ack-seq", type=int, required=True)
    ack.add_argument("--expected-cursor-revision", type=int, required=True)
    ack.add_argument("--idempotency-key", required=True)

    authority = subparsers.add_parser(
        "authority",
        help="show this role's exact frozen workspace authority",
    )
    authority.add_argument("assignment_id", type=UUID)
    return parser


def _read_private_text(path: Path, *, label: str) -> str:
    if path.is_symlink():
        raise CollaborativeDevelopmentCliError(f"{label} file must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CollaborativeDevelopmentCliError(f"{label} file is not readable") from error
    try:
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            metadata = os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o777 != 0o600:
                raise CollaborativeDevelopmentCliError(
                    f"{label} file must be a regular file with mode 0600"
                )
            value = handle.read(4_097).strip()
    except (OSError, UnicodeError) as error:
        raise CollaborativeDevelopmentCliError(f"{label} file is not readable") from error
    if not 32 <= len(value) <= 4_096:
        raise CollaborativeDevelopmentCliError(
            f"{label} must contain between 32 and 4096 characters"
        )
    return value


def _access_token(args: argparse.Namespace) -> str:
    if args.token_file is not None:
        return _read_private_text(args.token_file, label="access token")
    value = os.environ.get("LILIES_COLLABORATIVE_DEVELOPMENT_TOKEN", "").strip()
    if len(value) < 32:
        raise CollaborativeDevelopmentCliError(
            "set LILIES_COLLABORATIVE_DEVELOPMENT_TOKEN or use --token-file"
        )
    return value


def _read_json(source: str) -> dict[str, Any]:
    try:
        raw = (
            sys.stdin.buffer.read(_MAX_JSON_BYTES + 1)
            if source == "-"
            else Path(source).read_bytes()
        )
    except OSError as error:
        raise CollaborativeDevelopmentCliError("JSON input is not readable") from error
    if len(raw) > _MAX_JSON_BYTES:
        raise CollaborativeDevelopmentCliError("JSON input exceeds 2 MiB")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CollaborativeDevelopmentCliError("input is not valid JSON") from error
    if not isinstance(value, dict):
        raise CollaborativeDevelopmentCliError("JSON input must be an object")
    return value


def _read_argv_json(path: Path) -> tuple[str, ...]:
    if path.is_symlink():
        raise CollaborativeDevelopmentCliError(
            "handler argv file must not be a symlink"
        )
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise CollaborativeDevelopmentCliError(
            "handler argv file is not readable"
        ) from error
    if len(raw) > _MAX_ARGV_JSON_BYTES:
        raise CollaborativeDevelopmentCliError(
            "handler argv file exceeds 64 KiB"
        )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CollaborativeDevelopmentCliError(
            "handler argv file is not valid JSON"
        ) from error
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise CollaborativeDevelopmentCliError(
            "handler argv file must contain a non-empty JSON string array"
        )
    return tuple(value)


def _emit(value: Any, *, stream: Any = None) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=stream or sys.stdout,
    )


class _Client:
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
            raise CollaborativeDevelopmentCliError(
                "base URL must be a plain http(s) origin"
            )
        host = parsed.host
        assert host is not None
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.casefold() == "localhost"
        if parsed.scheme == "http" and not loopback:
            raise CollaborativeDevelopmentCliError(
                "plaintext collaborative development HTTP is allowed only "
                "for a loopback origin"
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
            raise CollaborativeDevelopmentCliError(
                "collaborative development API is unavailable"
            ) from error
        try:
            payload = response.json()
        except ValueError:
            payload = {"status": response.status_code}
        if response.is_error:
            detail = payload.get("detail", {}) if isinstance(payload, dict) else {}
            code = detail.get("code", "request_failed") if isinstance(detail, dict) else "request_failed"
            message = detail.get("message", "request failed") if isinstance(detail, dict) else "request failed"
            raise CollaborativeDevelopmentCliError(
                f"{code}: {message} (HTTP {response.status_code})"
            )
        return payload


def _revision_body(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "idempotency_key": args.idempotency_key,
        "expected_revision": args.expected_revision,
    }


def _dispatch(args: argparse.Namespace, client: _Client) -> Any:
    command = args.command
    if command == "create":
        return client.request(
            "POST",
            "/api/v1/collaborative-development/assignments",
            {
                "idempotency_key": args.idempotency_key,
                "assignment": _read_json(args.assignment_file),
            },
        )
    if command in {
        "show",
        "status",
        "work-list",
        "events",
        "authority",
        "review-reconciliations",
    }:
        if command == "events":
            suffix = "/events?" + urlencode(
                {"after": args.after, "limit": args.limit}
            )
        else:
            suffix = {
                "show": "",
                "status": "/status",
                "work-list": "/work-items",
                "authority": "/workspace-authority",
                "review-reconciliations": "/review-reconciliations",
            }[command]
        return client.request(
            "GET",
            f"/api/v1/collaborative-development/assignments/{args.assignment_id}{suffix}",
        )
    if command == "review-requeue":
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/assignments/"
            f"{args.assignment_id}/review-reconciliations/"
            f"{args.outbox_id}/requeue",
            {
                "idempotency_key": args.idempotency_key,
                "expected_work_item_revision": args.expected_work_item_revision,
                "expected_failed_attempt": args.expected_failed_attempt,
                "confirmation": "requeue_unknown_review_attempt",
                "reason": args.reason,
            },
        )
    if command == "mode":
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/assignments/"
            f"{args.assignment_id}/execution-mode",
            {**_revision_body(args), "mode": args.mode},
        )
    if command == "approval":
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/assignments/"
            f"{args.assignment_id}/approval-mode",
            {**_revision_body(args), "mode": args.mode},
        )
    if command in {"stop", "archive"}:
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/assignments/"
            f"{args.assignment_id}/{command}",
            _revision_body(args),
        )
    if command == "work-create":
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/assignments/"
            f"{args.assignment_id}/work-items",
            {
                "idempotency_key": args.idempotency_key,
                "work_item": _read_json(args.work_item_file),
            },
        )
    if command in {"dispatch", "close"}:
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/work-items/"
            f"{args.work_item_id}/{command}",
            _revision_body(args),
        )
    if command == "lease":
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/work-items/{args.work_item_id}/lease",
            {**_revision_body(args), "ttl_seconds": args.ttl_seconds},
        )
    if command == "start":
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/leases/{args.lease_id}/start",
            {
                "idempotency_key": args.idempotency_key,
                "expected_work_item_revision": args.expected_work_item_revision,
            },
        )
    if command in {"result", "review"}:
        source = args.result_file if command == "result" else args.review_file
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/work-items/"
            f"{args.work_item_id}/{command}s",
            {
                "idempotency_key": args.idempotency_key,
                "expected_work_item_revision": args.expected_work_item_revision,
                command: _read_json(source),
            },
        )
    if command == "result-show":
        return client.request(
            "GET",
            f"/api/v1/collaborative-development/results/{args.result_id}",
        )
    if command == "review-prepare":
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/results/"
            f"{args.result_id}/review-snapshot",
            {"idempotency_key": args.idempotency_key},
        )
    if command == "ack":
        return client.request(
            "POST",
            f"/api/v1/collaborative-development/assignments/{args.assignment_id}/acks",
            {
                "idempotency_key": args.idempotency_key,
                "ack_seq": args.ack_seq,
                "expected_cursor_revision": args.expected_cursor_revision,
            },
        )
    raise AssertionError(f"unsupported command: {command}")


def _serve(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise CollaborativeDevelopmentCliError(
            "standalone collaboration server is restricted to loopback"
        )
    owner_token = _read_private_text(args.owner_token_file, label="owner token")
    signing_key = _read_private_text(args.signing_key_file, label="signing key")
    service = CollaborativeDevelopmentService(
        store=CollaborativeDevelopmentStore(
            args.data_dir.expanduser().resolve() / "collaborative-development.db"
        ),
        enabled=True,
        autonomous_enabled=_environment_flag(
            "LILIES_AUTONOMOUS_COLLABORATION_ENABLED"
        ),
    )
    app = create_standalone_collaborative_development_app(
        service=service,
        credential_issuer=DevelopmentCredentialIssuer(signing_key),
        owner_token=owner_token,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


async def _run_worker(args: argparse.Namespace) -> None:
    data_dir = args.data_dir.expanduser().resolve()
    database_path = data_dir / "collaborative-development.db"
    journal_path = (
        args.journal_file.expanduser().resolve()
        if args.journal_file is not None
        else data_dir / "collaborative-development-dispatch.db"
    )
    handlers = {}
    for role, argv_file in (
        (AgentRole.lilies, args.lilies_handler_argv_file),
        (AgentRole.codex, args.codex_handler_argv_file),
    ):
        if argv_file is not None:
            handlers[role] = ExternalJsonArgvDispatchHandler(
                _read_argv_json(argv_file),
                timeout_seconds=args.adapter_timeout_seconds,
                retry_after_seconds=args.adapter_retry_seconds,
            )
    dispatcher_id = args.dispatcher_id or f"worker-{uuid4().hex}"
    service = CollaborativeDevelopmentService(
        store=CollaborativeDevelopmentStore(database_path),
        enabled=True,
        autonomous_enabled=_environment_flag(
            "LILIES_AUTONOMOUS_COLLABORATION_ENABLED"
        ),
    )
    await service.initialize()
    lifecycle_bridge = AutonomousDevelopmentLifecycleBridge(
        service=service,
        workspace_broker=DevelopmentWorkspaceBroker(
            (
                args.workspace_state_dir
                if args.workspace_state_dir is not None
                else data_dir / "development-workspaces"
            ).expanduser().resolve()
        ),
        lease_ttl_seconds=args.lease_ttl_seconds,
    )

    stop_event = asyncio.Event()
    installed_signals: list[signal.Signals] = []
    if args.continuous:
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_number, stop_event.set)
            except (NotImplementedError, RuntimeError):
                continue
            installed_signals.append(signal_number)
        _emit(
            {
                "event": "worker.started",
                "dispatcher_id": dispatcher_id,
                "journal_path": str(journal_path),
                "mode": "continuous",
                "schema_version": "1.0",
            }
        )

    def emit_batch(batch: WorkerBatch) -> None:
        _emit(
            {
                "event": "worker.batch",
                **batch.model_dump(mode="json"),
            }
        )

    try:
        batch = await run_dispatch_worker(
            database_path=database_path,
            journal_path=journal_path,
            handlers=handlers,
            once=args.once,
            poll_interval_seconds=args.poll_interval_seconds,
            limit=args.limit,
            claim_ttl_seconds=args.claim_ttl_seconds,
            dispatcher_id=dispatcher_id,
            stop_event=stop_event,
            on_batch=emit_batch,
            lifecycle_bridge=lifecycle_bridge,
        )
    finally:
        if args.continuous:
            loop = asyncio.get_running_loop()
            for signal_number in installed_signals:
                loop.remove_signal_handler(signal_number)
    if args.continuous:
        _emit(
            {
                "event": "worker.stopped",
                "dispatcher_id": batch.dispatcher_id,
                "journal_path": batch.journal_path,
                "schema_version": "1.0",
            }
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "serve":
            return _serve(args)
        if args.command == "worker":
            asyncio.run(_run_worker(args))
            return 0
        client = _Client(base_url=args.base_url, access_token=_access_token(args))
        _emit(_dispatch(args, client))
        return 0
    except (CollaborativeDevelopmentCliError, ValueError) as error:
        _emit(
            {
                "error": {
                    "code": "collaborative_development_cli_failed",
                    "message": str(error),
                }
            },
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
