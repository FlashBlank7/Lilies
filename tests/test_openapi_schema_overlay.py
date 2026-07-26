from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.openapi_connector import (
    MAX_OPENAPI_SCHEMA_OVERLAY_ACTIONS,
    OpenAPIMaterialError,
    OpenAPIMaterialLoader,
    OpenAPIConnectorGenerationRequest,
)


HEADERS = {
    "Authorization": "Bearer schema-overlay-test",
    "Content-Type": "application/json",
}


class ParcelSerializerHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/parcels/parcel-1":
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps({"id": "parcel-1"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


@contextmanager
def parcel_serializer_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ParcelSerializerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def settings(tmp_path: Path) -> Settings:
    value = Settings(
        api_token="schema-overlay-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
        platform_harness_network_egress_policy="full",
    )
    value.workspace_root.mkdir(parents=True, exist_ok=True)
    return value


def parcel_openapi_document() -> dict[str, Any]:
    """Neutral third-party contract whose published required list is too strict."""

    return {
        "openapi": "3.1.0",
        "info": {"title": "Parcel Tracking API", "version": "2026-07"},
        "paths": {
            "/parcels/{parcel_id}": {
                "get": {
                    "operationId": "retrieveParcel",
                    "parameters": [
                        {
                            "name": "parcel_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "example": "parcel-1"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "parcel",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Parcel"}
                                }
                            },
                        }
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Parcel": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "carrier": {"type": "string"},
                        "delivered_at": {"type": "string"},
                    },
                    "required": ["id", "carrier"],
                    "additionalProperties": False,
                }
            }
        },
    }


def generation_body(base_url: str, *, overlay: bool) -> dict[str, Any]:
    document = json.dumps(
        parcel_openapi_document(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    body: dict[str, Any] = {
        "connector_id": "parcel_tracking",
        "version": 1,
        "domain": "logistics",
        "document": document,
        "deployment": {
            "profile_id": "parcel-test",
            "environment": "test",
            "base_url": base_url,
            "allowed_hosts": ["127.0.0.1"],
            "available": True,
            "claim_ceiling": "H3",
        },
    }
    if overlay:
        body["schema_overlay"] = [
            {
                "op": "replace",
                "path": "/components/schemas/Parcel/required",
                "value": ["id"],
            },
            {
                "op": "add",
                "path": "/components/schemas/Parcel/properties/delivered_at/nullable",
                "value": True,
            },
        ]
    return body


def test_owner_api_applies_neutral_overlay_and_keeps_contract_gate(
    tmp_path: Path,
) -> None:
    with parcel_serializer_server() as base_url:
        official_body = generation_body(base_url, overlay=False)
        official_bytes = official_body["document"].encode()
        with TestClient(create_app(settings(tmp_path))) as client:
            baseline_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=official_body,
            )
            assert baseline_response.status_code == 201, baseline_response.text
            baseline = baseline_response.json()

            overlaid_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=generation_body(base_url, overlay=True),
            )
            assert overlaid_response.status_code == 201, overlaid_response.text
            overlaid = overlaid_response.json()

            provenance = overlaid["provenance"]
            overlay_provenance = provenance["schema_overlay"]
            assert provenance["source_digest"] == hashlib.sha256(official_bytes).hexdigest()
            assert overlay_provenance["action_count"] == 2
            assert overlay_provenance["overlay_digest"].startswith("sha256:")
            assert overlay_provenance["effective_contract_digest"].startswith("sha256:")
            assert (
                overlaid["manifest"]["source_provenance"]["schema_overlay"]
                == overlay_provenance
            )
            assert overlaid["request_fingerprint"] != baseline["request_fingerprint"]

            stale_baseline = client.get(
                f"/api/v1/connectors/generations/{baseline['id']}",
                headers=HEADERS,
            )
            assert stale_baseline.status_code == 200
            assert stale_baseline.json()["evidence_stale"] is True

            blocked_register = client.post(
                f"/api/v1/connectors/generations/{overlaid['id']}/register",
                headers=HEADERS,
            )
            assert blocked_register.status_code == 422
            assert "latest contract run must pass" in blocked_register.text

            contract_response = client.post(
                f"/api/v1/connectors/generations/{overlaid['id']}/contract-runs",
                headers=HEADERS,
                json={"operation_ids": ["retrieveParcel"]},
            )
            assert contract_response.status_code == 201, contract_response.text
            contract_run = contract_response.json()
            assert contract_run["status"] == "passed"
            assert contract_run["source_digest"] == provenance["source_digest"]
            assert contract_run["overlay_digest"] == overlay_provenance["overlay_digest"]
            assert (
                contract_run["effective_contract_digest"]
                == overlay_provenance["effective_contract_digest"]
            )

            registered = client.post(
                f"/api/v1/connectors/generations/{overlaid['id']}/register",
                headers=HEADERS,
            )
            assert registered.status_code == 201, registered.text
            assert registered.json()["connector_id"] == "parcel_tracking"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "message"),
    [
        (
            {
                "op": "add",
                "path": "/paths/~1new-resource",
                "value": {},
            },
            "must be inside",
        ),
        (
            {
                "op": "replace",
                "path": "/paths/~1parcels~1{parcel_id}/get/operationId",
                "value": "substituteOperation",
            },
            "must be inside",
        ),
        (
            {
                "op": "replace",
                "path": "/paths/~1parcels~1{parcel_id}/get/security",
                "value": [],
            },
            "must be inside",
        ),
        (
            {
                "op": "add",
                "path": "/components/securitySchemes/tokenAuth",
                "value": {"type": "http", "scheme": "bearer"},
            },
            "must be inside",
        ),
        (
            {
                "op": "add",
                "path": "/servers",
                "value": [{"url": "https://substitute.example"}],
            },
            "must be inside",
        ),
        (
            {
                "op": "replace",
                "path": "/components/schemas/Parcel/required~2invalid",
                "value": ["id"],
            },
            "invalid JSON Pointer escape",
        ),
        (
            {
                "op": "replace",
                "path": "/components/schemas/Parcel/required/10",
                "value": "id",
            },
            "out of range",
        ),
        (
            {
                "op": "replace",
                "path": "/components/schemas/Missing/required",
                "value": ["id"],
            },
            "parent path does not exist",
        ),
    ],
)
async def test_schema_overlay_fails_closed_for_unsafe_or_invalid_targets(
    action: dict[str, Any],
    message: str,
) -> None:
    request = OpenAPIConnectorGenerationRequest.model_validate(
        {
            **generation_body("https://parcel.example", overlay=False),
            "schema_overlay": [action],
        }
    )
    with pytest.raises(OpenAPIMaterialError, match=message) as captured:
        await OpenAPIMaterialLoader().load(request)
    assert captured.value.gap.capability == "schema_overlay"
    assert captured.value.gap.fatal is True


def test_schema_overlay_rejects_duplicate_oversized_and_excess_actions() -> None:
    base = generation_body("https://parcel.example", overlay=False)
    duplicate = {
        "op": "replace",
        "path": "/components/schemas/Parcel/required",
        "value": ["id"],
    }
    with pytest.raises(ValidationError, match="duplicate JSON Pointer paths"):
        OpenAPIConnectorGenerationRequest.model_validate(
            {**base, "schema_overlay": [duplicate, duplicate]}
        )

    with pytest.raises(ValidationError, match="exceeds 64000 bytes"):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **base,
                "schema_overlay": [
                    {
                        "op": "add",
                        "path": "/components/schemas/Parcel/description",
                        "value": "x" * 64_000,
                    }
                ],
            }
        )

    with pytest.raises(ValidationError):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **base,
                "schema_overlay": [
                    {
                        "op": "add",
                        "path": f"/components/schemas/Parcel/properties/field_{index}",
                        "value": {"type": "string"},
                    }
                    for index in range(MAX_OPENAPI_SCHEMA_OVERLAY_ACTIONS + 1)
                ],
            }
        )
