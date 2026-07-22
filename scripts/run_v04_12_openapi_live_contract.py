#!/usr/bin/env python3
"""Run selected generated OpenAPI contracts through the public platform API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = ROOT / "platform" / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from fastapi.testclient import TestClient  # noqa: E402

from agent_platform.api import create_app  # noqa: E402
from agent_platform.config import Settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a Connector and run selected live contracts."
    )
    parser.add_argument("--name", required=True, help="Evidence label, not a behavior switch")
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--connector-id", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--allowed-host", action="append", required=True)
    parser.add_argument("--auth-scheme-id", default="")
    parser.add_argument("--credentials-env", default="")
    parser.add_argument("--operation", action="append", required=True)
    parser.add_argument("--sample-inputs", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def checked(response: Any, action: str) -> dict[str, Any]:
    if response.status_code >= 400:
        raise RuntimeError(f"{action} failed with HTTP {response.status_code}: {response.text}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError(f"{action} returned a non-object response")
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    credential = os.environ.get(args.credentials_env, "") if args.credentials_env else ""
    if args.auth_scheme_id and not credential:
        raise RuntimeError("selected authentication requires a non-empty credentials environment variable")
    sample_inputs = {}
    if args.sample_inputs:
        sample_inputs = json.loads(args.sample_inputs.read_text(encoding="utf-8"))
        if not isinstance(sample_inputs, dict):
            raise RuntimeError("sample inputs must be a JSON object keyed by operation ID")

    with tempfile.TemporaryDirectory(prefix="lilies-openapi-contract-") as temporary:
        root = Path(temporary)
        settings = Settings(
            api_token="v04-12-live-contract",
            data_dir=root / "data",
            workspace_root=root / "workspaces",
            platform_harness_network_egress_policy="full",
            scheduler_poll_seconds=3600,
        )
        settings.workspace_root.mkdir(parents=True, exist_ok=True)
        headers = {
            "Authorization": "Bearer v04-12-live-contract",
            "Content-Type": "application/json",
        }
        with TestClient(create_app(settings)) as client:
            generation = checked(
                client.post(
                    "/api/v1/connectors/generations",
                    headers=headers,
                    json={
                        "connector_id": args.connector_id,
                        "version": 1,
                        "domain": args.domain,
                        "document": args.spec.read_text(encoding="utf-8"),
                        "deployment": {
                            "profile_id": "live-contract",
                            "environment": "test",
                            "base_url": args.base_url,
                            "allowed_hosts": args.allowed_host,
                            "available": True,
                            "claim_ceiling": "H3",
                            "auth_scheme_id": args.auth_scheme_id,
                        },
                    },
                ),
                "OpenAPI generation",
            )
            secret_ref = ""
            if credential:
                saved = checked(
                    client.post(
                        "/api/v1/platform/secrets",
                        headers=headers,
                        json={
                            "owner_id": "v04-12-contract",
                            "name": "host-credentials",
                            "value": credential,
                        },
                    ),
                    "credential storage",
                )
                secret_ref = str(saved["secret_ref"])
            contract_run = checked(
                client.post(
                    f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                    headers=headers,
                    json={
                        "operation_ids": args.operation,
                        "sample_inputs": sample_inputs,
                        "owner_id": "v04-12-contract",
                        "secret_ref": secret_ref,
                        "external_tenant_id": args.name,
                        "allow_mutating_operations": True,
                    },
                ),
                "live contract run",
            )
    operation_methods = {
        item["id"]: item["method"]
        for item in generation["manifest"]["operations"]
        if item["id"] in args.operation
    }
    return {
        "schema_version": "v0.4.12-openapi-live-contract-1",
        "experiment": args.name,
        "claim": "selected generated operations executed against a real host",
        "credential_source": args.credentials_env or "none",
        "credential_value_recorded": False,
        "generation": {
            "id": generation["id"],
            "source_digest": generation["provenance"]["source_digest"],
            "discovered_operations": generation["discovered_operation_count"],
            "generated_operations": generation["generated_operation_count"],
        },
        "selected_operations": operation_methods,
        "contract_run": contract_run,
        "reproduce": {
            "command_without_secret": " ".join(sys.argv),
            "required_secret_environment": args.credentials_env or None,
        },
    }


def main() -> int:
    args = parse_args()
    result = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "status": result["contract_run"]["status"],
                "selected_operations": result["selected_operations"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["contract_run"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
