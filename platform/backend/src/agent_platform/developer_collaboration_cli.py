from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from .collaboration_models import DeveloperResponsePayload, ReportRoute
from .developer_collaboration_client import (
    DeveloperCollaborationClient,
    DeveloperCollaborationClientError,
    read_developer_token,
)


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lilies-developer",
        description="Process only user-approved Lilies collaboration work.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="platform base URL (default: LILIES_PLATFORM_BASE_URL)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        help="private developer bearer file; otherwise use the environment",
    )
    parser.add_argument("--version", action="version", version="Lilies Developer 0.4.13")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inbox = subparsers.add_parser("inbox", help="list only developer-visible work")
    inbox.add_argument("--after", type=int, default=0)
    inbox.add_argument("--limit", type=int, default=100)
    inbox.add_argument("--route", choices=[item.value for item in ReportRoute])

    lease = subparsers.add_parser("lease", help="acquire a report lease")
    lease.add_argument("report_id", type=UUID)
    lease.add_argument("--expected-report-revision", type=int, required=True)
    lease.add_argument("--idempotency-key", required=True)
    lease.add_argument("--ttl-seconds", type=int, default=900)

    renew = subparsers.add_parser("renew", help="renew an owned report lease")
    renew.add_argument("report_id", type=UUID)
    renew.add_argument("--expected-lease-revision", type=int, required=True)
    renew.add_argument("--idempotency-key", required=True)
    renew.add_argument("--ttl-seconds", type=int, default=900)

    release = subparsers.add_parser("release", help="release an owned report lease")
    release.add_argument("report_id", type=UUID)
    release.add_argument("--expected-lease-revision", type=int, required=True)
    release.add_argument("--idempotency-key", required=True)
    release.add_argument("--reason", required=True)

    respond = subparsers.add_parser(
        "respond",
        help="submit a substantive DeveloperResponse payload",
    )
    respond.add_argument("report_id", type=UUID)
    respond.add_argument("--lease-id", type=UUID, required=True)
    respond.add_argument("--expected-report-revision", type=int, required=True)
    respond.add_argument("--idempotency-key", required=True)
    respond.add_argument(
        "--response-file",
        required=True,
        help="JSON DeveloperResponsePayload path, or - for stdin",
    )
    return parser


def _read_response_payload(source: str) -> DeveloperResponsePayload:
    try:
        if source == "-":
            raw = sys.stdin.buffer.read(_MAX_RESPONSE_BYTES + 1)
        else:
            raw = Path(source).read_bytes()
    except OSError as error:
        raise DeveloperCollaborationClientError(
            "developer response file is not readable"
        ) from error
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise DeveloperCollaborationClientError(
            "developer response exceeds the 2 MiB CLI limit"
        )
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeveloperCollaborationClientError(
            "developer response file is not valid JSON"
        ) from error
    try:
        return DeveloperResponsePayload.model_validate(value)
    except ValidationError as error:
        issues = [
            {
                "loc": [str(item) for item in detail.get("loc", ())],
                "type": str(detail.get("type", "value_error")),
            }
            for detail in error.errors()
        ]
        raise DeveloperCollaborationClientError(
            f"developer response does not match the strict schema: "
            f"{json.dumps(issues, separators=(',', ':'), sort_keys=True)}"
        ) from error


def _emit(value: Any, *, stream: Any = None) -> None:
    destination = stream or sys.stdout
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        file=destination,
    )


def _dispatch(args: argparse.Namespace, client: DeveloperCollaborationClient) -> Any:
    if args.command == "inbox":
        return client.inbox(after=args.after, limit=args.limit, route=args.route)
    if args.command == "lease":
        return client.acquire_lease(
            args.report_id,
            expected_report_revision=args.expected_report_revision,
            idempotency_key=args.idempotency_key,
            ttl_seconds=args.ttl_seconds,
        )
    if args.command == "renew":
        return client.renew_lease(
            args.report_id,
            expected_lease_revision=args.expected_lease_revision,
            idempotency_key=args.idempotency_key,
            ttl_seconds=args.ttl_seconds,
        )
    if args.command == "release":
        return client.release_lease(
            args.report_id,
            expected_lease_revision=args.expected_lease_revision,
            idempotency_key=args.idempotency_key,
            reason=args.reason,
        )
    if args.command == "respond":
        return client.respond(
            args.report_id,
            lease_id=args.lease_id,
            expected_report_revision=args.expected_report_revision,
            idempotency_key=args.idempotency_key,
            response=_read_response_payload(args.response_file),
        )
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        base_url = args.base_url or os.environ.get("LILIES_PLATFORM_BASE_URL", "")
        token = read_developer_token(
            environment_value=os.environ.get(
                "LILIES_COLLABORATION_DEVELOPER_TOKEN",
                "",
            ),
            token_file=args.token_file,
        )
        client = DeveloperCollaborationClient(
            base_url=base_url,
            access_token=token,
        )
        _emit(_dispatch(args, client))
        return 0
    except (DeveloperCollaborationClientError, ValueError) as error:
        _emit(
            {
                "error": {
                    "code": "developer_collaboration_failed",
                    "message": str(error),
                    **(
                        {"status": error.status_code}
                        if isinstance(error, DeveloperCollaborationClientError)
                        and error.status_code is not None
                        else {}
                    ),
                }
            },
            stream=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
