from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.blocks import build_block_registry
from agent_platform.config import Settings
from agent_platform.connector_sdk import (
    ConnectorCallback,
    ConnectorConflict,
    ConnectorDenied,
    ConnectorEmbeddingEnvelope,
    ConnectorExecutionRequest,
    ConnectorService,
)
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


ROOT = Path(__file__).resolve().parents[1]
HEADERS = {
    "Authorization": "Bearer connector-test",
    "Content-Type": "application/json",
}
TENANT_SECRET = "controlled-customer-secret"


class DecisionProvider(ModelProvider):
    name = "connector-decision"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        del model, system, messages, tools, max_output_tokens, thinking_enabled, effort
        del tool_choice, user_id
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 1}}},
        )
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "text", "text": ""}},
        )
        yield StreamEvent(
            type="content_block_delta",
            data={"index": 0, "delta": {"type": "text_delta", "text": "approved"}},
        )
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        )


class CustomerSystemHandler(BaseHTTPRequestHandler):
    get_calls = 0
    patch_calls = 0
    compensation_calls = 0
    authorization_headers: list[str] = []
    patch_bodies: list[dict[str, Any]] = []
    slow_reads = False

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.startswith("/cases/"):
            self._send(404, {"error": "not found"})
            return
        if type(self).slow_reads:
            time.sleep(0.1)
        type(self).get_calls += 1
        type(self).authorization_headers.append(self.headers.get("Authorization", ""))
        case_id = path.rsplit("/", 1)[-1]
        self._send(200, {"case_id": case_id, "summary": "Controlled customer case"})

    def do_PATCH(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        body = self._json_body()
        type(self).patch_calls += 1
        type(self).authorization_headers.append(self.headers.get("Authorization", ""))
        type(self).patch_bodies.append(body)
        if body.get("decision") == "force-failure":
            self._send(503, {"error": "controlled writeback failure"})
            return
        case_id = path.rsplit("/", 1)[-1]
        self._send(
            200,
            {
                "case_id": case_id,
                "status": "updated",
                "external_id": f"external-{case_id}",
                "compensation_payload": {
                    "case_id": case_id,
                    "previous_decision": "pending",
                },
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if not path.endswith("/compensate"):
            self._send(404, {"error": "not found"})
            return
        body = self._json_body()
        type(self).compensation_calls += 1
        type(self).authorization_headers.append(self.headers.get("Authorization", ""))
        case_id = path.split("/")[-2]
        self._send(
            200,
            {
                "case_id": case_id,
                "status": "compensated",
                "previous_decision": body.get("previous_decision", ""),
            },
        )

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length) if length else b"{}"
        value = json.loads(payload)
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
def customer_server() -> Iterator[tuple[str, type[CustomerSystemHandler]]]:
    CustomerSystemHandler.get_calls = 0
    CustomerSystemHandler.patch_calls = 0
    CustomerSystemHandler.compensation_calls = 0
    CustomerSystemHandler.authorization_headers = []
    CustomerSystemHandler.patch_bodies = []
    CustomerSystemHandler.slow_reads = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), CustomerSystemHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", CustomerSystemHandler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def settings(tmp_path: Path) -> Settings:
    config = Settings(
        api_token="connector-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
        platform_harness_network_egress_policy="full",
    )
    config.workspace_root.mkdir(parents=True, exist_ok=True)
    return config


def schema(
    schema_id: str,
    fields: list[tuple[str, str, bool]],
) -> dict[str, Any]:
    return {
        "schema_id": schema_id,
        "version": 1,
        "fields": [
            {"name": name, "value_type": value_type, "required": required}
            for name, value_type, required in fields
        ],
        "additional_properties": False,
    }


def manifest(
    base_url: str,
    *,
    version: int = 1,
    environment: str = "test",
    profile_id: str = "test",
    claim_ceiling: str = "H3",
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "connector_id": "customer_system",
        "version": version,
        "title": "Controlled Customer System",
        "description": "Versioned contract for one controlled customer fixture.",
        "domain": "customer_case",
        "created_at": "2026-07-16T00:00:00+00:00",
        "operations": [
            {
                "id": "get_case",
                "title": "Get case",
                "kind": "read",
                "method": "GET",
                "path": "/cases/{case_id}",
                "request_schema": schema(
                    "customer.case.read.request",
                    [("case_id", "string", True)],
                ),
                "response_schema": schema(
                    "customer.case.read.response",
                    [("case_id", "string", True), ("summary", "string", True)],
                ),
                "required_roles": ["operator"],
            },
            {
                "id": "update_case",
                "title": "Update case",
                "kind": "write",
                "method": "PATCH",
                "path": "/cases/{case_id}",
                "request_schema": schema(
                    "customer.case.update.request",
                    [("case_id", "string", True), ("decision", "string", True)],
                ),
                "response_schema": schema(
                    "customer.case.update.response",
                    [
                        ("case_id", "string", True),
                        ("status", "string", True),
                        ("external_id", "string", True),
                        ("compensation_payload", "object", True),
                    ],
                ),
                "required_roles": ["operator"],
                "compensation_operation_id": "restore_case",
            },
            {
                "id": "restore_case",
                "title": "Restore case",
                "kind": "compensate",
                "method": "POST",
                "path": "/cases/{case_id}/compensate",
                "request_schema": schema(
                    "customer.case.restore.request",
                    [
                        ("case_id", "string", True),
                        ("previous_decision", "string", True),
                    ],
                ),
                "response_schema": schema(
                    "customer.case.restore.response",
                    [
                        ("case_id", "string", True),
                        ("status", "string", True),
                        ("previous_decision", "string", True),
                    ],
                ),
                "required_roles": ["operator"],
            },
        ],
        "deployment_profiles": [
            {
                "id": profile_id,
                "environment": environment,
                "base_url": base_url,
                "auth_type": "bearer",
                "allowed_hosts": ["127.0.0.1"],
                "available": True,
                "timeout_seconds": 5,
                "claim_ceiling": claim_ceiling,
                "excluded_claims": ["customer production readiness"],
            }
        ],
        "callback_schema": schema(
            "customer.case.callback",
            [("phase", "string", True), ("note", "string", False)],
        ),
    }


def register_manifest(client: TestClient, base_url: str, **kwargs: Any) -> dict[str, Any]:
    response = client.post(
        "/api/v1/connectors/manifests",
        headers=HEADERS,
        json=manifest(base_url, **kwargs),
    )
    assert response.status_code == 201, response.text
    return response.json()


def register_tenant(
    client: TestClient,
    *,
    application_ids: list[str],
    tenant_id: str = "test-tenant",
    external_tenant_id: str = "customer-acme",
    external_subject: str = "subject-operator",
    actor_id: str = "test-operator",
    profile_id: str = "test",
    connector_version: int = 1,
    secret: str = TENANT_SECRET,
) -> tuple[dict[str, Any], dict[str, Any]]:
    saved_secret = client.post(
        "/api/v1/platform/secrets",
        headers=HEADERS,
        json={
            "owner_id": tenant_id,
            "name": "customer-system",
            "value": secret,
            "description": "controlled Connector HMAC and bearer secret",
        },
    )
    assert saved_secret.status_code == 201, saved_secret.text
    assert secret not in saved_secret.text
    binding = client.put(
        "/api/v1/connectors/bindings",
        headers=HEADERS,
        json={
            "expected_revision": 0,
            "binding": {
                "connector_id": "customer_system",
                "connector_version": connector_version,
                "tenant_id": tenant_id,
                "external_tenant_id": external_tenant_id,
                "profile_id": profile_id,
                "secret_ref": f"secret://{tenant_id}/customer-system",
                "application_ids": application_ids,
                "allowed_operations": ["get_case", "update_case", "restore_case"],
                "subjects": [
                    {
                        "external_subject": external_subject,
                        "actor_id": actor_id,
                        "roles": ["operator"],
                    }
                ],
            },
        },
    )
    assert binding.status_code == 200, binding.text
    policy = client.put(
        "/api/v1/connectors/policies",
        headers=HEADERS,
        json={
            "expected_revision": 0,
            "policy": {
                "connector_id": "customer_system",
                "connector_version": connector_version,
                "tenant_id": tenant_id,
                "domain": "customer_case",
                "allowed_profiles": [profile_id],
                "allowed_operations": ["get_case", "update_case", "restore_case"],
                "required_roles": ["operator"],
                "max_payload_bytes": 10000,
                "mutation_preauthorization_required": True,
                "allow_dry_run": True,
                "allow_compensation_during_stop": True,
            },
        },
    )
    assert policy.status_code == 200, policy.text
    return binding.json(), policy.json()


def execute_body(
    *,
    operation_id: str,
    payload: dict[str, Any],
    idempotency_key: str,
    authorization_id: str = "",
    dry_run: bool = False,
    tenant_id: str = "test-tenant",
    actor_id: str = "test-operator",
    actor_roles: list[str] | None = None,
    profile_id: str = "test",
    connector_version: int = 1,
    application_id: str = "app-controlled",
    run_id: str = "run-controlled",
) -> dict[str, Any]:
    return {
        "connector_id": "customer_system",
        "connector_version": connector_version,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "actor_roles": actor_roles or ["operator"],
        "profile_id": profile_id,
        "operation_id": operation_id,
        "payload": payload,
        "idempotency_key": idempotency_key,
        "authorization_id": authorization_id,
        "dry_run": dry_run,
        "application_id": application_id,
        "run_id": run_id,
    }


def create_authorization(
    client: TestClient,
    *,
    operation_id: str,
    payload: dict[str, Any],
    tenant_id: str = "test-tenant",
    actor_id: str = "test-operator",
    profile_id: str = "test",
    connector_version: int = 1,
    expires_in_seconds: int = 300,
    max_uses: int = 1,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/connectors/authorizations",
        headers=HEADERS,
        json={
            "connector_id": "customer_system",
            "connector_version": connector_version,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "profile_id": profile_id,
            "operation_id": operation_id,
            "payload": payload,
            "expires_in_seconds": expires_in_seconds,
            "max_uses": max_uses,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def wait_for_run(client: TestClient, run_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for _ in range(300):
        response = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS)
        assert response.status_code == 200, response.text
        result = response.json()
        if result["status"] in {"succeeded", "failed", "cancelled", "paused"}:
            return result
        time.sleep(0.01)
    return result


def signed_envelope(
    *,
    application_id: str,
    nonce: str,
    idempotency_key: str,
    secret: str = TENANT_SECRET,
    external_tenant_id: str = "customer-acme",
    external_subject: str = "subject-operator",
    write_mode: str = "dry_run",
    authorization_id: str = "",
    issued_at: datetime | None = None,
) -> tuple[dict[str, Any], str]:
    issued = issued_at or datetime.now(timezone.utc)
    body = {
        "connector_id": "customer_system",
        "connector_version": 1,
        "application_id": application_id,
        "external_tenant_id": external_tenant_id,
        "external_subject": external_subject,
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(minutes=5)).isoformat(),
        "nonce": nonce,
        "idempotency_key": idempotency_key,
        "authorization_id": authorization_id,
        "write_mode": write_mode,
        "request": {"case_id": "case-001"},
    }
    return body, ConnectorService.sign_payload(secret, body)


def test_manifest_schema_contract_is_immutable_restart_safe_and_secret_free(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    with customer_server() as (base_url, handler):
        app = create_app(config, DecisionProvider())
        with TestClient(app) as client:
            registered = register_manifest(client, base_url)
            assert registered["deployment_profiles"][0]["environment"] == "test"
            register_tenant(client, application_ids=["app-controlled"])

            contract = client.get(
                "/api/v1/connectors/manifests/customer_system/1/contract",
                headers=HEADERS,
            )
            assert contract.status_code == 200, contract.text
            assert contract.json()["paths"]["/cases/{case_id}"]["get"][
                "operationId"
            ] == "get_case"
            assert contract.json()["paths"]["/cases/{case_id}"]["patch"][
                "operationId"
            ] == "update_case"
            assert contract.json()["callbackSchema"]["schema_id"] == (
                "customer.case.callback"
            )

            malformed = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="get_case",
                    payload={"case_id": "case-001", "undeclared": True},
                    idempotency_key="invalid-schema",
                ),
            )
            assert malformed.status_code == 422
            assert "undeclared" in malformed.text
            assert handler.get_calls == 0

            changed = manifest(base_url)
            changed["description"] = "Mutation of an immutable version"
            conflict = client.post(
                "/api/v1/connectors/manifests",
                headers=HEADERS,
                json=changed,
            )
            assert conflict.status_code == 409

            duplicate_schema = manifest(base_url, version=2)
            duplicate_schema["operations"][0]["request_schema"]["fields"].append(
                {"name": "case_id", "value_type": "string", "required": True}
            )
            rejected = client.post(
                "/api/v1/connectors/manifests",
                headers=HEADERS,
                json=duplicate_schema,
            )
            assert rejected.status_code == 422

        persisted_bytes = b"".join(
            path.read_bytes()
            for path in config.data_dir.rglob("*")
            if path.is_file()
        )
        assert TENANT_SECRET.encode() not in persisted_bytes

        restarted = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(restarted) as client:
            manifests = client.get(
                "/api/v1/connectors/manifests",
                headers=HEADERS,
            ).json()
            bindings = client.get(
                "/api/v1/connectors/bindings",
                headers=HEADERS,
            ).json()
            assert [(item["connector_id"], item["version"]) for item in manifests] == [
                ("customer_system", 1)
            ]
            assert bindings[0]["tenant_id"] == "test-tenant"
            assert bindings[0]["secret_ref"] == (
                "secret://test-tenant/customer-system"
            )
            assert TENANT_SECRET not in json.dumps(bindings)


def test_application_scoped_connector_lists_do_not_cross_tenants(tmp_path: Path) -> None:
    with customer_server() as (base_url, _):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            register_manifest(client, base_url)
            register_tenant(
                client,
                application_ids=["app-a"],
                tenant_id="tenant-a",
                external_tenant_id="customer-a",
                external_subject="subject-a",
                actor_id="operator-a",
                secret="tenant-a-secret",
            )
            register_tenant(
                client,
                application_ids=["app-b"],
                tenant_id="tenant-b",
                external_tenant_id="customer-b",
                external_subject="subject-b",
                actor_id="operator-b",
                secret="tenant-b-secret",
            )
            for application_id, tenant_id, actor_id in (
                ("app-a", "tenant-a", "operator-a"),
                ("app-b", "tenant-b", "operator-b"),
            ):
                response = client.post(
                    "/api/v1/connectors/executions",
                    headers=HEADERS,
                    json=execute_body(
                        operation_id="get_case",
                        payload={"case_id": application_id},
                        idempotency_key=f"scope-{application_id}",
                        dry_run=True,
                        tenant_id=tenant_id,
                        actor_id=actor_id,
                        application_id=application_id,
                    ),
                )
                assert response.status_code == 201, response.text

            bindings = client.get(
                "/api/v1/connectors/bindings",
                headers=HEADERS,
                params={"application_id": "app-a"},
            )
            policies = client.get(
                "/api/v1/connectors/policies",
                headers=HEADERS,
                params={"application_id": "app-a"},
            )
            executions = client.get(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                params={"application_id": "app-a"},
            )

            assert [item["tenant_id"] for item in bindings.json()] == ["tenant-a"]
            assert [item["tenant_id"] for item in policies.json()] == ["tenant-a"]
            assert [item["tenant_id"] for item in executions.json()["items"]] == [
                "tenant-a"
            ]
            assert len(client.get("/api/v1/connectors/bindings", headers=HEADERS).json()) == 2
            assert len(
                client.get("/api/v1/connectors/executions", headers=HEADERS).json()["items"]
            ) == 2


def test_signed_identity_rejects_expiry_replay_unknown_subject_and_cross_tenant(
    tmp_path: Path,
) -> None:
    with customer_server() as (base_url, _):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            register_manifest(client, base_url)
            register_tenant(client, application_ids=["app-controlled"])
            register_tenant(
                client,
                application_ids=["app-other"],
                tenant_id="other-tenant",
                external_tenant_id="customer-other",
                external_subject="subject-other",
                actor_id="other-operator",
                secret="other-controlled-secret",
            )
            service = client.app.state.services.connectors
            body, signature = signed_envelope(
                application_id="app-controlled",
                nonce="nonce-valid-001",
                idempotency_key="identity-valid",
            )
            envelope = ConnectorEmbeddingEnvelope.model_validate(body)

            async def resolve() -> Any:
                return await service.resolve_embedding_identity(envelope, signature)

            identity = client.portal.call(resolve)
            assert identity.model_dump(mode="json") == {
                "connector_id": "customer_system",
                "connector_version": 1,
                "tenant_id": "test-tenant",
                "actor_id": "test-operator",
                "actor_roles": ["operator"],
                "profile_id": "test",
                "application_id": "app-controlled",
            }
            with pytest.raises(ConnectorConflict, match="replay"):
                client.portal.call(resolve)

            expired_body, expired_signature = signed_envelope(
                application_id="app-controlled",
                nonce="nonce-expired-001",
                idempotency_key="identity-expired",
                issued_at=datetime.now(timezone.utc) - timedelta(minutes=20),
            )

            async def resolve_expired() -> Any:
                return await service.resolve_embedding_identity(
                    ConnectorEmbeddingEnvelope.model_validate(expired_body),
                    expired_signature,
                )

            with pytest.raises(ConnectorDenied, match="expired"):
                client.portal.call(resolve_expired)

            unknown_subject, unknown_signature = signed_envelope(
                application_id="app-controlled",
                nonce="nonce-subject-001",
                idempotency_key="identity-subject",
                external_subject="subject-not-mapped",
            )

            async def resolve_unknown() -> Any:
                return await service.resolve_embedding_identity(
                    ConnectorEmbeddingEnvelope.model_validate(unknown_subject),
                    unknown_signature,
                )

            with pytest.raises(ConnectorDenied, match="subject"):
                client.portal.call(resolve_unknown)

            wrong_signature = client.post(
                "/api/v1/embedding/invoke",
                json=body | {"nonce": "nonce-wrong-signature"},
                headers={"X-Lilies-Signature": "invalid"},
            )
            assert wrong_signature.status_code == 403

            cross_tenant = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="get_case",
                    payload={"case_id": "case-cross"},
                    idempotency_key="cross-tenant-actor",
                    tenant_id="other-tenant",
                    actor_id="test-operator",
                    actor_roles=["operator"],
                ),
            )
            assert cross_tenant.status_code == 403
            assert "not mapped" in cross_tenant.text

            role_injection = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="get_case",
                    payload={"case_id": "case-role"},
                    idempotency_key="role-injection",
                    actor_roles=["operator", "admin"],
                ),
            )
            assert role_injection.status_code == 403
            assert "roles do not match" in role_injection.text


def test_dry_run_promotes_only_exact_preauthorized_payload_and_replays_once(
    tmp_path: Path,
) -> None:
    with customer_server() as (base_url, handler):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            register_manifest(client, base_url)
            register_tenant(client, application_ids=["app-controlled"])
            payload = {"case_id": "case-101", "decision": "approved"}
            dry_run = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="update_case",
                    payload=payload,
                    idempotency_key="dry-promote-101",
                    dry_run=True,
                ),
            )
            assert dry_run.status_code == 201, dry_run.text
            assert dry_run.json()["receipt"]["status"] == "dry_run"
            assert handler.patch_calls == 0

            wrong_payload_grant = create_authorization(
                client,
                operation_id="update_case",
                payload={"case_id": "case-101", "decision": "rejected"},
            )
            denied = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="update_case",
                    payload=payload,
                    idempotency_key="dry-promote-101",
                    authorization_id=wrong_payload_grant["id"],
                ),
            )
            assert denied.status_code == 403
            assert handler.patch_calls == 0

            grant = create_authorization(
                client,
                operation_id="update_case",
                payload=payload,
            )
            promoted = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="update_case",
                    payload=payload,
                    idempotency_key="dry-promote-101",
                    authorization_id=grant["id"],
                    run_id="run-promoted",
                ),
            )
            assert promoted.status_code == 201, promoted.text
            receipt = promoted.json()["receipt"]
            assert receipt["status"] == "succeeded"
            assert receipt["side_effect_state"] == "applied"
            assert receipt["compensation_available"] is True
            assert handler.patch_calls == 1
            assert handler.authorization_headers[-1] == f"Bearer {TENANT_SECRET}"

            replay = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="update_case",
                    payload=payload,
                    idempotency_key="dry-promote-101",
                    authorization_id=grant["id"],
                    run_id="run-replay",
                ),
            )
            assert replay.status_code == 201
            assert replay.json()["receipt"]["execution_id"] == receipt["execution_id"]
            assert replay.json()["receipt"]["replayed"] is True
            assert handler.patch_calls == 1

            conflict = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="update_case",
                    payload={"case_id": "case-101", "decision": "changed"},
                    idempotency_key="dry-promote-101",
                    authorization_id=grant["id"],
                ),
            )
            assert conflict.status_code == 409

            events = client.get(
                f"/api/v1/connectors/executions/{receipt['execution_id']}/events",
                headers=HEADERS,
            ).json()
            assert [item["event_type"] for item in events] == [
                "connector.execution.authorized",
                "connector.execution.dry_run_completed",
                "connector.execution.dry_run_promoted",
                "connector.execution.succeeded",
            ]


def test_concurrent_duplicate_and_authorization_use_call_adapter_once(
    tmp_path: Path,
) -> None:
    with customer_server() as (base_url, handler):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            register_manifest(client, base_url)
            register_tenant(client, application_ids=["app-controlled"])
            service = client.app.state.services.connectors
            handler.slow_reads = True
            read_request = ConnectorExecutionRequest.model_validate(
                execute_body(
                    operation_id="get_case",
                    payload={"case_id": "case-concurrent"},
                    idempotency_key="concurrent-read",
                )
            )

            async def concurrent_read() -> list[Any]:
                return await asyncio.gather(
                    service.execute(read_request),
                    service.execute(read_request),
                    return_exceptions=True,
                )

            read_results = client.portal.call(concurrent_read)
            assert sum(not isinstance(item, Exception) for item in read_results) == 1
            assert sum(isinstance(item, ConnectorConflict) for item in read_results) == 1
            assert handler.get_calls == 1
            handler.slow_reads = False

            payload = {"case_id": "case-auth-race", "decision": "approved"}
            grant = create_authorization(
                client,
                operation_id="update_case",
                payload=payload,
                max_uses=1,
            )
            first = ConnectorExecutionRequest.model_validate(
                execute_body(
                    operation_id="update_case",
                    payload=payload,
                    idempotency_key="auth-race-one",
                    authorization_id=grant["id"],
                )
            )
            second = first.model_copy(update={"idempotency_key": "auth-race-two"})

            async def concurrent_writes() -> list[Any]:
                return await asyncio.gather(
                    service.execute(first),
                    service.execute(second),
                    return_exceptions=True,
                )

            write_results = client.portal.call(concurrent_writes)
            assert sum(not isinstance(item, Exception) for item in write_results) == 1
            assert sum(isinstance(item, ConnectorDenied) for item in write_results) == 1
            assert handler.patch_calls == 1


def test_policy_revision_emergency_stop_and_failed_writeback_fail_closed(
    tmp_path: Path,
) -> None:
    with customer_server() as (base_url, handler):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            register_manifest(client, base_url)
            _, policy = register_tenant(
                client,
                application_ids=["app-controlled"],
            )

            stopped = client.post(
                "/api/v1/connectors/policies/customer_system/1/test-tenant/emergency-stop",
                headers=HEADERS,
                json={
                    "enabled": True,
                    "reason": "controlled emergency exercise",
                    "expected_revision": policy["revision"],
                },
            )
            assert stopped.status_code == 200, stopped.text
            assert stopped.json()["revision"] == policy["revision"] + 1
            stale = client.post(
                "/api/v1/connectors/policies/customer_system/1/test-tenant/emergency-stop",
                headers=HEADERS,
                json={
                    "enabled": False,
                    "reason": "stale operator request",
                    "expected_revision": policy["revision"],
                },
            )
            assert stale.status_code == 409

            denied = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="update_case",
                    payload={"case_id": "case-stop", "decision": "approved"},
                    idempotency_key="emergency-stop-denial",
                ),
            )
            assert denied.status_code == 403
            assert "emergency stop" in denied.text
            assert handler.patch_calls == 0
            exercise = client.post(
                "/api/v1/connectors/exercises",
                headers=HEADERS,
                json={
                    "connector_id": "customer_system",
                    "connector_version": 1,
                    "tenant_id": "test-tenant",
                    "kind": "emergency_stop",
                },
            )
            assert exercise.status_code == 201, exercise.text
            assert exercise.json()["status"] == "passed"
            assert exercise.json()["evidence"]["adapter_called"] is False

            resumed = client.post(
                "/api/v1/connectors/policies/customer_system/1/test-tenant/emergency-stop",
                headers=HEADERS,
                json={
                    "enabled": False,
                    "reason": "controlled exercise complete",
                    "expected_revision": stopped.json()["revision"],
                },
            )
            assert resumed.status_code == 200, resumed.text
            failed_payload = {"case_id": "case-failure", "decision": "force-failure"}
            grant = create_authorization(
                client,
                operation_id="update_case",
                payload=failed_payload,
            )
            failed = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="update_case",
                    payload=failed_payload,
                    idempotency_key="controlled-writeback-failure",
                    authorization_id=grant["id"],
                ),
            )
            assert failed.status_code == 502
            records = client.get(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                params={"status": "failed"},
            ).json()["items"]
            assert records[0]["status"] == "failed"
            assert records[0]["side_effect_state"] == "unknown"
            assert handler.patch_calls == 1


def test_callback_and_compensation_are_signed_ordered_explicit_and_idempotent(
    tmp_path: Path,
) -> None:
    with customer_server() as (base_url, handler):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            register_manifest(client, base_url)
            register_tenant(client, application_ids=["app-controlled"])
            payload = {"case_id": "case-recover", "decision": "approved"}
            grant = create_authorization(
                client,
                operation_id="update_case",
                payload=payload,
            )
            written = client.post(
                "/api/v1/connectors/executions",
                headers=HEADERS,
                json=execute_body(
                    operation_id="update_case",
                    payload=payload,
                    idempotency_key="write-before-callback",
                    authorization_id=grant["id"],
                ),
            )
            assert written.status_code == 201, written.text
            execution_id = written.json()["receipt"]["execution_id"]

            callback = ConnectorCallback(
                callback_id="callback-001",
                execution_id=execution_id,
                sequence=1,
                status="customer_acknowledged",
                data={"phase": "accepted", "note": "controlled callback"},
                received_at="2026-07-16T01:00:00+00:00",
            )
            callback_body = callback.model_dump(mode="json")
            callback_signature = ConnectorService.sign_payload(
                TENANT_SECRET,
                callback_body,
            )
            accepted = client.post(
                "/api/v1/connectors/callbacks",
                json=callback_body,
                headers={"X-Lilies-Signature": callback_signature},
            )
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["callback_status"] == "customer_acknowledged"
            replay = client.post(
                "/api/v1/connectors/callbacks",
                json=callback_body,
                headers={"X-Lilies-Signature": callback_signature},
            )
            assert replay.status_code == 409
            invalid = client.post(
                "/api/v1/connectors/callbacks",
                json=callback.model_copy(
                    update={"callback_id": "callback-002", "sequence": 2}
                ).model_dump(mode="json"),
                headers={"X-Lilies-Signature": "invalid"},
            )
            assert invalid.status_code == 403

            compensation_payload = {
                "case_id": "case-recover",
                "previous_decision": "pending",
            }
            compensation_grant = create_authorization(
                client,
                operation_id="restore_case",
                payload=compensation_payload,
            )
            compensated = client.post(
                f"/api/v1/connectors/executions/{execution_id}/compensate",
                headers=HEADERS,
                json={
                    "actor_id": "test-operator",
                    "actor_roles": ["operator"],
                    "authorization_id": compensation_grant["id"],
                    "idempotency_key": "compensate-case-recover",
                },
            )
            assert compensated.status_code == 200, compensated.text
            compensation_id = compensated.json()["execution_id"]
            assert compensated.json()["status"] == "succeeded"
            assert handler.compensation_calls == 1
            repeated = client.post(
                f"/api/v1/connectors/executions/{execution_id}/compensate",
                headers=HEADERS,
                json={
                    "actor_id": "test-operator",
                    "actor_roles": ["operator"],
                    "authorization_id": compensation_grant["id"],
                    "idempotency_key": "another-key-cannot-duplicate-compensation",
                },
            )
            assert repeated.status_code == 200
            assert repeated.json()["execution_id"] == compensation_id
            assert handler.compensation_calls == 1

            exercise = client.post(
                "/api/v1/connectors/exercises",
                headers=HEADERS,
                json={
                    "connector_id": "customer_system",
                    "connector_version": 1,
                    "tenant_id": "test-tenant",
                    "kind": "compensation",
                    "execution_id": execution_id,
                },
            )
            assert exercise.status_code == 201, exercise.text
            assert exercise.json()["status"] == "passed"
            assert exercise.json()["evidence_level"] == "H3"

        restarted = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(restarted) as client:
            exact = client.get(
                f"/api/v1/connectors/executions/{execution_id}",
                headers=HEADERS,
            )
            assert exact.status_code == 200, exact.text
            assert exact.json()["receipt"]["status"] == "compensated"
            assert exact.json()["receipt"]["compensation_execution_id"] == compensation_id
            assert exact.json()["receipt"]["callback_status"] == "customer_acknowledged"


def test_live_profile_exercise_is_explicitly_blocked_from_h5(
    tmp_path: Path,
) -> None:
    with customer_server() as (base_url, _):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            register_manifest(
                client,
                base_url,
                version=2,
                environment="live",
                profile_id="live",
                claim_ceiling="H3",
            )
            register_tenant(
                client,
                application_ids=["live-app"],
                tenant_id="live-tenant",
                external_tenant_id="customer-live",
                external_subject="live-subject",
                actor_id="live-operator",
                profile_id="live",
                connector_version=2,
                secret="live-fixture-secret",
            )
            exercise = client.post(
                "/api/v1/connectors/exercises",
                headers=HEADERS,
                json={
                    "connector_id": "customer_system",
                    "connector_version": 2,
                    "tenant_id": "live-tenant",
                    "kind": "emergency_stop",
                },
            )
            assert exercise.status_code == 201, exercise.text
            assert exercise.json()["status"] == "blocked_by_environment"
            assert exercise.json()["evidence_level"] == "H0"
            assert "production SLO or incident response" in exercise.json()[
                "excluded_claims"
            ]


def create_embedding_application(client: TestClient) -> tuple[str, dict[str, Any]]:
    created = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={
            "name": "Controlled customer embedding",
            "requirement": (
                "Handle one signed customer case and preview or apply a governed writeback."
            ),
        },
    )
    assert created.status_code == 201, created.text
    application_id = created.json()["id"]
    draft = client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()
    return application_id, draft


def apply_embedding_scenario(
    client: TestClient,
    application_id: str,
    draft: dict[str, Any],
) -> dict[str, Any]:
    applied = client.post(
        f"/api/v1/applications/{application_id}/scenarios/customer_system_embedding/apply",
        headers=HEADERS,
        json={
            "expected_revision": draft["revision"],
            "expected_content_hash": draft["content_hash"],
            "idempotency_key": "apply-customer-embedding-scenario",
        },
    )
    assert applied.status_code == 200, applied.text
    return client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()


def test_signed_published_workflow_and_governance_trace_are_tenant_safe(
    tmp_path: Path,
) -> None:
    with customer_server() as (base_url, handler):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            unauthorized = client.get("/api/v1/connectors/manifests")
            assert unauthorized.status_code == 401
            application_id, empty_draft = create_embedding_application(client)
            register_manifest(client, base_url)
            register_tenant(client, application_ids=[application_id])

            catalog = client.get("/api/v1/scenarios", headers=HEADERS)
            assert catalog.status_code == 200, catalog.text
            scenario = next(
                item
                for item in catalog.json()
                if item["id"] == "customer_system_embedding"
            )
            assert scenario["evidence_profile"]["selected_level"] == "H3"
            assert scenario["evidence_profile"]["status"] == "integration_verified"
            assert "real customer production identity" in scenario["evidence_profile"][
                "excluded_claims"
            ]

            draft = apply_embedding_scenario(client, application_id, empty_draft)
            validation = client.post(
                f"/api/v1/applications/{application_id}/draft/validate",
                headers=HEADERS,
            )
            assert validation.status_code == 200, validation.text
            assert validation.json()["valid"] is True, validation.json()
            nodes = draft["snapshot"]["workflow"]["nodes"]
            assert [item["type"] for item in nodes] == [
                "start",
                "variable_assigner",
                "connector_action",
                "llm",
                "variable_assigner",
                "connector_action",
                "answer",
            ]
            writeback = next(item for item in nodes if item["id"] == "customer_writeback")
            assert "base_url" not in json.dumps(writeback)
            assert "secret" not in json.dumps(writeback).casefold()

            tested = client.post(
                f"/api/v1/applications/{application_id}/tests/run",
                headers=HEADERS,
            )
            assert tested.status_code == 200, tested.text
            assert tested.json()["passed"] is True, tested.json()
            assert handler.get_calls == 1
            assert handler.patch_calls == 0

            controlled = client.post(
                f"/api/v1/applications/{application_id}/connector-test-runs",
                headers=HEADERS,
                json={
                    "request": {"case_id": "case-runtime-preview"},
                    "idempotency_key": "customer-runtime-controlled-preview",
                    "use_draft": True,
                },
            )
            assert controlled.status_code == 202, controlled.text
            assert controlled.json()["mode"] == "controlled_test_dry_run"
            assert controlled.json()["tenant_id"] == "test-tenant"
            assert "production evidence are excluded" in controlled.json()[
                "claim_boundary"
            ]
            controlled_run = wait_for_run(client, controlled.json()["run_id"])
            assert controlled_run["status"] == "succeeded", controlled_run
            controlled_answer = controlled_run["state"]["outputs"]["customer_answer"][
                "answer"
            ]
            assert controlled_answer["status"] == "dry_run"
            assert controlled_answer["side_effect_state"] == "none"
            assert handler.get_calls == 2
            assert handler.patch_calls == 0

            published = client.post(
                f"/api/v1/applications/{application_id}/versions",
                headers=HEADERS,
                json={"acknowledge_warnings": True},
            )
            assert published.status_code == 200, published.text

            envelope, signature = signed_envelope(
                application_id=application_id,
                nonce="published-invoke-001",
                idempotency_key="published-workflow-dry-run",
            )
            invoked = client.post(
                "/api/v1/embedding/invoke",
                json=envelope,
                headers={"X-Lilies-Signature": signature},
            )
            assert invoked.status_code == 202, invoked.text
            assert invoked.json()["tenant_id"] == "test-tenant"
            run = wait_for_run(client, invoked.json()["run_id"])
            assert run["status"] == "succeeded", run
            answer = run["state"]["outputs"]["customer_answer"]["answer"]
            assert answer["status"] == "dry_run"
            assert answer["tenant_id"] == "test-tenant"
            assert "authorization_id" not in answer
            assert "request_payload" not in answer
            assert TENANT_SECRET not in json.dumps(run)

            trace = client.get(
                f"/api/v1/governance/traces/{invoked.json()['run_id']}",
                headers=HEADERS,
            )
            assert trace.status_code == 200, trace.text
            connector_trace = trace.json()["connector"]
            assert len(connector_trace["executions"]) == 2
            assert {item["operation_id"] for item in connector_trace["executions"]} == {
                "get_case",
                "update_case",
            }
            assert "request_payload" not in json.dumps(connector_trace)
            assert TENANT_SECRET not in json.dumps(connector_trace)

            management = client.get(
                "/api/v1/governance/connectors",
                headers=HEADERS,
                params={"tenant_id": "test-tenant", "limit": 1, "offset": 0},
            )
            assert management.status_code == 200, management.text
            governance = management.json()
            assert governance["bindings"][0]["tenant_id"] == "test-tenant"
            assert governance["support"]["production_slo"] == "unsupported"
            assert "secret_ref" not in json.dumps(governance)
            assert "request_payload" not in json.dumps(governance)
            assert governance["claim_boundary"].startswith("Tenant-safe")

            filtered = client.get(
                "/api/v1/governance/connectors",
                headers=HEADERS,
                params={
                    "connector_id": "customer_system",
                    "tenant_id": "test-tenant",
                    "operation_id": "update_case",
                    "status": "dry_run",
                    "emergency_stop": False,
                    "limit": 1,
                    "offset": 0,
                },
            )
            assert filtered.status_code == 200, filtered.text
            assert filtered.json()["items"]
            assert all(
                item["tenant_id"] == "test-tenant"
                and item["operation_id"] == "update_case"
                and item["status"] == "dry_run"
                for item in filtered.json()["items"]
            )
            assert all(not item["emergency_stop"] for item in filtered.json()["policies"])

            next_page = client.get(
                "/api/v1/governance/connectors",
                headers=HEADERS,
                params={"tenant_id": "test-tenant", "limit": 1, "offset": 1},
            )
            assert next_page.status_code == 200, next_page.text
            assert governance["items"][0]["execution_id"] != next_page.json()["items"][0][
                "execution_id"
            ]


def test_capability_evaluation_binds_real_connector_carriers_and_caps_claims(
    tmp_path: Path,
) -> None:
    with customer_server() as (base_url, _):
        app = create_app(settings(tmp_path), DecisionProvider())
        with TestClient(app) as client:
            application_id, empty_draft = create_embedding_application(client)
            register_manifest(client, base_url)
            register_tenant(client, application_ids=[application_id])
            draft = apply_embedding_scenario(client, application_id, empty_draft)

            h1 = client.post(
                f"/api/v1/applications/{application_id}/evaluation/runs",
                headers=HEADERS,
                json={"profile_id": "h1_static", "environment_id": "local_mock"},
            )
            assert h1.status_code == 200, h1.text
            assert h1.json()["achieved_status"] == "static_verified"

            plan = client.post(
                f"/api/v1/applications/{application_id}/evaluation/plan",
                headers=HEADERS,
                json={
                    "profile_id": "h3_integration",
                    "environment_id": "local_contract",
                },
            )
            assert plan.status_code == 200, plan.text
            assert plan.json()["eligibility"] == "ready"
            cases = plan.json()["cases"]
            capabilities = {item["capability_ids"][0]: item for item in cases}
            assert {
                "F.embedded_request",
                "F.governed_writeback",
                "G.tenant_isolation",
                "G.idempotent_write",
                "G.compensation",
                "G.audit",
                "X.customer_identity",
                "X.customer_schema",
                "X.customer_writeback",
                "X.customer_callback",
                "X.deployment",
            } == set(capabilities)
            assert capabilities["F.governed_writeback"]["test"][
                "required_node_types"
            ] == ["connector_action", "answer"]
            assert capabilities["G.idempotent_write"]["test"][
                "required_node_types"
            ] == ["connector_action"]
            assert "carrier:platform.connector_callback" in capabilities[
                "X.customer_callback"
            ]["required_signals"]

            applied = client.post(
                f"/api/v1/applications/{application_id}/evaluation/tests/apply",
                headers=HEADERS,
                json={
                    "profile_id": "h3_integration",
                    "environment_id": "local_contract",
                    "expected_revision": draft["revision"],
                    "expected_content_hash": draft["content_hash"],
                    "mode": "replace_generated",
                    "idempotency_key": str(uuid4()),
                },
            )
            assert applied.status_code == 200, applied.text
            current = client.get(
                f"/api/v1/applications/{application_id}/draft",
                headers=HEADERS,
            ).json()
            h3 = client.post(
                f"/api/v1/applications/{application_id}/evaluation/runs",
                headers=HEADERS,
                json={
                    "profile_id": "h3_integration",
                    "environment_id": "local_contract",
                    "expected_revision": current["revision"],
                    "expected_content_hash": current["content_hash"],
                },
            )
            assert h3.status_code == 200, h3.text
            assert h3.json()["outcome"] == "completed", h3.json()
            assert h3.json()["achieved_status"] == "integration_verified"
            assert h3.json()["passed"] is True
            assert "production writeback reliability or SLO" in h3.json()[
                "excluded_claims"
            ]

            h4 = client.post(
                f"/api/v1/applications/{application_id}/evaluation/runs",
                headers=HEADERS,
                json={"profile_id": "h4_live", "environment_id": "configured_live"},
            )
            assert h4.status_code == 200
            assert h4.json()["outcome"] == "blocked"
            assert h4.json()["achieved_status"] == "blocked_by_environment"
            h5 = client.post(
                f"/api/v1/applications/{application_id}/evaluation/runs",
                headers=HEADERS,
                json={
                    "profile_id": "h5_production_observation",
                    "environment_id": "production_observation",
                },
            )
            assert h5.status_code == 200
            assert h5.json()["outcome"] == "blocked"
            assert h5.json()["achieved_status"] == "blocked_by_environment"

            evidence = client.get(
                "/api/v1/governance/capability-evidence",
                headers=HEADERS,
            )
            assert evidence.status_code == 200, evidence.text
            capabilities = {
                item["capability_id"]: item
                for item in evidence.json()["capabilities"]
            }
            for capability_id, gap_field in (
                (
                    "platform.connector_embedding_sdk",
                    "customer_live_and_production_environment",
                ),
                (
                    "platform.governed_connector_writeback",
                    "production_writeback_assurance",
                ),
            ):
                capability = capabilities[capability_id]
                assert capability["strongest_status"] == "integration_verified"
                assert capability["evidence_level"] == "H3"
                assert capability["integrity"] == "intact"
                assert {item["field"] for item in capability["known_gaps"]} == {
                    gap_field
                }
            assert evidence.json()["support"]["production_completeness"] == (
                "unsupported"
            )


def test_v0410_contract_workflow_routes_and_frontend_audience_boundaries() -> None:
    contract = json.loads(
        (ROOT / "docs/evolution-control/stage-contracts/v0.4.10.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(contract["mandatory_tasks"]) == 7
    assert {item["task_id"] for item in contract["mandatory_tasks"]} == {
        f"V04-10-T01{suffix}" for suffix in "ABCDEFG"
    }

    registry = build_block_registry()
    workflow = registry.expand_template(
        "customer_system_embedding",
        prefix="customer-embedding",
    )
    assert registry.validate_workflow(workflow) == []
    assert [item.type for item in workflow.nodes] == [
        "start",
        "variable_assigner",
        "connector_action",
        "llm",
        "variable_assigner",
        "connector_action",
        "answer",
    ]

    api_source = (ROOT / "platform/backend/src/agent_platform/api.py").read_text(
        encoding="utf-8"
    )
    studio_source = (
        ROOT / "platform/frontend/app/applications/[id]/page.tsx"
    ).read_text(encoding="utf-8")
    runtime_source = (
        ROOT / "platform/frontend/app/runtime/[id]/page.tsx"
    ).read_text(encoding="utf-8")
    operations_source = (
        ROOT / "platform/frontend/app/connector-operations-panel.tsx"
    ).read_text(encoding="utf-8")
    governance_source = (
        ROOT / "platform/frontend/app/governance/page.tsx"
    ).read_text(encoding="utf-8")

    for route in (
        "/api/v1/connectors/manifests",
        "/api/v1/connectors/bindings",
        "/api/v1/connectors/policies",
        "/api/v1/connectors/authorizations",
        "/api/v1/connectors/executions",
        "/api/v1/connectors/callbacks",
        "/api/v1/connectors/exercises",
        "/connector-test-runs",
        "/api/v1/embedding/invoke",
        "/api/v1/governance/connectors",
    ):
        assert route in api_source

    assert "'integrations'" in studio_source
    assert 'data-studio-workspace="integrations"' in studio_source
    assert "<ConnectorOperationsPanel" in studio_source
    assert 'data-engineer-connector-workspace="true"' in operations_source
    for action in (
        "registerManifest",
        "saveBinding",
        "savePolicy",
        "async function authorize",
        "setEmergency",
        "compensate",
    ):
        assert action in operations_source

    assert "internalConnectorInputs" in runtime_source
    assert 'data-customer-connector-view="bounded"' in runtime_source
    assert 'data-customer-connector-receipt="redacted"' in runtime_source
    assert "/connector-test-runs" in runtime_source
    assert "resultMarkdown && !connectorWorkflow" in runtime_source
    assert "'variable_assigner'" in runtime_source
    assert "secret_ref" not in runtime_source
    assert "X-Lilies-Signature" not in runtime_source

    assert "Connector Operations" in governance_source
    assert 'data-governance-connectors="tenant-redacted"' in governance_source
    assert "request_payload" not in governance_source
    assert "secret_ref" not in governance_source
