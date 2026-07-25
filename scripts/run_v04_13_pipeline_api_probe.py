#!/usr/bin/env python3
"""Capture an independent live-HTTP surface result for T01G."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from agent_platform.collaboration_qualification import (  # noqa: E402
    QualificationSurfaceResult,
    canonical_digest,
)


UNKNOWN_ASSIGNMENT = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")


def _body_digest(response: httpx.Response) -> str:
    return f"sha256:{hashlib.sha256(response.content).hexdigest()}"


def capture(*, base_url: str, token: str) -> QualificationSurfaceResult:
    records: list[dict[str, Any]] = []
    with httpx.Client(base_url=base_url, timeout=15) as client:
        probes = (
            ("health", "GET", "/health", None, 200),
            (
                "ordinary-public-openapi-hides-private-collaboration",
                "GET",
                "/openapi.json",
                token,
                200,
            ),
            (
                "formal-undiscoverable-without-bearer",
                "GET",
                "/api/v1/studio/collaboration/channels",
                None,
                404,
            ),
            (
                "development-undiscoverable-without-bearer",
                "GET",
                f"/api/v1/collaborative-development/assignments/{UNKNOWN_ASSIGNMENT}",
                None,
                404,
            ),
            (
                "formal-user-surface",
                "GET",
                "/api/v1/studio/collaboration/channels",
                token,
                200,
            ),
            (
                "development-assignment-bound-not-found",
                "GET",
                f"/api/v1/collaborative-development/assignments/{UNKNOWN_ASSIGNMENT}",
                token,
                404,
            ),
            (
                "local-lilies-status",
                "GET",
                "/api/v1/local-lilies/status",
                token,
                200,
            ),
        )
        for probe_id, method, path, bearer, expected in probes:
            response = client.request(
                method,
                path,
                headers=(
                    {"Authorization": f"Bearer {bearer}"}
                    if bearer is not None
                    else {}
                ),
            )
            record: dict[str, Any] = {
                "probe_id": probe_id,
                "method": method,
                "path": path,
                "authentication": (
                    "bounded_test_bearer" if bearer is not None else "none"
                ),
                "expected_status": expected,
                "actual_status": response.status_code,
                "content_type": response.headers.get("content-type", ""),
                "body_digest": _body_digest(response),
            }
            if probe_id == (
                "ordinary-public-openapi-hides-private-collaboration"
            ):
                openapi_schema_parsed = False
                try:
                    openapi = response.json()
                    paths = (
                        openapi.get("paths", {})
                        if isinstance(openapi, dict)
                        else {}
                    )
                    if isinstance(paths, dict):
                        path_names = list(paths)
                        openapi_schema_parsed = True
                    else:
                        path_names = []
                except ValueError:
                    path_names = []
                collaborative_development_path_count = sum(
                    route.startswith(
                        "/api/v1/collaborative-development/"
                    )
                    for route in path_names
                )
                formal_private_path_count = sum(
                    route.startswith("/api/v1/studio/collaboration/")
                    for route in path_names
                )
                record.update(
                    {
                        "public_path_count": len(path_names),
                        "collaborative_development_path_count": (
                            collaborative_development_path_count
                        ),
                        "formal_private_path_count": (
                            formal_private_path_count
                        ),
                        "formal_private_route_absent": (
                            formal_private_path_count == 0
                        ),
                        "openapi_schema_parsed": openapi_schema_parsed,
                        "semantic_match": (
                            response.status_code == expected
                            and openapi_schema_parsed
                            and len(path_names) > 0
                            and collaborative_development_path_count == 0
                            and formal_private_path_count == 0
                        ),
                    }
                )
            records.append(record)
    passed = all(
        record["actual_status"] == record["expected_status"]
        and record.get("semantic_match", True) is True
        for record in records
    )
    return QualificationSurfaceResult(
        status="passed" if passed else "failed",
        source=f"live-http:{base_url.rstrip('/')}",
        summary=(
            f"{len(records)} retained live HTTP observations exercised health, "
            "ordinary OpenAPI non-disclosure, unauthenticated route hiding, "
            "the authenticated formal user list, standalone development "
            "routing, and Local Lilies status; "
            f"{sum(record['actual_status'] == record['expected_status'] for record in records)} "
            "matched their expected status."
        ),
        observations=records,
        digest=canonical_digest(records),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture the T01G live API result.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.token_file is not None:
        token = args.token_file.read_text(encoding="utf-8").strip()
    else:
        token = os.environ.get("LILIES_T01G_API_TOKEN", "").strip()
    if len(token) < 16:
        parser.error("provide a bounded test token through --token-file or the environment")
    try:
        result = capture(base_url=args.base_url, token=token)
    except httpx.HTTPError as error:
        print(f"live API probe failed: {type(error).__name__}", file=sys.stderr)
        return 2
    rendered = json.dumps(
        result.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        indent=2,
    ) + "\n"
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
                    "status": result.status,
                    "output": str(destination),
                    "digest": result.digest,
                },
                ensure_ascii=False,
            )
        )
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
