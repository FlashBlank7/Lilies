from __future__ import annotations

import base64
import hashlib
import json
import ipaddress
import shutil
import socket
import sqlite3
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import httpcore
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.connector_sdk import (
    ConnectorObjectSchema,
    ConnectorOperation,
    ConnectorService,
)
from agent_platform.openapi_connector import (
    OpenAPIMaterialError,
    OpenAPIMaterialLoader,
    OpenAPIConnectorGenerationRequest,
)


HEADERS = {"Authorization": "Bearer openapi-test", "Content-Type": "application/json"}


class GeneratedContractHandler(BaseHTTPRequestHandler):
    received_headers: list[str] = []
    received_authorization: list[str] = []
    received_api_keys: list[str] = []
    received_bodies: list[dict[str, Any]] = []
    received_paths: list[str] = []
    received_cookies: list[str] = []
    received_content_types: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).received_paths.append(self.path)
        type(self).received_cookies.append(self.headers.get("Cookie", ""))
        type(self).received_api_keys.append(self.headers.get("X-API-Key", ""))
        if self.path.startswith("/wrong-content/"):
            self._send(200, {"id": "wrong", "name": "Wrong"}, content_type="text/plain")
            return
        if self.path.startswith("/error/"):
            self._send(422, {"error": "documented error"})
            return
        if self.path == "/wrapped":
            self._send(
                200,
                {"data": {"id": "wrapped"}, "meta": {"count": 1}},
            )
            return
        if self.path == "/wrong-envelope":
            self._send(200, {"id": "unwrapped"})
            return
        if not self.path.startswith("/items/"):
            self._send(404, {"error": "not found"})
            return
        type(self).received_headers.append(self.headers.get("X-Trace", ""))
        type(self).received_authorization.append(self.headers.get("Authorization", ""))
        self._send(
            200,
            {
                "id": "example",
                "name": "Generated item",
                "meta": {"source": "contract"},
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/items":
            self._send(404, {"error": "not found"})
            return
        body = self._body()
        type(self).received_bodies.append(body)
        type(self).received_authorization.append(self.headers.get("Authorization", ""))
        type(self).received_content_types.append(self.headers.get("Content-Type", ""))
        self._send(
            201,
            {
                "id": "created",
                "name": body.get("name", ""),
                "meta": {"source": "contract"},
            },
        )

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length) if length else b"{}")
        return value if isinstance(value, dict) else {}

    def _send(
        self,
        status: int,
        body: Any,
        *,
        content_type: str = "application/json",
    ) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class OpenAPIMaterialHandler(BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class MultipartContractHandler(BaseHTTPRequestHandler):
    received_content_type = ""
    received_body = b""

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/file-submissions":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        type(self).received_content_type = self.headers.get("Content-Type", "")
        type(self).received_body = self.rfile.read(length)
        payload = json.dumps({"stored": True}).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


class IsolatedLiveMutationContractHandler(BaseHTTPRequestHandler):
    received_authorization: list[str] = []
    received_bodies: list[dict[str, Any]] = []
    response_secret = "isolated-live-response-secret"

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/items":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) if length else b"{}")
        type(self).received_bodies.append(body)
        type(self).received_authorization.append(
            self.headers.get("Authorization", "")
        )
        payload = json.dumps(
            {
                "id": "isolated-live-created",
                "name": body.get("name", ""),
                "meta": {"source": "contract"},
                "secret_token": type(self).response_secret,
            }
        ).encode()
        self.send_response(201)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


@contextmanager
def generated_contract_server() -> Iterator[str]:
    GeneratedContractHandler.received_headers = []
    GeneratedContractHandler.received_authorization = []
    GeneratedContractHandler.received_api_keys = []
    GeneratedContractHandler.received_bodies = []
    GeneratedContractHandler.received_paths = []
    GeneratedContractHandler.received_cookies = []
    GeneratedContractHandler.received_content_types = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), GeneratedContractHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def multipart_contract_server() -> Iterator[str]:
    MultipartContractHandler.received_content_type = ""
    MultipartContractHandler.received_body = b""
    server = ThreadingHTTPServer(("127.0.0.1", 0), MultipartContractHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def isolated_live_mutation_contract_server() -> Iterator[str]:
    IsolatedLiveMutationContractHandler.received_authorization = []
    IsolatedLiveMutationContractHandler.received_bodies = []
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        IsolatedLiveMutationContractHandler,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def openapi_material_server(payload: bytes) -> Iterator[str]:
    OpenAPIMaterialHandler.payload = payload
    server = ThreadingHTTPServer(("127.0.0.1", 0), OpenAPIMaterialHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/openapi.json"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def settings(tmp_path: Path) -> Settings:
    value = Settings(
        api_token="openapi-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
        platform_harness_network_egress_policy="full",
    )
    value.workspace_root.mkdir(parents=True, exist_ok=True)
    return value


def openapi_document(*, title: str = "Inventory API") -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "2026.1"},
        "security": [{"basicAuth": []}],
        "paths": {
            "/items/{item_id}": {
                "get": {
                    "operationId": "getItem",
                    "summary": "Read one item",
                    "parameters": [
                        {
                            "name": "item_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "example": "folder/item"},
                        },
                        {
                            "name": "verbose",
                            "in": "query",
                            "schema": {"type": "boolean", "default": True},
                        },
                        {
                            "name": "X-Trace",
                            "in": "header",
                            "schema": {"type": "string", "example": "contract"},
                        },
                        {
                            "name": "session",
                            "in": "cookie",
                            "schema": {"type": "string", "example": "cookie-contract"},
                        },
                    ],
                    "responses": {
                        "200": {
                            "description": "item",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            },
                        },
                        "404": {"description": "not found"},
                    },
                }
            },
            "/items": {
                "post": {
                    "operationId": "createItem",
                    "summary": "Create one item",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "example": "Created"},
                                        "server_id": {"type": "string", "readOnly": True},
                                    },
                                    "required": ["name", "server_id"],
                                    "additionalProperties": False,
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Item"}
                                }
                            },
                        }
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {"basicAuth": {"type": "http", "scheme": "basic"}},
            "schemas": {
                "Item": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "meta": {
                            "type": "object",
                            "properties": {"source": {"type": "string"}},
                            "required": ["source"],
                        },
                    },
                    "required": ["id", "name", "meta"],
                    "additionalProperties": False,
                }
            },
        },
    }


def generation_body(base_url: str, document: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "connector_id": "generated_inventory",
        "version": 1,
        "domain": "inventory",
        "document": json.dumps(document or openapi_document()),
        "deployment": {
            "profile_id": "generated-test",
            "environment": "test",
            "base_url": base_url,
            "allowed_hosts": ["127.0.0.1"],
            "available": True,
            "claim_ceiling": "H3",
        },
    }


@pytest.mark.asyncio
async def test_loader_accepts_yaml_and_resolves_local_references() -> None:
    request = OpenAPIConnectorGenerationRequest.model_validate(
        {
            **generation_body("https://inventory.example"),
            "document": """
openapi: 3.0.3
info: {title: YAML API, version: '1'}
paths:
  /items:
    get:
      operationId: listItems
      responses:
        '200':
          description: ok
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Envelope'}
components:
  schemas:
    Envelope:
      type: object
      properties: {count: {type: integer}}
      required: [count]
""",
        }
    )
    document, provenance, gaps = await OpenAPIMaterialLoader().load(request)
    schema = document["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert schema["properties"]["count"]["type"] == "integer"
    assert provenance.openapi_version == "3.0.3"
    assert provenance.source_digest
    assert gaps == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("document", "gap_code"),
    [
        ("not: [valid", "IF-01"),
        (json.dumps({"openapi": "2.0", "info": {}, "paths": {}}), "IF-01"),
        (
            json.dumps(
                {
                    "openapi": "3.1.0",
                    "info": {"title": "remote", "version": "1"},
                    "paths": {},
                    "components": {
                        "schemas": {"Remote": {"$ref": "https://example.com/schema.json"}}
                    },
                }
            ),
            "IF-02",
        ),
    ],
)
async def test_loader_returns_typed_gaps_for_invalid_or_unsafe_documents(
    document: str,
    gap_code: str,
) -> None:
    request = OpenAPIConnectorGenerationRequest.model_validate(
        {**generation_body("https://inventory.example"), "document": document}
    )
    with pytest.raises(OpenAPIMaterialError) as captured:
        await OpenAPIMaterialLoader().load(request)
    assert captured.value.gap.code == gap_code
    assert captured.value.gap.fatal is True


@pytest.mark.asyncio
async def test_loader_rejects_oversized_and_unallowlisted_urls_with_typed_gap() -> None:
    oversized = OpenAPIConnectorGenerationRequest.model_validate(
        {**generation_body("https://inventory.example"), "document": "x" * 5_000_001}
    )
    with pytest.raises(OpenAPIMaterialError) as size_error:
        await OpenAPIMaterialLoader().load(oversized)
    assert size_error.value.gap.capability == "document_size"

    unsafe = OpenAPIConnectorGenerationRequest.model_validate(
        {
            **generation_body("https://inventory.example"),
            "document": "",
            "document_url": "http://127.0.0.1/openapi.json",
            "allowed_document_hosts": ["127.0.0.1"],
        }
    )
    with pytest.raises(OpenAPIMaterialError) as url_error:
        await OpenAPIMaterialLoader().load(unsafe)
    assert url_error.value.gap.capability == "document_url"


@pytest.mark.asyncio
async def test_loader_streams_allowlisted_url_and_enforces_dns_and_response_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = OpenAPIMaterialLoader()
    connected_hosts: list[str] = []
    real_connect = httpcore.AnyIOBackend.connect_tcp

    async def route_pinned_test_address(
        backend: httpcore.AnyIOBackend,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        connected_hosts.append(host)
        return await real_connect(
            backend,
            "127.0.0.1",
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", route_pinned_test_address)

    async def public_address(host: str, port: int | None) -> set[ipaddress.IPv4Address]:
        del host, port
        return {ipaddress.ip_address("8.8.8.8")}

    monkeypatch.setattr(loader, "_resolved_addresses", public_address)
    payload = json.dumps(openapi_document()).encode()
    with openapi_material_server(payload) as url:
        request = OpenAPIConnectorGenerationRequest.model_validate(
            {
                **generation_body("https://inventory.example"),
                "document": "",
                "document_url": url,
                "allowed_document_hosts": ["127.0.0.1"],
                "allow_insecure_document_http": True,
            }
        )
        document, provenance, _ = await loader.load(request)
        assert document["info"]["title"] == "Inventory API"
        assert provenance.source_kind == "url"
        assert connected_hosts == ["8.8.8.8"]

    with openapi_material_server(b"x" * 5_000_001) as url:
        oversized = OpenAPIConnectorGenerationRequest.model_validate(
            {
                **generation_body("https://inventory.example"),
                "document": "",
                "document_url": url,
                "allowed_document_hosts": ["127.0.0.1"],
                "allow_insecure_document_http": True,
            }
        )
        with pytest.raises(OpenAPIMaterialError) as captured:
            await loader.load(oversized)
        assert captured.value.gap.capability == "document_size"
        assert connected_hosts[-1] == "8.8.8.8"

    assert set(connected_hosts) == {"8.8.8.8"}

    dns_loader = OpenAPIMaterialLoader()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
    )
    dns_request = OpenAPIConnectorGenerationRequest.model_validate(
        {
            **generation_body("https://inventory.example"),
            "document": "",
            "document_url": "https://allowed.example/openapi.json",
            "allowed_document_hosts": ["allowed.example"],
        }
    )
    with pytest.raises(OpenAPIMaterialError) as dns_error:
        await dns_loader.load(dns_request)
    assert dns_error.value.gap.capability == "document_host"

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(socket.gaierror("dns unavailable")),
    )
    failed_dns_loader = OpenAPIMaterialLoader()
    with pytest.raises(OpenAPIMaterialError) as failed_dns:
        await failed_dns_loader.load(dns_request)
    assert failed_dns.value.gap.capability == "document_dns"


def test_api_generates_tests_and_registers_without_authored_manifest(tmp_path: Path) -> None:
    with generated_contract_server() as base_url:
        with TestClient(create_app(settings(tmp_path))) as client:
            generated_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=generation_body(base_url),
            )
            assert generated_response.status_code == 201, generated_response.text
            generated = generated_response.json()
            assert generated["discovered_operation_count"] == 2
            assert generated["generated_operation_count"] == 2
            assert generated["mapped_field_count"] == generated["total_field_count"] == 11
            assert generated["provenance"]["source_digest"]
            operations = {item["id"]: item for item in generated["manifest"]["operations"]}
            assert operations["getItem"]["parameters"] == [
                {
                    "input_key": "item_id",
                    "wire_name": "item_id",
                    "location": "path",
                    "required": True,
                    "style": "simple",
                    "explode": False,
                },
                {
                    "input_key": "verbose",
                    "wire_name": "verbose",
                    "location": "query",
                    "required": False,
                    "style": "form",
                    "explode": True,
                },
                {
                    "input_key": "X_Trace",
                    "wire_name": "X-Trace",
                    "location": "header",
                    "required": False,
                    "style": "simple",
                    "explode": False,
                },
                {
                    "input_key": "session",
                    "wire_name": "session",
                    "location": "cookie",
                    "required": False,
                    "style": "form",
                    "explode": True,
                },
            ]
            assert operations["createItem"]["request_body"]["input_key"] == "body"
            assert (
                "server_id"
                not in operations["createItem"]["request_schema"]["json_schema"]["properties"][
                    "body"
                ]["properties"]
            )
            generation_id = generated["id"]

            cases_response = client.get(
                f"/api/v1/connectors/generations/{generation_id}/contract-cases",
                headers=HEADERS,
            )
            assert cases_response.status_code == 200
            assert {item["kind"] for item in cases_response.json()} == {"positive", "negative"}

            blocked_register = client.post(
                f"/api/v1/connectors/generations/{generation_id}/register",
                headers=HEADERS,
            )
            assert blocked_register.status_code == 422
            assert "contract run must pass" in blocked_register.text

            secret = client.post(
                "/api/v1/platform/secrets",
                headers=HEADERS,
                json={
                    "owner_id": "contract-test",
                    "name": "generated-basic",
                    "value": "test:secret",
                },
            )
            assert secret.status_code == 201, secret.text

            run_response = client.post(
                f"/api/v1/connectors/generations/{generation_id}/contract-runs",
                headers=HEADERS,
                json={
                    "allow_mutating_operations": True,
                    "owner_id": "contract-test",
                    "secret_ref": "secret://contract-test/generated-basic",
                },
            )
            assert run_response.status_code == 201, run_response.text
            run = run_response.json()
            assert run["status"] == "passed"
            assert run["failed"] == 0
            assert run["attempts"] == 2
            assert run["time_to_first_valid_contract_ms"] is not None
            create_result = next(
                item
                for item in run["results"]
                if item["case"]["operation_id"] == "createItem"
                and item["case"]["kind"] == "positive"
            )
            assert create_result["response_evidence"]["identity"] == {"id": "created"}
            assert len(create_result["response_evidence"]["sha256"]) == 64
            assert create_result["executed_input_evidence"]["body_preview"] == {
                "body": {"name": "Created"},
            }
            assert create_result["response_evidence"]["body_preview"] == {
                "id": "created",
                "name": "Created",
                "meta": {"source": "contract"},
            }
            assert GeneratedContractHandler.received_headers == ["contract"]
            assert GeneratedContractHandler.received_paths == ["/items/folder%2Fitem?verbose=true"]
            assert GeneratedContractHandler.received_cookies == ["session=cookie-contract"]
            assert GeneratedContractHandler.received_bodies == [{"name": "Created"}]
            assert GeneratedContractHandler.received_content_types == ["application/json"]
            assert GeneratedContractHandler.received_authorization == [
                "Basic dGVzdDpzZWNyZXQ=",
                "Basic dGVzdDpzZWNyZXQ=",
            ]

            registered = client.post(
                f"/api/v1/connectors/generations/{generation_id}/register",
                headers=HEADERS,
            )
            assert registered.status_code == 201, registered.text
            assert (
                registered.json()["source_provenance"]["source_digest"]
                == generated["provenance"]["source_digest"]
            )

            repeated = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=generation_body(base_url),
            )
            assert repeated.status_code == 201
            assert repeated.json()["id"] == generation_id


def test_live_mutation_contract_requires_double_opt_in_and_redacts_evidence(
    tmp_path: Path,
) -> None:
    owner_secret = "isolated-live-owner-secret"
    with isolated_live_mutation_contract_server() as base_url:
        document = openapi_document()
        item_schema = document["components"]["schemas"]["Item"]
        item_schema["properties"]["secret_token"] = {"type": "string"}
        item_schema["required"].append("secret_token")
        body = generation_body(base_url, document)
        body.update(
            {
                "connector_id": "generated_isolated_live_inventory",
                "include_operation_ids": ["createItem"],
            }
        )
        body["deployment"].update(
            {
                "environment": "live",
                "claim_ceiling": "H4",
            }
        )

        with TestClient(create_app(settings(tmp_path))) as client:
            request_schema = client.get("/openapi.json").json()["components"][
                "schemas"
            ]["ConnectorContractRunRequest"]
            isolation_flag_schema = request_schema["properties"][
                "allow_isolated_live_mutations"
            ]
            assert isolation_flag_schema["default"] is False
            assert isolation_flag_schema["type"] == "boolean"
            assert "second opt-in" in isolation_flag_schema["description"]

            generation_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=body,
            )
            assert generation_response.status_code == 201, generation_response.text
            generation = generation_response.json()
            generation_id = generation["id"]

            secret_response = client.post(
                "/api/v1/platform/secrets",
                headers=HEADERS,
                json={
                    "owner_id": "contract-test",
                    "name": "isolated-live",
                    "value": owner_secret,
                },
            )
            assert secret_response.status_code == 201, secret_response.text
            run_base = {
                "operation_ids": ["createItem"],
                "owner_id": "contract-test",
                "secret_ref": "secret://contract-test/isolated-live",
            }

            default_response = client.post(
                f"/api/v1/connectors/generations/{generation_id}/contract-runs",
                headers=HEADERS,
                json=run_base,
            )
            assert default_response.status_code == 201, default_response.text
            default_run = default_response.json()
            assert default_run["status"] == "partial"
            assert default_run["skipped"] == 1
            assert default_run["unsupported"] == 0
            default_positive = next(
                item
                for item in default_run["results"]
                if item["case"]["kind"] == "positive"
            )
            assert default_positive["actual"] == (
                "mutating contract requires explicit allow_mutating_operations"
            )

            generic_only_response = client.post(
                f"/api/v1/connectors/generations/{generation_id}/contract-runs",
                headers=HEADERS,
                json={**run_base, "allow_mutating_operations": True},
            )
            assert generic_only_response.status_code == 201
            generic_only_run = generic_only_response.json()
            assert generic_only_run["status"] == "partial"
            assert generic_only_run["skipped"] == 0
            assert generic_only_run["unsupported"] == 1
            generic_only_positive = next(
                item
                for item in generic_only_run["results"]
                if item["case"]["kind"] == "positive"
            )
            assert generic_only_positive["actual"] == (
                "live/private mutation contracts require explicit "
                "allow_isolated_live_mutations"
            )

            isolation_only_response = client.post(
                f"/api/v1/connectors/generations/{generation_id}/contract-runs",
                headers=HEADERS,
                json={**run_base, "allow_isolated_live_mutations": True},
            )
            assert isolation_only_response.status_code == 201
            isolation_only_run = isolation_only_response.json()
            assert isolation_only_run["skipped"] == 1
            assert isolation_only_run["unsupported"] == 0
            assert IsolatedLiveMutationContractHandler.received_bodies == []

            double_opt_in_response = client.post(
                f"/api/v1/connectors/generations/{generation_id}/contract-runs",
                headers=HEADERS,
                json={
                    **run_base,
                    "allow_mutating_operations": True,
                    "allow_isolated_live_mutations": True,
                },
            )
            assert double_opt_in_response.status_code == 201, double_opt_in_response.text
            double_opt_in_run = double_opt_in_response.json()
            assert double_opt_in_run["status"] == "passed"
            assert double_opt_in_run["passed"] == 2
            assert double_opt_in_run["skipped"] == 0
            assert double_opt_in_run["unsupported"] == 0
            positive = next(
                item
                for item in double_opt_in_run["results"]
                if item["case"]["kind"] == "positive"
            )
            assert positive["response_evidence"]["body_preview"]["secret_token"] == "***"
            assert positive["response_evidence"]["redacted_fields"] == [
                "$.secret_token"
            ]
            assert IsolatedLiveMutationContractHandler.received_bodies == [
                {"name": "Created"}
            ]
            assert IsolatedLiveMutationContractHandler.received_authorization == [
                "Basic aXNvbGF0ZWQtbGl2ZS1vd25lci1zZWNyZXQ="
            ]

            listed_response = client.get(
                f"/api/v1/connectors/generations/{generation_id}/contract-runs",
                headers=HEADERS,
            )
            assert listed_response.status_code == 200
            for response_text in (
                generation_response.text,
                secret_response.text,
                default_response.text,
                generic_only_response.text,
                isolation_only_response.text,
                double_opt_in_response.text,
                listed_response.text,
            ):
                assert owner_secret not in response_text
                assert IsolatedLiveMutationContractHandler.response_secret not in response_text

            register_response = client.post(
                f"/api/v1/connectors/generations/{generation_id}/register",
                headers=HEADERS,
            )
            assert register_response.status_code == 201, register_response.text


def test_source_drift_marks_prior_contract_evidence_stale(tmp_path: Path) -> None:
    with generated_contract_server() as base_url:
        with TestClient(create_app(settings(tmp_path))) as client:
            first = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=generation_body(base_url),
            ).json()
            changed = openapi_document(title="Inventory API changed")
            second_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=generation_body(base_url, changed),
            )
            assert second_response.status_code == 201, second_response.text
            second = second_response.json()
            assert second["provenance"]["source_digest"] != first["provenance"]["source_digest"]
            old = client.get(
                f"/api/v1/connectors/generations/{first['id']}", headers=HEADERS
            ).json()
            assert old["evidence_stale"] is True
            stale_run = client.post(
                f"/api/v1/connectors/generations/{first['id']}/contract-runs",
                headers=HEADERS,
                json={},
            )
            assert stale_run.status_code == 422
            assert "source document changed" in stale_run.text


def test_generation_staleness_is_scoped_to_connector_version(tmp_path: Path) -> None:
    with generated_contract_server() as base_url:
        with TestClient(create_app(settings(tmp_path))) as client:
            v1_body = generation_body(
                base_url,
                openapi_document(title="Version one inventory API"),
            )
            v2_body = generation_body(
                base_url,
                openapi_document(title="Version two inventory API"),
            )
            v2_body["version"] = 2

            v1_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=v1_body,
            )
            v2_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=v2_body,
            )
            assert v1_response.status_code == 201, v1_response.text
            assert v2_response.status_code == 201, v2_response.text
            v1 = v1_response.json()
            v2 = v2_response.json()
            assert v1["provenance"]["source_digest"] != v2["provenance"]["source_digest"]

            listed = {
                item["id"]: item
                for item in client.get(
                    "/api/v1/connectors/generations",
                    headers=HEADERS,
                ).json()
            }
            assert listed[v1["id"]]["evidence_stale"] is False
            assert listed[v2["id"]]["evidence_stale"] is False

            secret_response = client.post(
                "/api/v1/platform/secrets",
                headers=HEADERS,
                json={
                    "owner_id": "contract-test",
                    "name": "versioned-generation",
                    "value": "test:secret",
                },
            )
            assert secret_response.status_code == 201, secret_response.text
            contract_request = {
                "allow_mutating_operations": True,
                "owner_id": "contract-test",
                "secret_ref": "secret://contract-test/versioned-generation",
            }
            for generation in (v1, v2):
                current_response = client.get(
                    f"/api/v1/connectors/generations/{generation['id']}",
                    headers=HEADERS,
                )
                assert current_response.status_code == 200, current_response.text
                assert current_response.json()["evidence_stale"] is False
                run_response = client.post(
                    f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                    headers=HEADERS,
                    json=contract_request,
                )
                assert run_response.status_code == 201, run_response.text
                assert run_response.json()["status"] == "passed"
                register_response = client.post(
                    f"/api/v1/connectors/generations/{generation['id']}/register",
                    headers=HEADERS,
                )
                assert register_response.status_code == 201, register_response.text
                assert register_response.json()["version"] == generation["version"]

            drifted_v1_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=generation_body(
                    base_url,
                    openapi_document(title="Version one inventory API drifted"),
                ),
            )
            assert drifted_v1_response.status_code == 201, drifted_v1_response.text
            drifted_v1 = drifted_v1_response.json()

            listed_after_drift = {
                item["id"]: item
                for item in client.get(
                    "/api/v1/connectors/generations",
                    headers=HEADERS,
                ).json()
            }
            assert listed_after_drift[v1["id"]]["evidence_stale"] is True
            assert listed_after_drift[drifted_v1["id"]]["evidence_stale"] is False
            assert listed_after_drift[v2["id"]]["evidence_stale"] is False

            stale_v1_run = client.post(
                f"/api/v1/connectors/generations/{v1['id']}/contract-runs",
                headers=HEADERS,
                json=contract_request,
            )
            assert stale_v1_run.status_code == 422
            assert "source document changed" in stale_v1_run.text

            still_valid_v2_registration = client.post(
                f"/api/v1/connectors/generations/{v2['id']}/register",
                headers=HEADERS,
            )
            assert still_valid_v2_registration.status_code == 201, (
                still_valid_v2_registration.text
            )


def test_negative_contract_failure_blocks_verified_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with generated_contract_server() as base_url:
        app = create_app(settings(tmp_path))
        with TestClient(app) as client:
            generation = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=generation_body(base_url),
            ).json()
            secret = client.post(
                "/api/v1/platform/secrets",
                headers=HEADERS,
                json={
                    "owner_id": "contract-test",
                    "name": "negative-gate",
                    "value": "test:secret",
                },
            )
            assert secret.status_code == 201
            service = app.state.services.openapi_connectors
            original = service.generate_contract_cases

            async def accepted_negative(generation_id: str) -> list[Any]:
                cases = await original(generation_id)
                positive = next(
                    item
                    for item in cases
                    if item.operation_id == "getItem" and item.kind == "positive"
                )
                return [
                    positive,
                    next(
                        item.model_copy(update={"generated_input": positive.generated_input})
                        for item in cases
                        if item.operation_id == "getItem" and item.kind == "negative"
                    ),
                ]

            monkeypatch.setattr(service, "generate_contract_cases", accepted_negative)
            run_response = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                headers=HEADERS,
                json={
                    "operation_ids": ["getItem"],
                    "owner_id": "contract-test",
                    "secret_ref": "secret://contract-test/negative-gate",
                },
            )
            assert run_response.status_code == 201
            run = run_response.json()
            assert run["passed"] == 1
            assert run["failed"] == 1
            assert run["status"] == "failed"
            registration = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/register",
                headers=HEADERS,
            )
            assert registration.status_code == 422


@pytest.mark.parametrize(
    ("scheme", "security_scheme", "expected_authorization", "expected_api_key"),
    [
        (
            "bearerAuth",
            {"type": "http", "scheme": "bearer"},
            "Bearer contract-secret",
            "",
        ),
        (
            "apiKeyAuth",
            {"type": "apiKey", "in": "header", "name": "X-API-Key"},
            "",
            "contract-secret",
        ),
    ],
)
def test_generated_runtime_preserves_bearer_and_api_key_security(
    tmp_path: Path,
    scheme: str,
    security_scheme: dict[str, Any],
    expected_authorization: str,
    expected_api_key: str,
) -> None:
    with generated_contract_server() as base_url:
        document = openapi_document()
        document["security"] = [{scheme: []}]
        document["components"]["securitySchemes"] = {scheme: security_scheme}
        with TestClient(create_app(settings(tmp_path))) as client:
            body = generation_body(base_url, document)
            body["connector_id"] = f"generated_{scheme}"
            generation = client.post(
                "/api/v1/connectors/generations", headers=HEADERS, json=body
            ).json()
            secret_name = f"secret-{scheme}"
            client.post(
                "/api/v1/platform/secrets",
                headers=HEADERS,
                json={
                    "owner_id": "contract-test",
                    "name": secret_name,
                    "value": "contract-secret",
                },
            )
            run = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                headers=HEADERS,
                json={
                    "operation_ids": ["getItem"],
                    "owner_id": "contract-test",
                    "secret_ref": f"secret://contract-test/{secret_name}",
                },
            ).json()
            assert run["status"] == "passed"
            assert GeneratedContractHandler.received_authorization == [expected_authorization]
            assert GeneratedContractHandler.received_api_keys == [expected_api_key]


@pytest.mark.parametrize("auth_prefix", ["Token", "Token "])
def test_generated_contract_qualification_separates_word_style_api_key_prefixes(
    tmp_path: Path,
    auth_prefix: str,
) -> None:
    secret = "qualification-secret"
    with generated_contract_server() as base_url:
        document = openapi_document()
        document["security"] = [{"tokenAuth": []}]
        document["components"]["securitySchemes"] = {
            "tokenAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "Authorization",
            }
        }
        with TestClient(create_app(settings(tmp_path))) as client:
            body = generation_body(base_url, document)
            body["connector_id"] = "generated_token_auth"
            body["deployment"]["auth_prefix"] = auth_prefix
            generation_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=body,
            )
            assert generation_response.status_code == 201
            generation = generation_response.json()

            secret_response = client.post(
                "/api/v1/platform/secrets",
                headers=HEADERS,
                json={
                    "owner_id": "contract-test",
                    "name": "token-auth-secret",
                    "value": secret,
                },
            )
            assert secret_response.status_code == 201

            run_response = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                headers=HEADERS,
                json={
                    "operation_ids": ["getItem"],
                    "owner_id": "contract-test",
                    "secret_ref": "secret://contract-test/token-auth-secret",
                },
            )
            assert run_response.status_code == 201
            run = run_response.json()
            assert run["status"] == "passed"
            assert GeneratedContractHandler.received_authorization == [
                "Token qualification-secret"
            ]
            assert secret not in generation_response.text
            assert secret not in secret_response.text
            assert secret not in run_response.text


@pytest.mark.parametrize(
    ("path", "operation_id", "expected_error"),
    [
        ("/wrong-content/{item_id}", "wrongContent", "content-type 'text/plain'"),
        ("/error/{item_id}", "documentedError", "response status 422; expected 200"),
    ],
)
def test_generated_runtime_rejects_content_type_and_error_status_mismatches(
    tmp_path: Path,
    path: str,
    operation_id: str,
    expected_error: str,
) -> None:
    with generated_contract_server() as base_url:
        document = openapi_document()
        operation = document["paths"].pop("/items/{item_id}")["get"]
        operation["operationId"] = operation_id
        operation["security"] = []
        operation["responses"]["422"] = {"description": "documented error"}
        document["paths"] = {path: {"get": operation}}
        document["security"] = []
        with TestClient(create_app(settings(tmp_path))) as client:
            body = generation_body(base_url, document)
            body["connector_id"] = f"generated_{operation_id}"
            generation = client.post(
                "/api/v1/connectors/generations", headers=HEADERS, json=body
            ).json()
            generated_operation = generation["manifest"]["operations"][0]
            assert generated_operation["error_responses"]["422"] == "documented error"
            run = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                headers=HEADERS,
                json={"operation_ids": [operation_id]},
            ).json()
            assert run["status"] == "failed"
            failed = next(item for item in run["results"] if item["status"] == "failed")
            assert expected_error in failed["actual"]


@pytest.mark.parametrize(
    ("path", "operation_id", "expected_status", "expected_error"),
    [
        ("/wrapped", "wrappedResponse", "passed", ""),
        ("/wrong-envelope", "wrongEnvelope", "failed", "missing required fields"),
    ],
)
def test_generated_runtime_preserves_complete_response_envelope_semantics(
    tmp_path: Path,
    path: str,
    operation_id: str,
    expected_status: str,
    expected_error: str,
) -> None:
    with generated_contract_server() as base_url:
        document = {
            "openapi": "3.1.0",
            "info": {"title": "Envelope API", "version": "1"},
            "paths": {
                path: {
                    "get": {
                        "operationId": operation_id,
                        "responses": {
                            "200": {
                                "description": "wrapped result",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "properties": {
                                                "data": {
                                                    "type": "object",
                                                    "properties": {"id": {"type": "string"}},
                                                    "required": ["id"],
                                                    "additionalProperties": False,
                                                },
                                                "meta": {
                                                    "type": "object",
                                                    "properties": {"count": {"type": "integer"}},
                                                    "required": ["count"],
                                                    "additionalProperties": False,
                                                },
                                            },
                                            "required": ["data", "meta"],
                                            "additionalProperties": False,
                                        }
                                    }
                                },
                            }
                        },
                    }
                }
            },
        }
        with TestClient(create_app(settings(tmp_path))) as client:
            body = generation_body(base_url, document)
            body["connector_id"] = f"generated_{operation_id}"
            generation = client.post(
                "/api/v1/connectors/generations", headers=HEADERS, json=body
            ).json()
            operation = generation["manifest"]["operations"][0]
            assert operation["response_json_schema"]["required"] == ["data", "meta"]
            run = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                headers=HEADERS,
                json={"operation_ids": [operation_id]},
            ).json()
            assert run["status"] == expected_status
            positive = next(item for item in run["results"] if item["case"]["kind"] == "positive")
            expected_body = (
                {"data": {"id": "wrapped"}, "meta": {"count": 1}}
                if path == "/wrapped"
                else {"id": "unwrapped"}
            )
            assert positive["response_evidence"]["body_preview"] == expected_body
            assert positive["response_evidence"]["redacted_fields"] == []
            assert positive["response_evidence"]["canonical_bytes"] > 0
            if expected_error:
                failed = positive
                assert failed["status"] == "failed"
                assert expected_error in failed["actual"]


def composed_quality_event_document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Quality Event API", "version": "1"},
        "paths": {
            "/quality-events": {
                "post": {
                    "operationId": "recordQualityEvent",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "allOf": [
                                        {
                                            "type": "object",
                                            "properties": {
                                                "event_id": {
                                                    "type": "string",
                                                    "readOnly": True,
                                                },
                                                "source": {
                                                    "allOf": [
                                                        {
                                                            "type": "object",
                                                            "properties": {
                                                                "facility": {
                                                                    "type": "string",
                                                                    "example": "line-a",
                                                                }
                                                            },
                                                            "required": ["facility"],
                                                        },
                                                        {
                                                            "type": "object",
                                                            "properties": {
                                                                "device": {
                                                                    "type": "string",
                                                                    "example": "sensor-7",
                                                                }
                                                            },
                                                            "required": ["device"],
                                                        },
                                                    ]
                                                },
                                                "observation": {
                                                    "oneOf": [
                                                        {
                                                            "type": "object",
                                                            "properties": {
                                                                "numeric": {"type": "number"}
                                                            },
                                                            "required": ["numeric"],
                                                        },
                                                        {
                                                            "type": "object",
                                                            "properties": {
                                                                "note": {"type": "string"}
                                                            },
                                                            "required": ["note"],
                                                        },
                                                    ]
                                                },
                                                "routing": {
                                                    "anyOf": [
                                                        {
                                                            "type": "object",
                                                            "properties": {
                                                                "queue": {"type": "string"}
                                                            },
                                                            "required": ["queue"],
                                                        },
                                                        {
                                                            "type": "object",
                                                            "properties": {
                                                                "priority": {
                                                                    "type": "integer"
                                                                }
                                                            },
                                                            "required": ["priority"],
                                                        },
                                                    ]
                                                },
                                            },
                                            "required": [
                                                "event_id",
                                                "source",
                                                "observation",
                                                "routing",
                                            ],
                                        },
                                        {
                                            "type": "object",
                                            "properties": {
                                                "client_reference": {
                                                    "type": "string",
                                                    "writeOnly": True,
                                                    "example": "client-42",
                                                }
                                            },
                                            "required": ["client_reference"],
                                        },
                                    ]
                                }
                            }
                        },
                    },
                    "responses": {
                        "202": {
                            "description": "accepted or pending",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "oneOf": [
                                            {
                                                "type": "object",
                                                "properties": {
                                                    "result": {
                                                        "allOf": [
                                                            {
                                                                "type": "object",
                                                                "properties": {
                                                                    "event_id": {
                                                                        "type": "string"
                                                                    }
                                                                },
                                                                "required": ["event_id"],
                                                            },
                                                            {
                                                                "type": "object",
                                                                "properties": {
                                                                    "status": {
                                                                        "type": "string"
                                                                    },
                                                                    "echoed_secret": {
                                                                        "type": "string",
                                                                        "writeOnly": True,
                                                                    },
                                                                },
                                                                "required": [
                                                                    "status",
                                                                    "echoed_secret",
                                                                ],
                                                            },
                                                        ]
                                                    }
                                                },
                                                "required": ["result"],
                                            },
                                            {
                                                "type": "object",
                                                "properties": {
                                                    "ticket": {"type": "integer"},
                                                    "debug": {
                                                        "type": "string",
                                                        "writeOnly": True,
                                                    },
                                                },
                                                "required": ["ticket", "debug"],
                                            },
                                        ]
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


def test_generation_supports_nested_schema_composition_and_directional_fields(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(settings(tmp_path))) as client:
        body = generation_body(
            "https://quality-events.example",
            composed_quality_event_document(),
        )
        body.update(
            {
                "connector_id": "quality_event_contract",
                "domain": "quality",
                "deployment": {
                    **body["deployment"],
                    "base_url": "https://quality-events.example",
                    "allowed_hosts": ["quality-events.example"],
                },
            }
        )
        response = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=body,
        )
        assert response.status_code == 201, response.text
        generated = response.json()
        assert generated["generated_operation_count"] == 1
        assert generated["mapped_field_count"] == generated["total_field_count"] == 10
        assert not any(item["code"] == "IF-06" for item in generated["gaps"])

        operation = generated["manifest"]["operations"][0]
        request_body_schema = operation["request_schema"]["json_schema"]["properties"]["body"]
        request_base = request_body_schema["allOf"][0]
        assert "event_id" not in request_base["properties"]
        assert "event_id" not in request_base["required"]
        assert (
            request_base["properties"]["source"]["allOf"][1]["properties"]["device"]["type"]
            == "string"
        )
        response_schema = operation["response_json_schema"]
        result_extension = response_schema["oneOf"][0]["properties"]["result"]["allOf"][1]
        assert "echoed_secret" not in result_extension["properties"]
        assert result_extension["required"] == ["status"]
        assert "debug" not in response_schema["oneOf"][1]["properties"]
        assert response_schema["oneOf"][1]["required"] == ["ticket"]
        assert operation["response_root_type"] == "object"

        cases = client.get(
            f"/api/v1/connectors/generations/{generated['id']}/contract-cases",
            headers=HEADERS,
        ).json()
        positive = next(item for item in cases if item["kind"] == "positive")
        sample = positive["generated_input"]
        assert sample == {
            "body": {
                "source": {"facility": "line-a", "device": "sensor-7"},
                "observation": {"numeric": 1.0},
                "routing": {"queue": "example"},
                "client_reference": "client-42",
            }
        }
        ConnectorObjectSchema._validate_json_schema(
            sample,
            operation["request_schema"]["json_schema"],
            label="generated request",
        )
        ConnectorObjectSchema._validate_json_schema(
            {"result": {"event_id": "evt-1", "status": "accepted"}},
            response_schema,
            label="generated response",
        )
        ConnectorObjectSchema._validate_json_schema(
            {"ticket": 9},
            response_schema,
            label="generated response",
        )
        with pytest.raises(ValueError, match="exactly one"):
            ConnectorObjectSchema._validate_json_schema(
                {},
                response_schema,
                label="generated response",
            )


def test_json_schema_validator_enforces_all_any_and_exactly_one_composition() -> None:
    schema = {
        "allOf": [
            {
                "type": "object",
                "properties": {"record_id": {"type": "string"}},
                "required": ["record_id"],
            },
            {
                "type": "object",
                "properties": {
                    "payload": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "integer"},
                        ]
                    },
                    "measurement": {
                        "oneOf": [
                            {"type": "number"},
                            {"type": "integer"},
                        ]
                    },
                },
                "required": ["payload", "measurement"],
            },
        ]
    }
    ConnectorObjectSchema._validate_json_schema(
        {"record_id": "r-1", "payload": 3, "measurement": 2.5},
        schema,
        label="composition",
    )
    with pytest.raises(ValueError, match="missing required"):
        ConnectorObjectSchema._validate_json_schema(
            {"record_id": "r-1", "payload": 3},
            schema,
            label="composition",
        )
    with pytest.raises(ValueError, match="at least one"):
        ConnectorObjectSchema._validate_json_schema(
            {"record_id": "r-1", "payload": True, "measurement": 2.5},
            schema,
            label="composition",
        )
    with pytest.raises(ValueError, match="exactly one"):
        ConnectorObjectSchema._validate_json_schema(
            {"record_id": "r-1", "payload": "ok", "measurement": 2},
            schema,
            label="composition",
        )


def test_json_schema_validator_accepts_nullable_all_of_without_weakening_branches() -> None:
    nullable_composed_schema = {
        "nullable": True,
        "allOf": [
            {
                "type": "object",
                "properties": {"code": {"type": "string"}},
                "required": ["code"],
            }
        ],
    }
    ConnectorObjectSchema._validate_json_schema(
        None,
        nullable_composed_schema,
        label="nullable composition",
    )
    ConnectorObjectSchema._validate_json_schema(
        {"code": "A-1"},
        nullable_composed_schema,
        label="nullable composition",
    )
    with pytest.raises(ValueError, match="missing required"):
        ConnectorObjectSchema._validate_json_schema(
            {},
            nullable_composed_schema,
            label="nullable composition",
        )
    with pytest.raises(ValueError, match="must not be null"):
        ConnectorObjectSchema._validate_json_schema(
            None,
            {
                "allOf": [
                    {
                        "type": "object",
                        "properties": {"code": {"type": "string"}},
                    }
                ]
            },
            label="non-nullable composition",
        )


def test_generation_supports_bounded_large_parameter_sets_and_types_excess(
    tmp_path: Path,
) -> None:
    supported_parameters = [
        {
            "name": f"filter_{index}",
            "in": "query",
            "schema": {"type": "string"},
        }
        for index in range(128)
    ]
    excessive_parameters = [
        {
            "name": f"option_{index}",
            "in": "query",
            "schema": {"type": "string"},
        }
        for index in range(1_001)
    ]
    response = {
        "200": {
            "description": "ok",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"count": {"type": "integer"}},
                        "required": ["count"],
                    }
                }
            },
        }
    }
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Search Contract API", "version": "1"},
        "paths": {
            "/search": {
                "get": {
                    "operationId": "searchRecords",
                    "parameters": supported_parameters,
                    "responses": response,
                }
            },
            "/unbounded-search": {
                "get": {
                    "operationId": "unboundedSearch",
                    "parameters": excessive_parameters,
                    "responses": response,
                }
            },
            "/conflicted-result": {
                "get": {
                    "operationId": "conflictedResult",
                    "responses": {
                        "200": {
                            "description": "impossible result",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "allOf": [
                                            {"type": "string"},
                                            {"type": "object"},
                                        ]
                                    }
                                }
                            },
                        }
                    },
                }
            },
        },
    }
    with TestClient(create_app(settings(tmp_path))) as client:
        body = generation_body("https://search-contract.example", document)
        body.update(
            {
                "connector_id": "search_contract",
                "domain": "search",
                "deployment": {
                    **body["deployment"],
                    "base_url": "https://search-contract.example",
                    "allowed_hosts": ["search-contract.example"],
                },
            }
        )
        result = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=body,
        )
        assert result.status_code == 201, result.text
        generated = result.json()
        assert generated["discovered_operation_count"] == 3
        assert generated["generated_operation_count"] == 1
        assert len(generated["manifest"]["operations"][0]["parameters"]) == 128
        assert any(
            item["code"] == "IF-04" and item["capability"] == "parameter_count"
            for item in generated["gaps"]
        )
        assert any(
            item["code"] == "IF-06"
            and item["capability"] == "schema_composition_conflict"
            for item in generated["gaps"]
        )


def test_generation_identity_includes_deployment_and_migrates_legacy_rows(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    first_body = generation_body("https://contract-a.example")
    first_body["deployment"]["allowed_hosts"] = ["contract-a.example"]
    with TestClient(create_app(configured)) as client:
        first = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=first_body,
        ).json()
        repeated = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=first_body,
        ).json()
        assert repeated["id"] == first["id"]
        assert first["request_fingerprint"].startswith("sha256:")

        second_body = {
            **first_body,
            "deployment": {
                **first_body["deployment"],
                "profile_id": "alternate-test",
                "base_url": "https://contract-b.example",
                "allowed_hosts": ["contract-b.example"],
                "timeout_seconds": 45,
            },
        }
        second = client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=second_body,
        ).json()
        assert second["id"] != first["id"]
        assert second["request_fingerprint"] != first["request_fingerprint"]

    database = configured.data_dir / "agent_platform.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            ALTER TABLE openapi_connector_generations
            RENAME TO openapi_connector_generations_current;
            CREATE TABLE openapi_connector_generations (
              id TEXT PRIMARY KEY,
              connector_id TEXT NOT NULL,
              version INTEGER NOT NULL,
              source_digest TEXT NOT NULL,
              record_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              UNIQUE(connector_id,version,source_digest)
            );
            INSERT INTO openapi_connector_generations
              (id,connector_id,version,source_digest,record_json,created_at)
            SELECT id,connector_id,version,source_digest,record_json,created_at
            FROM openapi_connector_generations_current
            WHERE id IN (
              SELECT id
              FROM openapi_connector_generations_current
              ORDER BY created_at
              LIMIT 1
            );
            DROP TABLE openapi_connector_generations_current;
            """
        )

    with TestClient(create_app(configured)) as migrated_client:
        migrated = migrated_client.post(
            "/api/v1/connectors/generations",
            headers=HEADERS,
            json=first_body,
        )
        assert migrated.status_code == 201, migrated.text
        assert migrated.json()["id"] == first["id"]
        with sqlite3.connect(database) as connection:
            columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(openapi_connector_generations)"
                )
            }
            assert "request_fingerprint" in columns


def test_registration_requires_latest_run_to_cover_every_contract_case(
    tmp_path: Path,
) -> None:
    with generated_contract_server() as base_url:
        document = openapi_document()
        document["security"] = []
        document["components"]["securitySchemes"] = {}
        first_operation = document["paths"]["/items/{item_id}"]["get"]
        first_operation["security"] = []
        document["paths"] = {
            "/items/{item_id}": {"get": first_operation},
            "/items/{item_id}/history": {
                "get": {
                    **first_operation,
                    "operationId": "getItemHistory",
                    "summary": "Read item history",
                }
            },
        }
        with TestClient(create_app(settings(tmp_path))) as client:
            body = generation_body(base_url, document)
            body["connector_id"] = "generated_contract_coverage"
            generation = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=body,
            ).json()
            partial = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                headers=HEADERS,
                json={"operation_ids": ["getItem"]},
            )
            assert partial.status_code == 201, partial.text
            assert partial.json()["status"] == "passed"
            rejected = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/register",
                headers=HEADERS,
            )
            assert rejected.status_code == 422
            assert "every generated positive and negative case" in rejected.text

            complete = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/contract-runs",
                headers=HEADERS,
                json={},
            )
            assert complete.status_code == 201, complete.text
            assert complete.json()["status"] == "passed"
            registered = client.post(
                f"/api/v1/connectors/generations/{generation['id']}/register",
                headers=HEADERS,
            )
            assert registered.status_code == 201, registered.text


def test_selected_manifest_generates_and_executes_bounded_multipart_blob_contract(
    tmp_path: Path,
) -> None:
    document = {
        "openapi": "3.1.0",
        "info": {"title": "File Submission API", "version": "1"},
        "paths": {
            "/file-submissions": {
                "post": {
                    "operationId": "submitFile",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "multipart/form-data": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "description": {"type": "string"},
                                        "attachment": {
                                            "type": "string",
                                            "format": "binary",
                                        },
                                    },
                                    "required": ["description", "attachment"],
                                },
                                "encoding": {
                                    "attachment": {
                                        "contentType": "application/octet-stream"
                                    }
                                },
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "stored",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"stored": {"type": "boolean"}},
                                        "required": ["stored"],
                                        "additionalProperties": False,
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/administrative-reset": {
                "delete": {
                    "operationId": "resetService",
                    "responses": {
                        "204": {"description": "reset"}
                    },
                }
            },
        },
    }
    with multipart_contract_server() as base_url:
        with TestClient(create_app(settings(tmp_path))) as client:
            body = generation_body(base_url, document)
            body.update(
                {
                    "connector_id": "selected_file_submission",
                    "domain": "file_submission",
                    "include_operation_ids": ["submitFile"],
                }
            )
            generated_response = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=body,
            )
            assert generated_response.status_code == 201, generated_response.text
            generated = generated_response.json()
            assert generated["discovered_operation_count"] == 2
            assert generated["generated_operation_count"] == 1
            assert generated["operation_selection"] == {
                "mode": "include",
                "requested_operation_ids": ["submitFile"],
                "generated_operation_ids": ["submitFile"],
            }
            assert (
                generated["manifest"]["source_provenance"]["operation_selection"]
                == generated["operation_selection"]
            )
            assert [
                item["id"] for item in generated["manifest"]["operations"]
            ] == ["submitFile"]
            request_body = generated["manifest"]["operations"][0]["request_body"]
            assert request_body["content_type"] == "multipart/form-data"
            assert {
                item["wire_name"]: item["kind"]
                for item in request_body["multipart_parts"]
            } == {"description": "text", "attachment": "blob"}
            blob_schema = generated["manifest"]["operations"][0]["request_schema"][
                "json_schema"
            ]["properties"]["body"]["properties"]["attachment"]
            assert blob_schema["x-lilies-blob-contract"] == "inline-base64-v1"
            assert blob_schema["x-lilies-max-decoded-bytes"] == 20 * 1024 * 1024

            excluded_generation = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json={
                    **body,
                    "include_operation_ids": [],
                    "exclude_operation_ids": ["resetService"],
                },
            )
            assert excluded_generation.status_code == 201, excluded_generation.text
            assert excluded_generation.json()["id"] != generated["id"]
            assert (
                excluded_generation.json()["request_fingerprint"]
                != generated["request_fingerprint"]
            )
            assert excluded_generation.json()["operation_selection"]["mode"] == "exclude"

            unknown = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json={
                    **body,
                    "connector_id": "unknown_selection",
                    "include_operation_ids": ["missingOperation"],
                },
            )
            assert unknown.status_code == 422
            assert unknown.json()["detail"]["capability_gap"]["code"] == "IF-04"
            assert (
                unknown.json()["detail"]["capability_gap"]["capability"]
                == "operation_selection"
            )

            content = b"neutral-file"
            digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
            valid_payload = {
                "body": {
                    "description": "neutral upload",
                    "attachment": {
                        "filename": "neutral.bin",
                        "content_type": "application/octet-stream",
                        "content_base64": base64.b64encode(content).decode(),
                        "sha256": digest,
                    },
                }
            }
            persistence_safe = ConnectorService._persistence_safe_payload(
                ConnectorOperation.model_validate(
                    generated["manifest"]["operations"][0]
                ),
                valid_payload,
            )
            assert persistence_safe["body"]["attachment"] == {
                "filename": "neutral.bin",
                "content_type": "application/octet-stream",
                "size_bytes": len(content),
                "sha256": digest,
                "content_redacted": True,
            }
            invalid_blob_run = client.post(
                f"/api/v1/connectors/generations/{generated['id']}/contract-runs",
                headers=HEADERS,
                json={
                    "allow_mutating_operations": True,
                    "sample_inputs": {
                        "submitFile": {
                            "body": {
                                "description": "neutral upload",
                                "attachment": {
                                    "filename": "neutral.bin",
                                    "content_type": "application/octet-stream",
                                    "content_base64": base64.b64encode(content).decode(),
                                    "sha256": "sha256:" + ("0" * 64),
                                },
                            }
                        }
                    },
                },
            )
            assert invalid_blob_run.status_code == 201
            assert invalid_blob_run.json()["status"] == "failed"
            assert "does not match sha256" in next(
                item["actual"]
                for item in invalid_blob_run.json()["results"]
                if item["case"]["kind"] == "positive"
            )
            assert MultipartContractHandler.received_body == b""

            run = client.post(
                f"/api/v1/connectors/generations/{generated['id']}/contract-runs",
                headers=HEADERS,
                json={
                    "allow_mutating_operations": True,
                    "sample_inputs": {
                        "submitFile": valid_payload
                    },
                },
            )
            assert run.status_code == 201, run.text
            assert run.json()["status"] == "passed"
            positive_result = next(
                item
                for item in run.json()["results"]
                if item["case"]["kind"] == "positive"
            )
            assert positive_result["executed_input_evidence"]["body_preview"]["body"][
                "attachment"
            ]["content_base64"] == "***"
            assert MultipartContractHandler.received_content_type.startswith(
                "multipart/form-data; boundary="
            )
            assert b'name="description"' in MultipartContractHandler.received_body
            assert b'name="attachment"; filename="neutral.bin"' in (
                MultipartContractHandler.received_body
            )
            assert content in MultipartContractHandler.received_body
            encoded_content = base64.b64encode(content)
            assert all(
                encoded_content not in database_file.read_bytes()
                for database_file in (tmp_path / "data").glob("agent_platform.db*")
                if database_file.is_file()
            )

            registered = client.post(
                f"/api/v1/connectors/generations/{generated['id']}/register",
                headers=HEADERS,
            )
            assert registered.status_code == 201, registered.text
            assert [item["id"] for item in registered.json()["operations"]] == [
                "submitFile"
            ]


def test_experiment_fixture_does_not_author_connector_contracts() -> None:
    source = Path(__file__).read_text(encoding="utf-8")
    forbidden = [
        "Connector" + "Manifest(",
        "Connector" + "Operation(",
        "ConnectorObject" + "Schema(",
    ]
    assert all(item not in source for item in forbidden)


def test_generalization_runner_uses_only_openapi_material(tmp_path: Path) -> None:
    spec = tmp_path / "openapi.json"
    output = tmp_path / "result.json"
    spec.write_text(json.dumps(openapi_document()), encoding="utf-8")
    command = [
        str(Path(__file__).resolve().parents[1] / ".venv/bin/python"),
        "scripts/run_v04_12_openapi_generalization.py",
        "--name",
        "fixture",
        "--spec",
        str(spec),
        "--connector-id",
        "fixture_connector",
        "--domain",
        "fixture",
        "--base-url",
        "https://fixture.example",
        "--allowed-host",
        "fixture.example",
        "--source-repository",
        "https://example.invalid/repository",
        "--source-commit",
        "frozen-commit",
        "--source-command",
        "fixture generation",
        "--output",
        str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["delivery"]["discovered_operations"] == 2
    assert result["delivery"]["generated_operations"] == 2
    assert result["delivery"]["human_authored_adapter_count"] == 0
    assert result["delivery"]["human_authored_mapping_count"] == 0
    assert result["forbidden_assistance_scan"]["status"] == "pass"


def test_studio_uses_openapi_generation_as_default_and_labels_manual_legacy() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "platform/frontend/app/connector-operations-panel.tsx"
    ).read_text(encoding="utf-8")
    assert 'data-openapi-default-path="true"' in source
    assert "/api/v1/connectors/generations" in source
    assert 'data-connector-action="run-generated-contracts"' in source
    assert 'data-connector-action="register-generated"' in source
    assert 'data-manual-manifest-legacy="true"' in source
    assert "专家旧路径：手工登记 manifest JSON" in source


