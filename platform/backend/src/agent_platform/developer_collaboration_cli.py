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

from .collaboration_models import (
    DeveloperResponsePayload,
    DeveloperWorkerReceiptReference,
    ReportRoute,
)
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

    promote = subparsers.add_parser(
        "promote",
        help="promote the exact lease-bound developer workspace delta",
    )
    promote.add_argument("report_id", type=UUID)
    promote.add_argument("--lease-id", type=UUID, required=True)
    promote.add_argument("--expected-report-revision", type=int, required=True)
    promote.add_argument("--response-id", type=UUID, required=True)
    promote.add_argument("--idempotency-key", required=True)
    promote.add_argument("--workspace-manifest-digest", required=True)
    promote.add_argument("--source-manifest-digest", required=True)
    promote.add_argument("--worker-receipt-id", type=UUID)
    promote.add_argument("--worker-receipt-digest")

    worker = subparsers.add_parser(
        "worker",
        help="run the platform-owned sandboxed developer runtime",
    )
    worker.add_argument("report_id", type=UUID)
    worker.add_argument("--lease-id", type=UUID, required=True)
    worker.add_argument("--expected-report-revision", type=int, required=True)
    worker.add_argument("--response-id", type=UUID, required=True)
    worker.add_argument("--idempotency-key", required=True)
    worker.add_argument("--timeout-seconds", type=int, default=600)
    worker.add_argument(
        "--argument",
        dest="arguments",
        action="append",
        required=True,
        help="one fixed-runtime argument; repeat in process order",
    )

    respond = subparsers.add_parser(
        "respond",
        help="submit a substantive DeveloperResponse payload",
    )
    respond.add_argument("report_id", type=UUID)
    respond.add_argument("--lease-id", type=UUID, required=True)
    respond.add_argument("--expected-report-revision", type=int, required=True)
    respond.add_argument("--idempotency-key", required=True)
    respond.add_argument("--worker-receipt-id", type=UUID)
    respond.add_argument("--worker-receipt-digest")
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


def _worker_receipt_reference(
    args: argparse.Namespace,
) -> DeveloperWorkerReceiptReference | None:
    receipt_id = getattr(args, "worker_receipt_id", None)
    receipt_digest = getattr(args, "worker_receipt_digest", None)
    if receipt_id is None and receipt_digest is None:
        return None
    if receipt_id is None or receipt_digest is None:
        raise DeveloperCollaborationClientError(
            "worker receipt ID and digest must be supplied together"
        )
    return DeveloperWorkerReceiptReference(
        receipt_id=receipt_id,
        receipt_digest=receipt_digest,
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
    if args.command == "promote":
        parameters = {
            "lease_id": args.lease_id,
            "expected_report_revision": args.expected_report_revision,
            "response_id": args.response_id,
            "idempotency_key": args.idempotency_key,
            "workspace_manifest_digest": args.workspace_manifest_digest,
            "source_manifest_digest": args.source_manifest_digest,
        }
        receipt = _worker_receipt_reference(args)
        if receipt is not None:
            parameters["developer_worker_receipt"] = receipt
        return client.promote_source(args.report_id, **parameters)
    if args.command == "worker":
        return client.run_worker(
            args.report_id,
            lease_id=args.lease_id,
            expected_report_revision=args.expected_report_revision,
            response_id=args.response_id,
            idempotency_key=args.idempotency_key,
            arguments=args.arguments,
            timeout_seconds=args.timeout_seconds,
        )
    if args.command == "respond":
        parameters = {
            "lease_id": args.lease_id,
            "expected_report_revision": args.expected_report_revision,
            "idempotency_key": args.idempotency_key,
            "response": _read_response_payload(args.response_file),
        }
        receipt = _worker_receipt_reference(args)
        if receipt is not None:
            parameters["developer_worker_receipt"] = receipt
        return client.respond(args.report_id, **parameters)
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
