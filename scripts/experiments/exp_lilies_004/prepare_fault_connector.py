#!/usr/bin/env python3
"""Create a public-schema-derived fault-test Connector through public APIs."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from typing import Any


def request_json(
    method: str,
    url: str,
    *,
    token: str = "",
    body: Any | None = None,
) -> Any:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            payload = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"{method} {url} returned HTTP {error.code}: {detail}"
        ) from error
    return json.loads(payload) if payload else None


def platform(
    method: str,
    base_url: str,
    token: str,
    path: str,
    body: Any | None = None,
) -> Any:
    return request_json(
        method,
        f"{base_url.rstrip('/')}{path}",
        token=token,
        body=body,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-base", default="http://127.0.0.1:8014")
    parser.add_argument("--platform-token", required=True)
    parser.add_argument(
        "--thingsboard-base",
        default="http://127.0.0.1:19090",
    )
    parser.add_argument("--application-id", required=True)
    args = parser.parse_args()

    existing = platform(
        "GET",
        args.platform_base,
        args.platform_token,
        "/api/v1/connectors/manifests/thingsboard-rest/2",
    )
    generation_id = None
    if existing is None:
        generations = platform(
            "GET",
            args.platform_base,
            args.platform_token,
            "/api/v1/connectors/generations",
        )
        reusable = next(
            (
                item
                for item in reversed(generations)
                if item.get("connector_id") == "thingsboard-rest"
                and item.get("version") == 2
                and item.get("status") == "generated"
            ),
            None,
        )
        if reusable is not None:
            generation_id = str(reusable["id"])
        else:
            document = request_json(
                "GET",
                f"{args.thingsboard_base.rstrip('/')}/v3/api-docs",
            )
            source_manifest = platform(
                "GET",
                args.platform_base,
                args.platform_token,
                "/api/v1/connectors/manifests/thingsboard-rest/1",
            )
            save_alarm = next(
            item
            for item in source_manifest["operations"]
            if item["id"] == "saveAlarm"
            )
            generated = platform(
                "POST",
                args.platform_base,
                args.platform_token,
                "/api/v1/connectors/generations",
                {
                    "connector_id": "thingsboard-rest",
                    "version": 2,
                    "domain": "industrial-iot",
                    "deployment": {
                        "profile_id": "exp004-fault",
                        "environment": "test",
                        "base_url": "http://127.0.0.1:19091",
                        "allowed_hosts": ["127.0.0.1"],
                        "available": True,
                        "timeout_seconds": 30,
                        "claim_ceiling": "H3",
                        "auth_scheme_id": "ApiKeyForm",
                        "auth_prefix": "Bearer ",
                    },
                    "document": json.dumps(
                        document,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "include_operation_ids": [
                        "getLatestTimeseries",
                        "saveAlarm",
                    ],
                    "operation_contract_overlays": [
                        {
                            "operation_id": "saveAlarm",
                            "request_body_schema": (
                                save_alarm["request_schema"][
                                    "json_schema"
                                ]["properties"]["body"]
                            ),
                            "request_body_required": True,
                            "response_schemas": {
                                "200": save_alarm["response_json_schema"]
                            },
                        },
                    ],
                },
            )
            generation_id = str(generated["id"])
        platform(
            "POST",
            args.platform_base,
            args.platform_token,
            (
                "/api/v1/connectors/generations/"
                f"{generation_id}/register"
            ),
        )

    platform(
        "PUT",
        args.platform_base,
        args.platform_token,
        "/api/v1/connectors/bindings",
        {
            "binding": {
                "connector_id": "thingsboard-rest",
                "connector_version": 2,
                "tenant_id": "exp004-tenant",
                "external_tenant_id": "thingsboard-demo-tenant",
                "profile_id": "exp004-fault",
                "secret_ref": "secret://exp004-tenant/thingsboard-jwt",
                "application_ids": [args.application_id],
                "allowed_operations": [
                    "getLatestTimeseries",
                    "saveAlarm",
                ],
                "subjects": [
                    {
                        "external_subject": "workflow-operator",
                        "actor_id": "workflow-operator",
                        "roles": ["operator"],
                    }
                ],
            },
            "expected_revision": 0,
        },
    )
    platform(
        "PUT",
        args.platform_base,
        args.platform_token,
        "/api/v1/connectors/policies",
        {
            "policy": {
                "connector_id": "thingsboard-rest",
                "connector_version": 2,
                "tenant_id": "exp004-tenant",
                "domain": "industrial-iot",
                "allowed_profiles": ["exp004-fault"],
                "allowed_operations": [
                    "getLatestTimeseries",
                    "saveAlarm",
                ],
                "required_roles": ["operator"],
                "max_payload_bytes": 100000,
                "operation_request_constraints": [],
                "mutation_preauthorization_required": True,
                "allow_dry_run": True,
                "allow_compensation_during_stop": False,
            },
            "expected_revision": 0,
        },
    )
    print(
        json.dumps(
            {
                "connector_id": "thingsboard-rest",
                "connector_version": 2,
                "profile_id": "exp004-fault",
                "generation_id": generation_id,
                "status": "ready",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
