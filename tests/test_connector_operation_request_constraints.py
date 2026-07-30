from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.connector_sdk import (
    ConnectorDenied,
    ConnectorDeploymentProfile,
    ConnectorDomainPolicy,
    ConnectorExecutionRequest,
    ConnectorIdentitySubject,
    ConnectorManifest,
    ConnectorObjectSchema,
    ConnectorOperation,
    ConnectorOperationRequestConstraint,
    ConnectorParameterBinding,
    ConnectorRequestBody,
    ConnectorTenantBinding,
)
from agent_platform.lilies_platform_contract import PLATFORM_CONTRACT_VERSION
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from tests.test_runtime import ScriptedProvider


ZERO_DIGEST = "sha256:" + "0" * 64
INTERNAL_HEADERS = {"Authorization": "Bearer internal-test-token"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="internal-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
        lilies_platform_contract_version=PLATFORM_CONTRACT_VERSION,
    )


def _application(client: TestClient) -> UUID:
    response = client.post(
        "/api/v1/applications",
        headers=INTERNAL_HEADERS,
        json={
            "name": "Warehouse order controls",
            "description": "Neutral request-authority fixture.",
            "requirement": "",
            "mode": "workflow",
            "delivery_mode": "guided",
            "governed_hard_gate": True,
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _order_request_schema(operation_id: str) -> ConnectorObjectSchema:
    return ConnectorObjectSchema(
        schema_id=f"{operation_id}.request",
        json_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "mode": {"type": "string"},
                "body": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string"},
                        "customer_note": {"type": "string"},
                        "priority": {"type": "number"},
                        "internal_code": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
            "required": ["order_id", "body"],
            "additionalProperties": False,
        },
    )


def _order_operation(operation_id: str) -> ConnectorOperation:
    return ConnectorOperation(
        id=operation_id,
        title=operation_id,
        kind="write",
        method="PATCH",
        path="/v1/orders/{order_id}",
        request_schema=_order_request_schema(operation_id),
        response_schema=ConnectorObjectSchema(
            schema_id=f"{operation_id}.response",
            additional_properties=True,
        ),
        parameters=[
            ConnectorParameterBinding(
                input_key="order_id",
                wire_name="order_id",
                location="path",
                required=True,
            ),
            ConnectorParameterBinding(
                input_key="mode",
                wire_name="mode",
                location="query",
            ),
        ],
        request_body=ConnectorRequestBody(input_key="body", required=True),
        required_roles=["operator"],
    )


def _manifest() -> ConnectorManifest:
    return ConnectorManifest(
        connector_id="warehouse",
        version=1,
        title="Warehouse orders",
        description="Neutral warehouse order connector.",
        domain="orders",
        operations=[
            _order_operation("orders.patch"),
            _order_operation("orders.legacy_patch"),
        ],
        deployment_profiles=[
            ConnectorDeploymentProfile(
                id="private",
                environment="private",
                base_url="http://127.0.0.1:19081",
                auth_type="none",
                allowed_hosts=["127.0.0.1"],
                available=True,
                claim_ceiling="H3",
            )
        ],
        created_at="2026-07-26T00:00:00+00:00",
    )


def _reserved_header_manifest() -> ConnectorManifest:
    operations: list[ConnectorOperation] = []
    for suffix, wire_name in (
        ("authorization", "Authorization"),
        ("tenant", "X-Lilies-Tenant"),
        ("actor", "X-Lilies-Actor"),
        ("idempotency", "Idempotency-Key"),
    ):
        operation_id = f"orders.header_{suffix}"
        operation = _order_operation(operation_id)
        request_schema = json.loads(json.dumps(operation.request_schema.json_schema))
        request_schema["properties"]["header_value"] = {"type": "string"}
        operations.append(
            operation.model_copy(
                update={
                    "request_schema": ConnectorObjectSchema(
                        schema_id=f"{operation_id}.request",
                        json_schema=request_schema,
                    ),
                    "parameters": [
                        *operation.parameters,
                        ConnectorParameterBinding(
                            input_key="header_value",
                            wire_name=wire_name,
                            location="header",
                            required=True,
                        ),
                    ],
                }
            )
        )
    return _manifest().model_copy(update={"operations": operations})


async def _register_manifest_and_binding(
    services: Any,
    *,
    application_id: str,
    manifest: ConnectorManifest | None = None,
) -> None:
    manifest = manifest or _manifest()
    await services.connectors.register_manifest(manifest)
    await services.connectors.upsert_binding(
        ConnectorTenantBinding(
            connector_id=manifest.connector_id,
            connector_version=manifest.version,
            tenant_id="warehouse-tenant",
            external_tenant_id="warehouse-external",
            profile_id="private",
            secret_ref="secret://warehouse-tenant/unused",
            application_ids=[application_id],
            allowed_operations=[item.id for item in manifest.operations],
            subjects=[
                ConnectorIdentitySubject(
                    external_subject="builder",
                    actor_id="builder",
                    roles=["operator"],
                )
            ],
        ),
        expected_revision=0,
    )


def _policy_document(*, fixed_path_key: str = "order_id") -> dict[str, Any]:
    return {
        "connector_id": "warehouse",
        "connector_version": 1,
        "tenant_id": "warehouse-tenant",
        "domain": "orders",
        "allowed_profiles": ["private"],
        "allowed_operations": ["orders.patch", "orders.legacy_patch"],
        "required_roles": ["operator"],
        "mutation_preauthorization_required": False,
        "operation_request_constraints": [
            {
                "operation_id": "orders.patch",
                "allowed_body_fields": ["status", "customer_note", "priority"],
                "fixed_path_values": {fixed_path_key: "order-42"},
                "fixed_query_values": {"mode": "bounded"},
                "fixed_body_values": {"status": "ready", "priority": 1},
            }
        ],
    }


def _credential_headers(
    client: TestClient,
    *,
    application_id: UUID,
) -> dict[str, str]:
    assignment_id = uuid4()
    session_id = uuid4()
    credential = client.portal.call(
        client.app.state.services.platform_blackbox_auth.issue_credential,
        TaskCredentialGrant(
            assignment_id=assignment_id,
            session_id=session_id,
            scopes=[PlatformBlackboxScope.catalog_read],
            application_ids=[application_id],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            allowed_network_hosts=["127.0.0.1"],
            connector_access=True,
            writable_host_operations=[
                "warehouse.orders.patch",
                "warehouse.orders.legacy_patch",
            ],
            max_write_count=20,
            max_payload_bytes=1024 * 1024,
        ),
    )
    return {
        "Authorization": f"Bearer {credential.access_token.get_secret_value()}",
        "X-Lilies-Assignment-ID": str(assignment_id),
        "X-Lilies-Session-ID": str(session_id),
        "X-Lilies-Contract-Digest": ZERO_DIGEST,
    }


def _catalog_get(
    client: TestClient,
    path: str,
    headers: dict[str, str],
    *,
    key: str,
):
    return client.get(
        path,
        headers={
            **headers,
            "X-Lilies-Tool-Call-ID": f"tool-{key}",
            "X-Lilies-Idempotency-Key": key,
        },
    )


def _execution(
    *,
    operation_id: str,
    payload: dict[str, Any],
    idempotency_key: str,
    application_id: str = "",
) -> ConnectorExecutionRequest:
    return ConnectorExecutionRequest(
        connector_id="warehouse",
        connector_version=1,
        tenant_id="warehouse-tenant",
        actor_id="builder",
        actor_roles=["operator"],
        profile_id="private",
        operation_id=operation_id,
        payload=payload,
        idempotency_key=idempotency_key,
        application_id=application_id,
    )


def test_policy_narrows_builder_schema_and_execute_rechecks_constraint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _application(client)
        services = client.app.state.services
        client.portal.call(
            partial(
                _register_manifest_and_binding,
                services,
                application_id=str(application_id),
            )
        )
        saved_policy = client.put(
            "/api/v1/connectors/policies",
            headers=INTERNAL_HEADERS,
            json={"policy": _policy_document(), "expected_revision": 0},
        )
        assert saved_policy.status_code == 200, saved_policy.text
        assert saved_policy.json()["operation_request_constraints"] == [
            {
                "operation_id": "orders.patch",
                "allowed_body_fields": ["status", "customer_note", "priority"],
                "fixed_path_values": {"order_id": "order-42"},
                "fixed_query_values": {"mode": "bounded"},
                "fixed_body_values": {"status": "ready", "priority": 1},
            }
        ]

        headers = _credential_headers(client, application_id=application_id)
        contract = _catalog_get(
            client,
            "/api/v1/lilies/platform-contract",
            headers,
            key="orders-constraint-contract",
        )
        assert contract.status_code == 200, contract.text
        headers["X-Lilies-Contract-Digest"] = contract.json()["data"]["contract_digest"]
        catalog = _catalog_get(
            client,
            "/api/v1/lilies/tools",
            headers,
            key="orders-constraint-tools",
        )
        assert catalog.status_code == 200, catalog.text
        descriptors = {
            item["input_schema"]["x-lilies-connector"]["operation_id"]: item
            for item in catalog.json()["data"]
            if str(item.get("name", "")).startswith("connector:warehouse:")
        }
        assert set(descriptors) == {"orders.patch", "orders.legacy_patch"}

        narrowed_payload = descriptors["orders.patch"]["input_schema"]["properties"]["payload"]
        assert narrowed_payload["properties"]["order_id"]["const"] == "order-42"
        assert narrowed_payload["properties"]["mode"]["const"] == "bounded"
        assert {"order_id", "mode", "body"}.issubset(narrowed_payload["required"])
        narrowed_body = narrowed_payload["properties"]["body"]
        assert set(narrowed_body["properties"]) == {
            "status",
            "customer_note",
            "priority",
        }
        assert narrowed_body["properties"]["status"]["const"] == "ready"
        assert narrowed_body["properties"]["priority"]["const"] == 1
        assert {"status", "priority"}.issubset(narrowed_body["required"])
        assert narrowed_body["additionalProperties"] is False

        legacy_body = descriptors["orders.legacy_patch"]["input_schema"]["properties"]["payload"][
            "properties"
        ]["body"]
        assert legacy_body["additionalProperties"] is True
        assert "internal_code" in legacy_body["properties"]

        adapter_payloads: list[dict[str, Any]] = []

        async def fake_adapter(**kwargs: Any) -> dict[str, Any]:
            adapter_payloads.append(kwargs["payload"])
            return {"id": kwargs["payload"]["order_id"]}

        monkeypatch.setattr(services.connectors, "_call_adapter", fake_adapter)
        valid_payload = {
            "order_id": "order-42",
            "mode": "bounded",
            "body": {
                "status": "ready",
                "customer_note": "packed",
                "priority": 1.0,
            },
        }
        valid = client.portal.call(
            partial(
                services.connectors.execute,
                _execution(
                    operation_id="orders.patch",
                    payload=valid_payload,
                    idempotency_key="orders-valid",
                ),
            )
        )
        assert valid.status == "succeeded"
        assert adapter_payloads == [valid_payload]

        invalid_payloads = [
            (
                {
                    **valid_payload,
                    "body": {
                        "status": "ready",
                        "customer_note": "packed",
                        "priority": 1.0,
                        "internal_code": "must-not-pass",
                    },
                },
                "policy-denied fields",
            ),
            ({**valid_payload, "order_id": "order-43"}, "path input drifted"),
            ({**valid_payload, "mode": "unbounded"}, "query input drifted"),
            (
                {**valid_payload, "body": {"status": "hold"}},
                "body field drifted",
            ),
        ]
        for index, (payload, message) in enumerate(invalid_payloads):
            with pytest.raises(ConnectorDenied, match=message):
                client.portal.call(
                    partial(
                        services.connectors.execute,
                        _execution(
                            operation_id="orders.patch",
                            payload=payload,
                            idempotency_key=f"orders-denied-{index}",
                        ),
                    )
                )
        assert adapter_payloads == [valid_payload]
        with pytest.raises(ConnectorDenied, match="outside the connector tenant binding"):
            client.portal.call(
                partial(
                    services.connectors.execute,
                    _execution(
                        operation_id="orders.patch",
                        payload=valid_payload,
                        idempotency_key="orders-wrong-application",
                        application_id="unbound-warehouse-app",
                    ),
                )
            )
        assert adapter_payloads == [valid_payload]

        legacy_payload = {
            "order_id": "order-99",
            "mode": "legacy",
            "body": {
                "status": "hold",
                "internal_code": "legacy-compatible",
                "extension_field": {"source": "warehouse"},
            },
        }
        legacy = client.portal.call(
            partial(
                services.connectors.execute,
                _execution(
                    operation_id="orders.legacy_patch",
                    payload=legacy_payload,
                    idempotency_key="orders-legacy-valid",
                ),
            )
        )
        assert legacy.status == "succeeded"
        assert adapter_payloads[-1] == legacy_payload
        denial_events = client.portal.call(
            partial(
                services.connectors.list_events,
                connector_id="warehouse",
                tenant_id="warehouse-tenant",
            )
        )
        assert (
            len(
                [
                    event
                    for event in denial_events
                    if event["event_type"] == "connector.execution.denied"
                ]
            )
            == len(invalid_payloads) + 1
        )


def test_execute_rejects_platform_controlled_headers_before_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _application(client)
        services = client.app.state.services
        manifest = _reserved_header_manifest()
        client.portal.call(
            partial(
                _register_manifest_and_binding,
                services,
                application_id=str(application_id),
                manifest=manifest,
            )
        )
        policy = ConnectorDomainPolicy(
            connector_id=manifest.connector_id,
            connector_version=manifest.version,
            tenant_id="warehouse-tenant",
            domain=manifest.domain,
            allowed_profiles=["private"],
            allowed_operations=[operation.id for operation in manifest.operations],
            required_roles=["operator"],
            mutation_preauthorization_required=False,
        )
        client.portal.call(
            partial(
                services.connectors.set_policy,
                policy,
                expected_revision=0,
            )
        )
        adapter_payloads: list[dict[str, Any]] = []

        async def fake_adapter(**kwargs: Any) -> dict[str, Any]:
            adapter_payloads.append(kwargs["payload"])
            return {"id": kwargs["payload"]["order_id"]}

        monkeypatch.setattr(services.connectors, "_call_adapter", fake_adapter)
        for index, operation in enumerate(manifest.operations):
            with pytest.raises(
                ConnectorDenied,
                match="platform-controlled request headers",
            ):
                client.portal.call(
                    partial(
                        services.connectors.execute,
                        _execution(
                            operation_id=operation.id,
                            payload={
                                "order_id": "order-42",
                                "mode": "bounded",
                                "header_value": "attempted-override",
                                "body": {"status": "ready"},
                            },
                            idempotency_key=f"orders-header-denied-{index}",
                            application_id=str(application_id),
                        ),
                    )
                )
        assert adapter_payloads == []
        denial_events = client.portal.call(
            partial(
                services.connectors.list_events,
                connector_id="warehouse",
                tenant_id="warehouse-tenant",
            )
        )
        assert (
            len(
                [
                    event
                    for event in denial_events
                    if event["event_type"] == "connector.execution.denied"
                ]
            )
            == 4
        )


def test_policy_rejects_unknown_inputs_and_bounds_constraint_json(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _application(client)
        client.portal.call(
            partial(
                _register_manifest_and_binding,
                client.app.state.services,
                application_id=str(application_id),
            )
        )
        unknown_path = client.put(
            "/api/v1/connectors/policies",
            headers=INTERNAL_HEADERS,
            json={
                "policy": _policy_document(fixed_path_key="unknown_order_key"),
                "expected_revision": 0,
            },
        )
        assert unknown_path.status_code == 422
        assert "unknown parameter inputs" in unknown_path.text

    with pytest.raises(ValidationError, match="at most 100 items"):
        ConnectorOperationRequestConstraint(
            operation_id="orders.patch",
            allowed_body_fields=[f"field_{index}" for index in range(101)],
        )
    with pytest.raises(ValidationError, match="string exceeds the byte limit"):
        ConnectorOperationRequestConstraint(
            operation_id="orders.patch",
            fixed_body_values={"customer_note": "x" * 4097},
        )
    with pytest.raises(ValidationError, match="JSON exceeds the depth limit"):
        nested: dict[str, Any] = {}
        current = nested
        for index in range(10):
            child: dict[str, Any] = {}
            current[f"level_{index}"] = child
            current = child
        ConnectorOperationRequestConstraint(
            operation_id="orders.patch",
            fixed_body_values={"customer_note": nested},
        )


def test_old_sqlite_policy_json_loads_with_no_request_constraints(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _application(client)
        services = client.app.state.services
        client.portal.call(
            partial(
                _register_manifest_and_binding,
                services,
                application_id=str(application_id),
            )
        )
        client.portal.call(
            partial(
                services.connectors.set_policy,
                ConnectorDomainPolicy.model_validate(_policy_document()),
                expected_revision=0,
            )
        )
        with services.storage._connect() as connection:
            row = connection.execute(
                """SELECT record_json FROM connector_domain_policies
                   WHERE connector_id=? AND version=? AND tenant_id=?""",
                ("warehouse", 1, "warehouse-tenant"),
            ).fetchone()
            record = json.loads(row["record_json"])
            record.pop("operation_request_constraints")
            connection.execute(
                """UPDATE connector_domain_policies SET record_json=?
                   WHERE connector_id=? AND version=? AND tenant_id=?""",
                (
                    json.dumps(record),
                    "warehouse",
                    1,
                    "warehouse-tenant",
                ),
            )
        loaded = client.portal.call(
            services.connectors.get_policy,
            "warehouse",
            1,
            "warehouse-tenant",
        )
        assert loaded.operation_request_constraints == []

        openapi = client.get("/openapi.json").json()
        constraint_schema = openapi["components"]["schemas"]["ConnectorOperationRequestConstraint"]
        assert constraint_schema["additionalProperties"] is False
        assert {
            "operation_id",
            "allowed_body_fields",
            "fixed_path_values",
            "fixed_query_values",
            "fixed_body_values",
        }.issubset(constraint_schema["properties"])
