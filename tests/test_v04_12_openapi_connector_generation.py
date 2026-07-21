from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.openapi_connector import (
    OpenAPIMaterialError,
    OpenAPIMaterialLoader,
    OpenAPIConnectorGenerationRequest,
)


HEADERS = {"Authorization": "Bearer openapi-test", "Content-Type": "application/json"}


class GeneratedContractHandler(BaseHTTPRequestHandler):
    received_headers: list[str] = []
    received_authorization: list[str] = []
    received_bodies: list[dict[str, Any]] = []

    def do_GET(self) -> None:  # noqa: N802
        if not self.path.startswith("/items/example"):
            self._send(404, {"error": "not found"})
            return
        type(self).received_headers.append(self.headers.get("X-Trace", ""))
        type(self).received_authorization.append(self.headers.get("Authorization", ""))
        self._send(200, {"id": "example", "name": "Generated item"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/items":
            self._send(404, {"error": "not found"})
            return
        body = self._body()
        type(self).received_bodies.append(body)
        type(self).received_authorization.append(self.headers.get("Authorization", ""))
        self._send(201, {"id": "created", "name": body.get("name", "")})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(length) if length else b"{}")
        return value if isinstance(value, dict) else {}

    def _send(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
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
    GeneratedContractHandler.received_bodies = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), GeneratedContractHandler)
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
                            "schema": {"type": "string", "example": "example"},
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
                    },
                    "required": ["id", "name"],
                    "additionalProperties": False,
                }
            }
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
    schema = document["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
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
                    "components": {"schemas": {"Remote": {"$ref": "https://example.com/schema.json"}}},
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
            assert generated["mapped_field_count"] == generated["total_field_count"] == 8
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
            ]
            assert operations["createItem"]["request_body"]["input_key"] == "body"
            assert "server_id" not in operations["createItem"]["request_schema"][
                "json_schema"
            ]["properties"]["body"]["properties"]
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
            assert GeneratedContractHandler.received_headers == ["contract"]
            assert GeneratedContractHandler.received_bodies == [{"name": "Created"}]
            assert GeneratedContractHandler.received_authorization == [
                "Basic dGVzdDpzZWNyZXQ=",
                "Basic dGVzdDpzZWNyZXQ=",
            ]

            registered = client.post(
                f"/api/v1/connectors/generations/{generation_id}/register",
                headers=HEADERS,
            )
            assert registered.status_code == 201, registered.text
            assert registered.json()["source_provenance"]["source_digest"] == generated["provenance"]["source_digest"]

            repeated = client.post(
                "/api/v1/connectors/generations",
                headers=HEADERS,
                json=generation_body(base_url),
            )
            assert repeated.status_code == 201
            assert repeated.json()["id"] == generation_id


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
        Path(__file__).resolve().parents[1]
        / "platform/frontend/app/connector-operations-panel.tsx"
    ).read_text(encoding="utf-8")
    assert 'data-openapi-default-path="true"' in source
    assert "/api/v1/connectors/generations" in source
    assert 'data-connector-action="run-generated-contracts"' in source
    assert 'data-connector-action="register-generated"' in source
    assert 'data-manual-manifest-legacy="true"' in source
    assert "专家旧路径：手工登记 manifest JSON" in source
