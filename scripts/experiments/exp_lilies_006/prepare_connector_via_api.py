#!/usr/bin/env python3
"""Generate the ERPNext planning connector from the public task schema."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def request(
    method: str,
    url: str,
    *,
    token: str,
    body: Any | None = None,
    missing_ok: bool = False,
) -> Any:
    data = (
        json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        if body is not None
        else None
    )
    value = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(value, timeout=120) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url}: HTTP {error.code}: {detail}") from error
    return json.loads(payload) if payload else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8018")
    parser.add_argument("--token", required=True)
    parser.add_argument("--application-id", required=True)
    parser.add_argument("--erpnext-base", default="http://127.0.0.1:18060")
    parser.add_argument("--schema-file", type=Path, required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    manifest = request(
        "GET",
        f"{base}/api/v1/connectors/manifests/erpnext-planning/1",
        token=args.token,
        missing_ok=True,
    )
    generation_id = None
    if manifest is None:
        document = json.loads(args.schema_file.read_text(encoding="utf-8"))
        document["servers"] = [{"url": args.erpnext_base.rstrip("/")}]
        generated = request(
            "POST",
            f"{base}/api/v1/connectors/generations",
            token=args.token,
            body={
                "connector_id": "erpnext-planning",
                "version": 1,
                "domain": "inventory-planning",
                "deployment": {
                    "profile_id": "exp006-local",
                    "environment": "test",
                    "base_url": args.erpnext_base.rstrip("/"),
                    "allowed_hosts": ["127.0.0.1"],
                    "available": True,
                    "timeout_seconds": 60,
                    "claim_ceiling": "H3",
                    "auth_scheme_id": "FrappeToken",
                    "auth_prefix": "token ",
                },
                "document": json.dumps(document, ensure_ascii=False, separators=(",", ":")),
                "include_operation_ids": [
                    "listBins",
                    "listMaterialRequests",
                    "createMaterialRequestDraft",
                    "getMaterialRequest",
                ],
            },
        )
        generation_id = str(generated["id"])
        contract = request(
            "POST",
            f"{base}/api/v1/connectors/generations/{generation_id}/contract-runs",
            token=args.token,
            body={
                "operation_ids": [
                    "listBins",
                    "listMaterialRequests",
                    "createMaterialRequestDraft",
                    "getMaterialRequest",
                ],
                "sample_inputs": {
                    "listBins": {
                        "fields": '["item_code","warehouse","actual_qty"]',
                        "limit_page_length": 10,
                    },
                    "listMaterialRequests": {
                        "fields": '["name","docstatus","title","status"]',
                        "limit_page_length": 10,
                    },
                    "createMaterialRequestDraft": {
                        "body": {
                            "material_request_type": "Purchase",
                            "company": "Lilies Planning",
                            "schedule_date": "2026-08-15",
                            "title": "EXP006 generated connector contract",
                            "docstatus": 0,
                            "items": [
                                {
                                    "item_code": "ITEM-A",
                                    "qty": 10,
                                    "warehouse": "Stores - L",
                                    "schedule_date": "2026-08-15",
                                }
                            ],
                        }
                    },
                    "getMaterialRequest": {"name": "MAT-MR-2026-00001"},
                },
                "owner_id": "exp006-tenant",
                "secret_ref": "secret://exp006-tenant/erpnext-api-token",
                "external_tenant_id": "erpnext-local-site",
                "allow_mutating_operations": True,
            },
        )
        if contract.get("status") != "passed":
            raise RuntimeError(f"generated ERPNext connector contract did not pass: {contract}")
        request(
            "POST",
            f"{base}/api/v1/connectors/generations/{generation_id}/register",
            token=args.token,
        )
    binding_matches = request(
        "GET",
        (
            f"{base}/api/v1/connectors/bindings?"
            + urllib.parse.urlencode(
                {
                    "connector_id": "erpnext-planning",
                    "tenant_id": "exp006-tenant",
                }
            )
        ),
        token=args.token,
    )
    existing_binding = binding_matches[0] if binding_matches else None
    application_ids = sorted(
        {
            *(existing_binding.get("application_ids", []) if existing_binding else []),
            args.application_id,
        }
    )
    request(
        "PUT",
        f"{base}/api/v1/connectors/bindings",
        token=args.token,
        body={
            "binding": {
                "connector_id": "erpnext-planning",
                "connector_version": 1,
                "tenant_id": "exp006-tenant",
                "external_tenant_id": "erpnext-local-site",
                "profile_id": "exp006-local",
                "secret_ref": "secret://exp006-tenant/erpnext-api-token",
                "application_ids": application_ids,
                "allowed_operations": [
                    "listBins",
                    "listMaterialRequests",
                    "createMaterialRequestDraft",
                    "getMaterialRequest",
                ],
                "subjects": [
                    {
                        "external_subject": "workflow-planner",
                        "actor_id": "workflow-planner",
                        "roles": ["planner"],
                    }
                ],
            },
            "expected_revision": (int(existing_binding["revision"]) if existing_binding else 0),
        },
    )
    policy_matches = request(
        "GET",
        (
            f"{base}/api/v1/connectors/policies?"
            + urllib.parse.urlencode(
                {
                    "connector_id": "erpnext-planning",
                    "tenant_id": "exp006-tenant",
                }
            )
        ),
        token=args.token,
    )
    existing_policy = policy_matches[0] if policy_matches else None
    request(
        "PUT",
        f"{base}/api/v1/connectors/policies",
        token=args.token,
        body={
            "policy": {
                "connector_id": "erpnext-planning",
                "connector_version": 1,
                "tenant_id": "exp006-tenant",
                "domain": "inventory-planning",
                "allowed_profiles": ["exp006-local"],
                "allowed_operations": [
                    "listBins",
                    "listMaterialRequests",
                    "createMaterialRequestDraft",
                    "getMaterialRequest",
                ],
                "required_roles": ["planner"],
                "max_payload_bytes": 1000000,
                "operation_request_constraints": [],
                "mutation_preauthorization_required": True,
                "allow_dry_run": True,
                "allow_compensation_during_stop": False,
            },
            "expected_revision": (int(existing_policy["revision"]) if existing_policy else 0),
        },
    )
    print(
        json.dumps(
            {
                "connector_id": "erpnext-planning",
                "version": 1,
                "generation_id": generation_id,
                "status": "ready",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
