from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

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
    ConnectorParameterBinding,
    ConnectorRequestBody,
    ConnectorSchemaField,
    ConnectorService,
    ConnectorTenantBinding,
)
from agent_platform.lilies_platform_contract import (
    PLATFORM_CONTRACT_VERSION,
    public_contract_schema_digest,
)
from agent_platform.lilies_platform_api import _connector_policy_match
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from tests.test_runtime import ScriptedProvider


ZERO_DIGEST = "sha256:" + "0" * 64


def test_connector_policy_matches_business_resources_without_fuzzy_overreach() -> None:
    assert _connector_policy_match(
        "warehouse",
        "records_list",
        ["warehouse.records"],
    )
    assert _connector_policy_match(
        "warehouse",
        "order_po_line_list",
        ["warehouse.purchase_order_lines"],
    )
    assert _connector_policy_match(
        "warehouse",
        "attachment_create",
        ["warehouse.purchase_order.attachments.create"],
    )
    assert not _connector_policy_match(
        "warehouse",
        "records_list",
        ["warehouse.inventory_items"],
    )
    assert not _connector_policy_match(
        "warehouse",
        "company_part_list",
        ["warehouse.companies"],
    )
    assert _connector_policy_match(
        "paperless",
        "documents_partial_update",
        ["paperless.document.custom_fields.update"],
    )
    assert _connector_policy_match(
        "inventree",
        "order_po_list",
        ["inventree.purchase_orders"],
    )


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
        headers={"Authorization": "Bearer internal-test-token"},
        json={
            "name": "Connector discovery",
            "description": "Task-scoped public connector discovery.",
            "requirement": "",
            "mode": "workflow",
            "delivery_mode": "guided",
            "governed_hard_gate": True,
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _credential_headers(
    client: TestClient,
    *,
    application_id: UUID,
    connector_access: bool,
) -> dict[str, str]:
    assignment_id = uuid4()
    session_id = uuid4()
    connector_policy = (
        {
            "allowed_network_hosts": ["127.0.0.1"],
            "connector_access": True,
            "readable_host_objects": ["warehouse.records"],
            "writable_host_operations": ["warehouse.records.update"],
            "permission_required_actions": ["warehouse.records.update"],
            "compensation_actions": ["warehouse.records.restore"],
            "max_write_count": 18,
            "max_payload_bytes": 4 * 1024 * 1024,
        }
        if connector_access
        else {}
    )
    issued = client.portal.call(
        client.app.state.services.platform_blackbox_auth.issue_credential,
        TaskCredentialGrant(
            assignment_id=assignment_id,
            session_id=session_id,
            scopes=[
                PlatformBlackboxScope.catalog_read,
                *(
                    [PlatformBlackboxScope.run_execute]
                    if connector_access
                    else []
                ),
            ],
            application_ids=[application_id],
            allowed_actions_digest=(
                "sha256:" + "2" * 64 if connector_access else None
            ),
            budget_digest=(
                "sha256:" + "3" * 64 if connector_access else None
            ),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            **connector_policy,
        ),
    )
    return {
        "Authorization": f"Bearer {issued.access_token.get_secret_value()}",
        "X-Lilies-Assignment-ID": str(assignment_id),
        "X-Lilies-Session-ID": str(session_id),
        "X-Lilies-Contract-Digest": ZERO_DIGEST,
    }


def _get(
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


def _post(
    client: TestClient,
    path: str,
    headers: dict[str, str],
    *,
    key: str,
    payload: dict[str, Any],
):
    return client.post(
        path,
        headers={
            **headers,
            "X-Lilies-Tool-Call-ID": f"tool-{key}",
            "X-Lilies-Idempotency-Key": key,
        },
        json={**payload, "idempotency_key": key},
    )


def _open_object(schema_id: str) -> ConnectorObjectSchema:
    return ConnectorObjectSchema(
        schema_id=schema_id,
        fields=[],
        additional_properties=True,
    )


def _mutation_operation(
    *,
    operation_id: str,
    kind: str,
    compensation_operation_id: str | None = None,
) -> ConnectorOperation:
    request_schema = ConnectorObjectSchema(
        schema_id=f"{operation_id}.request",
        fields=[
            ConnectorSchemaField(name="record_id", value_type="integer"),
            ConnectorSchemaField(name="body", value_type="object"),
        ],
    )
    return ConnectorOperation(
        id=operation_id,
        title=operation_id,
        kind=kind,
        method="PATCH",
        path="/v1/records/{record_id}",
        request_schema=request_schema,
        response_schema=_open_object(f"{operation_id}.response"),
        parameters=[
            ConnectorParameterBinding(
                input_key="record_id",
                wire_name="record_id",
                location="path",
                required=True,
            )
        ],
        request_body=ConnectorRequestBody(input_key="body", required=True),
        required_roles=["operator"],
        compensation_operation_id=compensation_operation_id,
    )


def _generic_manifest() -> ConnectorManifest:
    list_response: dict[str, Any] = {
        "oneOf": [
            {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            {"type": "object", "additionalProperties": True},
        ]
    }
    restore_id = "records.restore"
    operations = [
        ConnectorOperation(
            id="records.list",
            title="List records",
            kind="read",
            method="GET",
            path="/v1/records",
            request_schema=ConnectorObjectSchema(
                schema_id="records.list.request",
                fields=[
                    ConnectorSchemaField(
                        name="limit",
                        value_type="integer",
                        required=False,
                    )
                ],
            ),
            response_schema=_open_object("records.list.response"),
            response_json_schema=list_response,
            parameters=[
                ConnectorParameterBinding(
                    input_key="limit",
                    wire_name="limit",
                    location="query",
                )
            ],
            required_roles=["operator"],
        ),
        _mutation_operation(
            operation_id="records.update",
            kind="write",
            compensation_operation_id=restore_id,
        ),
        _mutation_operation(
            operation_id=restore_id,
            kind="compensate",
        ),
    ]
    return ConnectorManifest(
        connector_id="warehouse",
        version=1,
        title="Warehouse records",
        description="Neutral connector projection fixture.",
        domain="records",
        operations=operations,
        deployment_profiles=[
            ConnectorDeploymentProfile(
                id="private",
                environment="private",
                base_url="http://127.0.0.1:19080",
                auth_type="bearer",
                allowed_hosts=["127.0.0.1"],
                available=True,
                claim_ceiling="H3",
            )
        ],
        source_provenance={
            "kind": "openapi",
            "source_digest": "sha256:" + "1" * 64,
        },
        created_at="2026-07-26T00:00:00+00:00",
    )


async def _register_generic_connector(
    services: Any,
    *,
    application_id: str,
    request_key_writes: bool = False,
) -> None:
    manifest = _generic_manifest()
    if request_key_writes:
        manifest = manifest.model_copy(update={
            "operations": [
                operation.model_copy(update={
                    "idempotency_semantics": "request_key",
                })
                if operation.id == "records.update"
                else operation
                for operation in manifest.operations
            ],
        })
    await services.connectors.register_manifest(manifest)
    operation_ids = [item.id for item in manifest.operations]
    await services.connectors.upsert_binding(
        ConnectorTenantBinding(
            connector_id=manifest.connector_id,
            connector_version=manifest.version,
            tenant_id="test-tenant",
            external_tenant_id="external-test-tenant",
            profile_id="private",
            secret_ref="secret://test-tenant/warehouse-token",
            application_ids=[application_id],
            allowed_operations=operation_ids,
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
    await services.connectors.set_policy(
        ConnectorDomainPolicy(
            connector_id=manifest.connector_id,
            connector_version=manifest.version,
            tenant_id="test-tenant",
            domain=manifest.domain,
            allowed_profiles=["private"],
            allowed_operations=operation_ids,
            required_roles=["operator"],
            mutation_preauthorization_required=True,
        ),
        expected_revision=0,
    )


def test_generic_manifest_validates_declared_union_response_shape() -> None:
    manifest = _generic_manifest()
    assert PLATFORM_CONTRACT_VERSION == 4
    assert (
        public_contract_schema_digest()
        == "sha256:9b3edbd7b9039d3796c0c462f1a69efc7b94e5d516b7d00ea6a9f26134ab599d"
    )
    operation = manifest.operation("records.list")
    ConnectorService.validate_operation_response(
        operation,
        {"count": 1, "results": []},
    )
    ConnectorService.validate_operation_response(
        operation,
        [{"id": 1}],
    )
    with pytest.raises(ValueError, match="exactly one"):
        ConnectorService.validate_operation_response(
            operation,
            "not-a-list-response",
        )


def test_public_tool_catalog_projects_only_task_scoped_redacted_connectors(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _application(client)
        headers = _credential_headers(
            client,
            application_id=application_id,
            connector_access=True,
        )
        before = _get(
            client,
            "/api/v1/lilies/platform-contract",
            headers,
            key="connector-contract-before",
        )
        assert before.status_code == 200, before.text
        before_contract = before.json()["data"]

        client.portal.call(
            partial(
                _register_generic_connector,
                client.app.state.services,
                application_id=str(application_id),
            )
        )

        after = _get(
            client,
            "/api/v1/lilies/platform-contract",
            headers,
            key="connector-contract-after",
        )
        assert after.status_code == 200, after.text
        after_contract = after.json()["data"]
        assert after_contract["contract_version"] == PLATFORM_CONTRACT_VERSION
        assert after_contract["tool_catalog_digest"] != before_contract["tool_catalog_digest"]
        assert after_contract["contract_digest"] != before_contract["contract_digest"]

        headers["X-Lilies-Contract-Digest"] = after_contract["contract_digest"]
        catalog = _get(
            client,
            "/api/v1/lilies/tools",
            headers,
            key="connector-catalog",
        )
        assert catalog.status_code == 200, catalog.text
        connectors = [
            item
            for item in catalog.json()["data"]
            if str(item.get("name", "")).startswith("connector:")
        ]
        assert len(connectors) == 3
        assert all(
            set(item)
            == {
                "name",
                "type",
                "published",
                "description",
                "input_schema",
                "output_schema",
            }
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["execution_context"][
                "application_ids"
            ]
            == [str(application_id)]
            for item in connectors
        )
        assert all(item["type"] == "core" for item in connectors)
        assert all(
            item["input_schema"]["x-lilies-connector"]["available"] is True
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["execution_modes"]
            == ["dry_run", "execute"]
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["retryable_status_codes"]
            == [408, 425, 429, 500, 502, 503, 504]
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["idempotency_semantics"]
            == "none"
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["max_attempts"] == 3
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["authorization_required"]
            is (
                item["input_schema"]["x-lilies-connector"]["operation_kind"]
                != "read"
            )
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["authorization_modes"]
            == (
                ["explicit", "runtime_exact"]
                if item["input_schema"]["x-lilies-connector"][
                    "authorization_required"
                ]
                else ["explicit"]
            )
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["binding_revision"] == 1
            and item["input_schema"]["x-lilies-connector"]["policy_revision"] == 1
            for item in connectors
        )
        output_schemas = {
            item["input_schema"]["x-lilies-connector"]["operation_id"]: item[
                "output_schema"
            ]
            for item in connectors
        }
        assert {
            item["type"]
            for item in output_schemas["records.list"]["oneOf"]
        } == {"array", "object"}
        serialized = json.dumps(connectors, sort_keys=True)
        for forbidden in (
            "secret://",
            "base_url",
            "external_tenant_id",
            "source_provenance",
            ":19080",
        ):
            assert forbidden not in serialized

        disabled_headers = _credential_headers(
            client,
            application_id=application_id,
            connector_access=False,
        )
        disabled_contract = _get(
            client,
            "/api/v1/lilies/platform-contract",
            disabled_headers,
            key="connector-disabled-contract",
        )
        assert disabled_contract.status_code == 200, disabled_contract.text
        disabled_headers["X-Lilies-Contract-Digest"] = disabled_contract.json()[
            "data"
        ]["contract_digest"]
        disabled_catalog = _get(
            client,
            "/api/v1/lilies/tools",
            disabled_headers,
            key="connector-disabled-catalog",
        )
        assert disabled_catalog.status_code == 200, disabled_catalog.text
        assert not [
            item
            for item in disabled_catalog.json()["data"]
            if str(item.get("name", "")).startswith("connector:")
        ]


def _connector_contract(
    client: TestClient,
    headers: dict[str, str],
    *,
    operation_id: str,
    key: str,
) -> dict[str, Any]:
    contract_response = _get(
        client,
        "/api/v1/lilies/platform-contract",
        headers,
        key=f"{key}-contract",
    )
    assert contract_response.status_code == 200, contract_response.text
    contract = contract_response.json()["data"]
    headers["X-Lilies-Contract-Digest"] = contract["contract_digest"]
    assert "platform_connector_authorization_issue" in {
        item["name"] for item in contract["operations"]
    }
    catalog = _get(
        client,
        "/api/v1/lilies/tools",
        headers,
        key=f"{key}-tools",
    )
    assert catalog.status_code == 200, catalog.text
    return next(
        item["input_schema"]["x-lilies-connector"]
        for item in catalog.json()["data"]
        if item["name"] == f"connector:warehouse:1:{operation_id}"
    )


def _authorization_request(
    *,
    application_id: UUID | str,
    contract: dict[str, Any],
    payload: dict[str, Any],
    expires_in_seconds: int = 300,
) -> dict[str, Any]:
    context = contract["execution_context"]
    return {
        "application_id": str(application_id),
        "connector_id": contract["connector_id"],
        "connector_version": contract["connector_version"],
        "tenant_id": context["tenant_id"],
        "actor_id": context["actor_id"],
        "profile_id": context["profile_id"],
        "operation_id": contract["operation_id"],
        "operation_kind": contract["operation_kind"],
        "descriptor_digest": contract["descriptor_digest"],
        "payload": payload,
        "expires_in_seconds": expires_in_seconds,
    }


def _execution_request(
    *,
    application_id: UUID | str,
    headers: dict[str, str],
    operation_id: str,
    payload: dict[str, Any],
    authorization_id: str,
    idempotency_key: str,
    session_id: str | None = None,
    max_write_count: int = 18,
) -> ConnectorExecutionRequest:
    return ConnectorExecutionRequest(
        connector_id="warehouse",
        connector_version=1,
        tenant_id="test-tenant",
        actor_id="builder",
        actor_roles=["operator"],
        profile_id="private",
        operation_id=operation_id,
        payload=payload,
        idempotency_key=idempotency_key,
        authorization_id=authorization_id,
        application_id=str(application_id),
        assignment_id=headers["X-Lilies-Assignment-ID"],
        session_id=session_id or headers["X-Lilies-Session-ID"],
        allowed_network_hosts=["127.0.0.1"],
        allowed_compensation_operations=["warehouse.records.restore"],
        permission_required=True,
        assignment_max_write_count=max_write_count,
        assignment_max_payload_bytes=4 * 1024 * 1024,
    )


def test_public_connector_authorization_is_exact_audited_and_single_use(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _application(client)
        headers = _credential_headers(
            client,
            application_id=application_id,
            connector_access=True,
        )
        client.portal.call(
            partial(
                _register_generic_connector,
                client.app.state.services,
                application_id=str(application_id),
            )
        )
        contract = _connector_contract(
            client,
            headers,
            operation_id="records.update",
            key="authorization-exact",
        )
        payload = {"record_id": 7, "body": {"status": "matched"}}
        request_payload = _authorization_request(
            application_id=application_id,
            contract=contract,
            payload=payload,
        )
        path = (
            f"/api/v1/lilies/applications/{application_id}/"
            "connector-authorizations"
        )
        response = _post(
            client,
            path,
            headers,
            key="authorization-exact-issue",
            payload=request_payload,
        )
        assert response.status_code == 201, response.text
        receipt = response.json()["data"]
        assert receipt["payload_hash"] == (
            "sha256:" + ConnectorService.payload_hash(payload)
        )
        assert receipt["descriptor_digest"] == contract["descriptor_digest"]
        assert receipt["assignment_id"] == headers["X-Lilies-Assignment-ID"]
        assert receipt["session_id"] == headers["X-Lilies-Session-ID"]
        assert receipt["application_id"] == str(application_id)
        assert receipt["operation_kind"] == "write"
        assert receipt["max_uses"] == 1
        assert receipt["assignment_max_write_count"] == 18
        assert receipt["assignment_write_count_at_issue"] == 0
        assert receipt["expires_at"] <= receipt["task_deadline_at"]
        assert receipt["receipt_digest"].startswith("sha256:")

        replay = _post(
            client,
            path,
            headers,
            key="authorization-exact-issue",
            payload=request_payload,
        )
        assert replay.status_code == 201, replay.text
        assert replay.headers["X-Lilies-Idempotent-Replay"] == "true"
        assert replay.json()["data"] == receipt

        service = client.app.state.services.connectors
        service._call_adapter = AsyncMock(return_value={})
        wrong_payload = _execution_request(
            application_id=application_id,
            headers=headers,
            operation_id="records.update",
            payload={"record_id": 7, "body": {"status": "drifted"}},
            authorization_id=receipt["authorization_id"],
            idempotency_key="authorization-wrong-payload",
        )
        with pytest.raises(ConnectorDenied, match="scope does not match"):
            client.portal.call(service.execute, wrong_payload)

        wrong_session = _execution_request(
            application_id=application_id,
            headers=headers,
            operation_id="records.update",
            payload=payload,
            authorization_id=receipt["authorization_id"],
            idempotency_key="authorization-wrong-session",
            session_id=str(uuid4()),
        )
        with pytest.raises(ConnectorDenied, match="assignment scope"):
            client.portal.call(service.execute, wrong_session)

        wrong_budget = _execution_request(
            application_id=application_id,
            headers=headers,
            operation_id="records.update",
            payload=payload,
            authorization_id=receipt["authorization_id"],
            idempotency_key="authorization-wrong-budget",
            max_write_count=17,
        )
        with pytest.raises(ConnectorDenied, match="budget scope"):
            client.portal.call(service.execute, wrong_budget)

        exact = _execution_request(
            application_id=application_id,
            headers=headers,
            operation_id="records.update",
            payload=payload,
            authorization_id=receipt["authorization_id"],
            idempotency_key="authorization-exact-execute",
        )
        executed = client.portal.call(service.execute, exact)
        assert executed.status == "succeeded"
        assert executed.authorization_id == receipt["authorization_id"]
        assert service._call_adapter.await_count == 1

        second_use = exact.model_copy(
            update={"idempotency_key": "authorization-second-use"}
        )
        with pytest.raises(ConnectorDenied, match="revoked or exhausted"):
            client.portal.call(service.execute, second_use)
        budget = client.portal.call(
            service.export_assignment_budget,
            headers["X-Lilies-Assignment-ID"],
        )
        assert budget.write_count == 1
        assert len(budget.writes) == 1
        assert budget.writes[0].authorization_ref_digest is not None
        audit = client.portal.call(
            partial(
                client.app.state.services.platform_blackbox_auth.list_audit,
                assignment_id=UUID(headers["X-Lilies-Assignment-ID"]),
            )
        )
        issue_audit = [
            item
            for item in audit
            if item.operation.value
            == "platform_connector_authorization_issue"
        ]
        assert [item.event_type.value for item in issue_audit] == [
            "request.authorized",
            "request.completed",
            "request.replayed",
        ]

        internal = client.post(
            "/api/v1/connectors/authorizations",
            headers=headers,
            json={},
        )
        assert internal.status_code == 403
        assert internal.json()["error"]["code"] == "internal_endpoint_denied"


def test_public_connector_authorization_rejects_scope_and_descriptor_drift(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _application(client)
        other_application_id = _application(client)
        headers = _credential_headers(
            client,
            application_id=application_id,
            connector_access=True,
        )
        services = client.app.state.services
        client.portal.call(
            partial(
                _register_generic_connector,
                services,
                application_id=str(application_id),
            )
        )
        contract = _connector_contract(
            client,
            headers,
            operation_id="records.update",
            key="authorization-drift-initial",
        )
        payload = {"record_id": 8, "body": {"status": "review"}}
        request_payload = _authorization_request(
            application_id=application_id,
            contract=contract,
            payload=payload,
        )
        path = (
            f"/api/v1/lilies/applications/{application_id}/"
            "connector-authorizations"
        )

        wrong_session_headers = {
            **headers,
            "X-Lilies-Session-ID": str(uuid4()),
        }
        wrong_session = _post(
            client,
            path,
            wrong_session_headers,
            key="authorization-wrong-session-public",
            payload=request_payload,
        )
        assert wrong_session.status_code == 403
        assert wrong_session.json()["error"]["code"] == "authorization_denied"

        wrong_operation_payload = {
            **request_payload,
            "operation_id": "records.unknown",
        }
        wrong_operation = _post(
            client,
            path,
            headers,
            key="authorization-wrong-operation-public",
            payload=wrong_operation_payload,
        )
        assert wrong_operation.status_code == 403
        assert (
            wrong_operation.json()["error"]["code"]
            == "connector_authorization_denied"
        )

        wrong_app_payload = {
            **request_payload,
            "application_id": str(other_application_id),
        }
        wrong_app = _post(
            client,
            (
                f"/api/v1/lilies/applications/{other_application_id}/"
                "connector-authorizations"
            ),
            headers,
            key="authorization-wrong-app-public",
            payload=wrong_app_payload,
        )
        assert wrong_app.status_code == 404

        policy = client.portal.call(
            services.connectors.get_policy,
            "warehouse",
            1,
            "test-tenant",
        )
        client.portal.call(
            partial(
                services.connectors.set_policy,
                policy.model_copy(
                    update={"emergency_reason": "revision-only policy drift"}
                ),
                expected_revision=policy.revision,
            )
        )
        current_contract = _get(
            client,
            "/api/v1/lilies/platform-contract",
            headers,
            key="authorization-policy-drift-contract",
        )
        assert current_contract.status_code == 200
        headers["X-Lilies-Contract-Digest"] = current_contract.json()["data"][
            "contract_digest"
        ]
        stale_policy = _post(
            client,
            path,
            headers,
            key="authorization-stale-policy-descriptor",
            payload=request_payload,
        )
        assert stale_policy.status_code == 409
        assert (
            stale_policy.json()["error"]["code"]
            == "connector_descriptor_drift"
        )

        refreshed = _connector_contract(
            client,
            headers,
            operation_id="records.update",
            key="authorization-drift-refreshed",
        )
        refreshed_payload = _authorization_request(
            application_id=application_id,
            contract=refreshed,
            payload=payload,
        )
        binding = (
            client.portal.call(
                partial(
                    services.connectors.list_bindings,
                    application_id=str(application_id),
                )
            )
        )[0]
        client.portal.call(
            partial(
                services.connectors.upsert_binding,
                binding,
                expected_revision=binding.revision,
            )
        )
        binding_contract = _get(
            client,
            "/api/v1/lilies/platform-contract",
            headers,
            key="authorization-binding-drift-contract",
        )
        assert binding_contract.status_code == 200
        headers["X-Lilies-Contract-Digest"] = binding_contract.json()["data"][
            "contract_digest"
        ]
        stale_binding = _post(
            client,
            path,
            headers,
            key="authorization-stale-binding-descriptor",
            payload=refreshed_payload,
        )
        assert stale_binding.status_code == 409
        assert (
            stale_binding.json()["error"]["code"]
            == "connector_descriptor_drift"
        )


def test_public_connector_authorization_expiry_fails_closed(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _application(client)
        headers = _credential_headers(
            client,
            application_id=application_id,
            connector_access=True,
        )
        services = client.app.state.services
        client.portal.call(
            partial(
                _register_generic_connector,
                services,
                application_id=str(application_id),
            )
        )
        contract = _connector_contract(
            client,
            headers,
            operation_id="records.update",
            key="authorization-expiry",
        )
        payload = {"record_id": 9, "body": {"status": "approve"}}
        issued = _post(
            client,
            (
                f"/api/v1/lilies/applications/{application_id}/"
                "connector-authorizations"
            ),
            headers,
            key="authorization-expiry-issue",
            payload=_authorization_request(
                application_id=application_id,
                contract=contract,
                payload=payload,
                expires_in_seconds=1,
            ),
        )
        assert issued.status_code == 201, issued.text
        time.sleep(1.1)
        request = _execution_request(
            application_id=application_id,
            headers=headers,
            operation_id="records.update",
            payload=payload,
            authorization_id=issued.json()["data"]["authorization_id"],
            idempotency_key="authorization-expired-execute",
        )
        with pytest.raises(ConnectorDenied, match="expired"):
            client.portal.call(services.connectors.execute, request)
