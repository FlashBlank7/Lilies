from __future__ import annotations

import copy
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
    MAX_OPENAPI_OPERATION_CONTRACT_OVERLAYS,
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


class LeaseLifecycleHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/leases":
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps({"id": "lease-1"}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_DELETE(self) -> None:  # noqa: N802
        if self.path != "/leases/lease-1":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()

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


@contextmanager
def lease_lifecycle_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), LeaseLifecycleHandler)
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


def operation_contract_document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Neutral Fulfilment API", "version": "2026-07"},
        "servers": [{"url": "https://fulfilment.example"}],
        "security": [{"tokenAuth": []}],
        "paths": {
            "/shipments": {
                "post": {
                    "operationId": "createShipment",
                    "security": [{"tokenAuth": []}],
                    "responses": {
                        "201": {"description": "created"},
                        "400": {"description": "invalid request"},
                    },
                }
            },
            "/shipments/{shipment_id}": {
                "servers": [{"url": "https://regional.fulfilment.example"}],
                "get": {
                    "operationId": "retrieveShipment",
                    "parameters": [
                        {
                            "name": "shipment_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "shipment",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "additionalProperties": True,
                                    }
                                }
                            },
                        },
                        "404": {"description": "not found"},
                    },
                },
            },
        },
        "components": {
            "securitySchemes": {
                "tokenAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Authorization",
                }
            }
        },
    }


def operation_contract_overlays() -> list[dict[str, Any]]:
    return [
        {
            "operation_id": "createShipment",
            "request_body_required": True,
            "request_body_schema": {
                "type": "object",
                "properties": {"reference": {"type": "string"}},
                "required": ["reference"],
                "additionalProperties": False,
            },
            "response_schemas": {
                "201": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                    "additionalProperties": False,
                }
            },
        },
        {
            "operation_id": "retrieveShipment",
            "response_schemas": {
                "200": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "state": {"type": "string"},
                    },
                    "required": ["id", "state"],
                    "additionalProperties": False,
                }
            },
        },
    ]


def operation_contract_generation_body(
    *,
    overlays: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "connector_id": "neutral_fulfilment",
        "version": 1,
        "domain": "logistics",
        "document": json.dumps(
            operation_contract_document(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "deployment": {
            "profile_id": "fulfilment-test",
            "environment": "test",
            "base_url": "https://fulfilment.example",
            "allowed_hosts": ["fulfilment.example"],
            "available": True,
            "claim_ceiling": "H3",
            "auth_scheme_id": "tokenAuth",
        },
        "operation_contract_overlays": overlays,
    }


def operation_semantics_document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Lease Lifecycle API", "version": "2026-07"},
        "paths": {
            "/leases": {
                "post": {
                    "operationId": "createLease",
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"id": {"type": "string"}},
                                        "required": ["id"],
                                        "additionalProperties": False,
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/leases/{lease_id}": {
                "get": {
                    "operationId": "retrieveLease",
                    "parameters": [
                        {
                            "name": "lease_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "example": "lease-1"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "lease",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"id": {"type": "string"}},
                                        "required": ["id"],
                                        "additionalProperties": False,
                                    }
                                }
                            },
                        }
                    },
                },
                "delete": {
                    "operationId": "deleteLease",
                    "parameters": [
                        {
                            "name": "lease_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "example": "lease-1"},
                        }
                    ],
                    "responses": {"204": {"description": "deleted"}},
                },
            },
        },
    }


def operation_semantics_generation_body(
    base_url: str,
    *,
    overlays: list[dict[str, Any]] | None,
    document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "connector_id": "lease_lifecycle",
        "version": 1,
        "domain": "infrastructure",
        "document": json.dumps(
            document or operation_semantics_document(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "deployment": {
            "profile_id": "lease-test",
            "environment": "test",
            "base_url": base_url,
            "allowed_hosts": ["127.0.0.1", "lease.example"],
            "available": True,
            "claim_ceiling": "H3",
        },
    }
    if overlays is not None:
        body["operation_semantics_overlays"] = overlays
    return body


def operation_semantics_overlays() -> list[dict[str, Any]]:
    return [
        {
            "operation_id": "createLease",
            "compensation_operation_id": "deleteLease",
        },
        {
            "operation_id": "deleteLease",
            "kind": "compensate",
        },
    ]


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


@pytest.mark.asyncio
async def test_operation_contract_overlay_only_changes_existing_json_schemas() -> None:
    original = operation_contract_document()
    request = OpenAPIConnectorGenerationRequest.model_validate(
        operation_contract_generation_body(overlays=operation_contract_overlays())
    )

    effective, provenance, _ = await OpenAPIMaterialLoader().load(request)

    assert provenance.operation_contract_overlay.operation_count == 2
    assert provenance.operation_contract_overlay.request_schema_count == 1
    assert provenance.operation_contract_overlay.request_body_required_count == 1
    assert provenance.operation_contract_overlay.response_schema_count == 2
    assert provenance.operation_contract_overlay.overlay_digest.startswith("sha256:")
    assert provenance.operation_contract_overlay.effective_contract_digest.startswith("sha256:")
    create = effective["paths"]["/shipments"]["post"]
    retrieve = effective["paths"]["/shipments/{shipment_id}"]["get"]
    assert create["requestBody"]["required"] is True
    assert create["requestBody"]["content"]["application/json"]["schema"]["required"] == [
        "reference"
    ]
    assert create["responses"]["201"]["content"]["application/json"]["schema"]["required"] == ["id"]
    assert retrieve["responses"]["200"]["content"]["application/json"]["schema"]["required"] == [
        "id",
        "state",
    ]
    assert set(create["responses"]) == set(original["paths"]["/shipments"]["post"]["responses"])
    assert set(retrieve["responses"]) == set(
        original["paths"]["/shipments/{shipment_id}"]["get"]["responses"]
    )
    assert effective["servers"] == original["servers"]
    assert effective["security"] == original["security"]
    assert effective["components"]["securitySchemes"] == original["components"]["securitySchemes"]
    assert create["security"] == original["paths"]["/shipments"]["post"]["security"]
    assert (
        effective["paths"]["/shipments/{shipment_id}"]["servers"]
        == original["paths"]["/shipments/{shipment_id}"]["servers"]
    )


def test_operation_contract_overlay_is_deterministic_and_marks_old_evidence_stale(
    tmp_path: Path,
) -> None:
    overlays = operation_contract_overlays()
    optional_overlays = copy.deepcopy(overlays)
    optional_overlays[0].pop("request_body_required")
    baseline_body = operation_contract_generation_body(overlays=optional_overlays)
    with TestClient(create_app(settings(tmp_path))) as client:
        baseline_response = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=baseline_body,
        )
        assert baseline_response.status_code == 201, baseline_response.text
        baseline = baseline_response.json()

        first_response = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=operation_contract_generation_body(overlays=overlays),
        )
        assert first_response.status_code == 201, first_response.text
        first = first_response.json()
        reversed_response = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=operation_contract_generation_body(overlays=list(reversed(overlays))),
        )
        assert reversed_response.status_code == 201, reversed_response.text
        reversed_generation = reversed_response.json()

        operation_provenance = first["provenance"]["operation_contract_overlay"]
        assert (
            first["manifest"]["source_provenance"]["operation_contract_overlay"]
            == operation_provenance
        )
        assert (
            operation_provenance == reversed_generation["provenance"]["operation_contract_overlay"]
        )
        assert first["request_fingerprint"] == reversed_generation["request_fingerprint"]
        assert first["id"] == reversed_generation["id"]
        assert first["request_fingerprint"] != baseline["request_fingerprint"]
        assert (
            first["provenance"]["operation_contract_overlay"]["overlay_digest"]
            != baseline["provenance"]["operation_contract_overlay"]["overlay_digest"]
        )
        required_create = next(
            operation
            for operation in first["manifest"]["operations"]
            if operation["id"] == "createShipment"
        )
        optional_create = next(
            operation
            for operation in baseline["manifest"]["operations"]
            if operation["id"] == "createShipment"
        )
        assert required_create["request_body"]["required"] is True
        assert "body" in required_create["request_schema"]["json_schema"]["required"]
        assert optional_create["request_body"]["required"] is False
        assert "body" not in optional_create["request_schema"]["json_schema"]["required"]

        stale_response = client.get(
            f"/api/v1/connectors/generations/{baseline['id']}",
            headers=HEADERS,
        )
        assert stale_response.status_code == 200
        assert stale_response.json()["evidence_stale"] is True


def test_operation_contract_overlay_provenance_binds_contract_and_registration(
    tmp_path: Path,
) -> None:
    with parcel_serializer_server() as base_url:
        body = generation_body(base_url, overlay=False)
        body["operation_contract_overlays"] = [
            {
                "operation_id": "retrieveParcel",
                "response_schemas": {
                    "200": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                        "required": ["id"],
                        "additionalProperties": False,
                    }
                },
            }
        ]
        with TestClient(create_app(settings(tmp_path))) as client:
            generation_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=body,
            )
            assert generation_response.status_code == 201, generation_response.text
            generation = generation_response.json()
            operation_provenance = generation["provenance"][
                "operation_contract_overlay"
            ]

            contract_response = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                headers=HEADERS,
                json={"operation_ids": ["retrieveParcel"]},
            )
            assert contract_response.status_code == 201, contract_response.text
            contract_run = contract_response.json()
            assert contract_run["status"] == "passed"
            assert contract_run["operation_contract_overlay"] == operation_provenance

            register_response = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/register",
                headers=HEADERS,
            )
            assert register_response.status_code == 201, register_response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overlay", "message"),
    [
        (
            {
                "operation_id": "missingOperation",
                "request_body_required": True,
                "request_body_schema": {"type": "object"},
            },
            "must reference an operationId",
        ),
        (
            {
                "operation_id": "createShipment",
                "response_schemas": {"202": {"type": "object"}},
            },
            "status must already exist exactly once",
        ),
    ],
)
async def test_operation_contract_overlay_fails_closed_for_unknown_targets(
    overlay: dict[str, Any],
    message: str,
) -> None:
    request = OpenAPIConnectorGenerationRequest.model_validate(
        operation_contract_generation_body(overlays=[overlay])
    )
    with pytest.raises(OpenAPIMaterialError, match=message) as captured:
        await OpenAPIMaterialLoader().load(request)
    assert captured.value.gap.capability == "operation_contract_overlay"
    assert captured.value.gap.fatal is True


@pytest.mark.parametrize(
    "unsafe_field",
    [
        {"path": "/substitute"},
        {"method": "put"},
        {"replacement_operation_id": "substituteOperation"},
        {"security": []},
        {"servers": [{"url": "https://substitute.example"}]},
        {"responses": {"202": {"type": "object"}}},
    ],
)
def test_operation_contract_overlay_rejects_api_surface_changes(
    unsafe_field: dict[str, Any],
) -> None:
    overlay = {
        "operation_id": "createShipment",
        "request_body_schema": {"type": "object"},
        **unsafe_field,
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OpenAPIConnectorGenerationRequest.model_validate(
            operation_contract_generation_body(overlays=[overlay])
        )


def test_operation_contract_overlay_rejects_non_success_duplicate_and_bounded_input() -> None:
    body = operation_contract_generation_body(overlays=[])
    with pytest.raises(ValidationError, match="explicit 2xx statuses"):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **body,
                "operation_contract_overlays": [
                    {
                        "operation_id": "createShipment",
                        "response_schemas": {"400": {"type": "object"}},
                    }
                ],
            }
        )

    duplicate = {
        "operation_id": "createShipment",
        "request_body_schema": {"type": "object"},
    }
    with pytest.raises(ValidationError, match="duplicate operationId"):
        OpenAPIConnectorGenerationRequest.model_validate(
            {**body, "operation_contract_overlays": [duplicate, duplicate]}
        )

    with pytest.raises(ValidationError, match="exceeds 64000 bytes"):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **body,
                "operation_contract_overlays": [
                    {
                        "operation_id": "createShipment",
                        "request_body_required": True,
                        "request_body_schema": {
                            "type": "object",
                            "description": "x" * 64_000,
                        },
                    }
                ],
            }
        )

    with pytest.raises(ValidationError):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **body,
                "operation_contract_overlays": [
                    {
                        "operation_id": f"operation{index}",
                        "request_body_schema": {"type": "object"},
                    }
                    for index in range(MAX_OPENAPI_OPERATION_CONTRACT_OVERLAYS + 1)
                ],
            }
        )


@pytest.mark.parametrize("invalid_required", [False, None, 1, "true"])
def test_operation_contract_overlay_request_required_is_true_only(
    invalid_required: Any,
) -> None:
    body = operation_contract_generation_body(overlays=[])
    with pytest.raises(ValidationError, match="request_body_required"):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **body,
                "operation_contract_overlays": [
                    {
                        "operation_id": "createShipment",
                        "request_body_schema": {"type": "object"},
                        "request_body_required": invalid_required,
                    }
                ],
            }
        )

    with pytest.raises(
        ValidationError,
        match="request_body_required=true requires request_body_schema",
    ):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **body,
                "operation_contract_overlays": [
                    {
                        "operation_id": "createShipment",
                        "request_body_required": True,
                    }
                ],
            }
        )


def test_operation_semantics_overlay_is_deterministic_and_binds_registration(
    tmp_path: Path,
) -> None:
    overlays = operation_semantics_overlays()
    with lease_lifecycle_server() as base_url:
        baseline_body = operation_semantics_generation_body(
            base_url,
            overlays=None,
        )
        explicit_empty_body = operation_semantics_generation_body(
            base_url,
            overlays=[],
        )
        selected_baseline_body = operation_semantics_generation_body(
            base_url,
            overlays=None,
        )
        selected_baseline_body["include_operation_ids"] = [
            "createLease",
            "deleteLease",
        ]
        semantics_body = operation_semantics_generation_body(
            base_url,
            overlays=overlays,
        )
        semantics_body["include_operation_ids"] = ["createLease", "deleteLease"]
        reversed_body = operation_semantics_generation_body(
            base_url,
            overlays=list(reversed(overlays)),
        )
        reversed_body["include_operation_ids"] = ["createLease", "deleteLease"]

        with TestClient(create_app(settings(tmp_path))) as client:
            baseline_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=baseline_body,
            )
            assert baseline_response.status_code == 201, baseline_response.text
            baseline = baseline_response.json()
            explicit_empty_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=explicit_empty_body,
            )
            assert explicit_empty_response.status_code == 201
            explicit_empty = explicit_empty_response.json()
            assert explicit_empty["id"] == baseline["id"]
            assert explicit_empty["request_fingerprint"] == baseline["request_fingerprint"]
            assert all(
                operation["kind"] == ("read" if operation["method"] == "GET" else "write")
                and operation["compensation_operation_id"] is None
                for operation in baseline["manifest"]["operations"]
            )

            selected_baseline_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=selected_baseline_body,
            )
            assert selected_baseline_response.status_code == 201
            selected_baseline = selected_baseline_response.json()
            generated_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=semantics_body,
            )
            assert generated_response.status_code == 201, generated_response.text
            generated = generated_response.json()
            reversed_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=reversed_body,
            )
            assert reversed_response.status_code == 201, reversed_response.text
            reversed_generation = reversed_response.json()
            assert reversed_generation["id"] == generated["id"]
            assert (
                reversed_generation["request_fingerprint"]
                == generated["request_fingerprint"]
            )

            provenance = generated["provenance"]["operation_semantics_overlay"]
            assert provenance["operation_count"] == 2
            assert provenance["compensate_count"] == 1
            assert provenance["compensation_binding_count"] == 1
            assert provenance["overlay_digest"].startswith("sha256:")
            assert provenance["effective_manifest_digest"].startswith("sha256:")
            assert (
                generated["manifest"]["source_provenance"][
                    "operation_semantics_overlay"
                ]
                == provenance
            )
            assert generated["request_fingerprint"] != baseline["request_fingerprint"]

            operations = {
                operation["id"]: operation
                for operation in generated["manifest"]["operations"]
            }
            assert operations["createLease"]["kind"] == "write"
            assert (
                operations["createLease"]["compensation_operation_id"]
                == "deleteLease"
            )
            assert operations["deleteLease"]["kind"] == "compensate"
            assert operations["deleteLease"]["method"] == "DELETE"
            assert operations["createLease"]["idempotency_semantics"] == "none"
            assert operations["deleteLease"]["idempotency_semantics"] == "none"
            selected_baseline_operations = {
                operation["id"]: operation
                for operation in selected_baseline["manifest"]["operations"]
            }
            for operation_id, operation in operations.items():
                expected = dict(selected_baseline_operations[operation_id])
                actual = dict(operation)
                expected.pop("kind")
                expected.pop("compensation_operation_id")
                actual.pop("kind")
                actual.pop("compensation_operation_id")
                assert actual == expected

            original = operation_semantics_document()
            assert operations["createLease"]["path"] == "/leases"
            assert operations["createLease"]["success_status_codes"] == [201]
            assert operations["deleteLease"]["path"] == "/leases/{lease_id}"
            assert operations["deleteLease"]["success_status_codes"] == [204]
            assert set(original["paths"]) == {
                operation["path"] for operation in baseline["manifest"]["operations"]
            }

            stale_response = client.get(
                f"/api/v1/connectors/generations/{baseline['id']}",
                headers=HEADERS,
            )
            assert stale_response.status_code == 200
            assert stale_response.json()["evidence_stale"] is True

            contract_response = client.post(
                f"/api/v1/connectors/generations/{generated['id']}/contract-runs",
                headers=HEADERS,
                json={"allow_mutating_operations": True},
            )
            assert contract_response.status_code == 201, contract_response.text
            contract_run = contract_response.json()
            assert contract_run["status"] == "passed"
            assert contract_run["operation_semantics_overlay"] == provenance

            register_response = client.post(
                f"/api/v1/connectors/generations/{generated['id']}/register",
                headers=HEADERS,
            )
            assert register_response.status_code == 201, register_response.text
            assert (
                register_response.json()["source_provenance"][
                    "operation_semantics_overlay"
                ]
                == provenance
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overlays", "message"),
    [
        (
            [{"operation_id": "missingOperation", "kind": "compensate"}],
            "exists in the live OpenAPI document",
        ),
        (
            [{"operation_id": "retrieveLease", "kind": "compensate"}],
            "only be declared for an existing DELETE",
        ),
        (
            [
                {
                    "operation_id": "retrieveLease",
                    "compensation_operation_id": "deleteLease",
                },
                {"operation_id": "deleteLease", "kind": "compensate"},
            ],
            "only be bound to a write operation",
        ),
        (
            [
                {
                    "operation_id": "deleteLease",
                    "compensation_operation_id": "deleteLease",
                }
            ],
            "cannot compensate itself",
        ),
        (
            [
                {
                    "operation_id": "deleteLease",
                    "compensation_operation_id": "createLease",
                }
            ],
            "must reference an existing DELETE",
        ),
        (
            [
                {
                    "operation_id": "createLease",
                    "compensation_operation_id": "deleteLease",
                }
            ],
            "declared kind=compensate",
        ),
    ],
)
async def test_operation_semantics_overlay_rejects_unknown_or_wrong_methods(
    overlays: list[dict[str, Any]],
    message: str,
) -> None:
    request = OpenAPIConnectorGenerationRequest.model_validate(
        operation_semantics_generation_body(
            "https://lease.example",
            overlays=overlays,
        )
    )
    with pytest.raises(OpenAPIMaterialError, match=message) as captured:
        await OpenAPIMaterialLoader().load(request)
    assert captured.value.gap.capability == "operation_semantics_overlay"
    assert captured.value.gap.fatal is True


@pytest.mark.asyncio
async def test_operation_semantics_overlay_rejects_ambiguous_and_multi_primary_targets(
) -> None:
    ambiguous_document = operation_semantics_document()
    ambiguous_document["paths"]["/old-leases/{lease_id}"] = {
        "delete": {
            "operationId": "deleteLease",
            "responses": {"204": {"description": "deleted"}},
        }
    }
    ambiguous_request = OpenAPIConnectorGenerationRequest.model_validate(
        operation_semantics_generation_body(
            "https://lease.example",
            overlays=operation_semantics_overlays(),
            document=ambiguous_document,
        )
    )
    with pytest.raises(OpenAPIMaterialError, match="ambiguous"):
        await OpenAPIMaterialLoader().load(ambiguous_request)

    multi_primary_document = operation_semantics_document()
    multi_primary_document["paths"]["/renewals"] = {
        "post": {
            "operationId": "renewLease",
            "responses": {"204": {"description": "renewed"}},
        }
    }
    multi_primary_overlays = [
        *operation_semantics_overlays(),
        {
            "operation_id": "renewLease",
            "compensation_operation_id": "deleteLease",
        },
    ]
    multi_primary_request = OpenAPIConnectorGenerationRequest.model_validate(
        operation_semantics_generation_body(
            "https://lease.example",
            overlays=multi_primary_overlays,
            document=multi_primary_document,
        )
    )
    with pytest.raises(OpenAPIMaterialError, match="multiple primary write"):
        await OpenAPIMaterialLoader().load(multi_primary_request)


def test_operation_semantics_overlay_rejects_invalid_shape_and_surface_fields() -> None:
    body = operation_semantics_generation_body(
        "https://lease.example",
        overlays=[],
    )
    duplicate = {"operation_id": "deleteLease", "kind": "compensate"}
    with pytest.raises(ValidationError, match="duplicate operationId"):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **body,
                "operation_semantics_overlays": [duplicate, duplicate],
            }
        )

    for overlay in (
        {"operation_id": "deleteLease"},
        {
            "operation_id": "deleteLease",
            "kind": "compensate",
            "compensation_operation_id": "createLease",
        },
    ):
        with pytest.raises(ValidationError, match="exactly one"):
            OpenAPIConnectorGenerationRequest.model_validate(
                {**body, "operation_semantics_overlays": [overlay]}
            )

    with pytest.raises(ValidationError):
        OpenAPIConnectorGenerationRequest.model_validate(
            {
                **body,
                "operation_semantics_overlays": [
                    {"operation_id": "deleteLease", "kind": "write"}
                ],
            }
        )

    for unsafe_field in (
        {"method": "DELETE"},
        {"path": "/replacement"},
        {"security": []},
        {"success_status_codes": [200]},
        {"idempotency_semantics": "request_key"},
    ):
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            OpenAPIConnectorGenerationRequest.model_validate(
                {
                    **body,
                    "operation_semantics_overlays": [
                        {
                            "operation_id": "deleteLease",
                            "kind": "compensate",
                            **unsafe_field,
                        }
                    ],
                }
            )


def test_operation_semantics_overlay_requires_both_operations_in_generated_manifest(
    tmp_path: Path,
) -> None:
    body = operation_semantics_generation_body(
        "https://lease.example",
        overlays=operation_semantics_overlays(),
    )
    body["include_operation_ids"] = ["createLease"]
    with TestClient(create_app(settings(tmp_path))) as client:
        response = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=body,
        )
    assert response.status_code == 422
    assert "same generated manifest" in response.text
