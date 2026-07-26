from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.connector_sdk import ConnectorService
from agent_platform.lilies_platform_contract import (
    PLATFORM_CONTRACT_VERSION,
    public_contract_schema_digest,
)
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from agent_platform.standard_connector_catalog import (
    standard_connector_manifests,
)
from scripts.run_v04_13_codex_builder import _provision_standard_connectors
from tests.test_runtime import ScriptedProvider


ZERO_DIGEST = "sha256:" + "0" * 64


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
            "readable_host_objects": [
                "inventree.companies",
                "inventree.parts",
                "inventree.purchase_order_lines",
                "inventree.purchase_orders",
                "paperless.custom_fields",
                "paperless.documents",
                "paperless.tasks",
            ],
            "writable_host_operations": [
                "inventree.purchase_order.metadata.update",
                "paperless.document.custom_fields.update",
                "paperless.document.tags.update",
            ],
            "permission_required_actions": [
                "inventree.purchase_order.metadata.update",
                "paperless.document.custom_fields.update",
                "paperless.document.tags.update",
            ],
            "compensation_actions": [
                "inventree.purchase_order.metadata.restore",
                "paperless.document.custom_fields.restore",
                "paperless.document.tags.restore",
            ],
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
            scopes=[PlatformBlackboxScope.catalog_read],
            application_ids=[application_id],
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


def test_standard_presets_validate_live_list_response_shapes_and_expose_binary_gap() -> None:
    manifests = list(
        standard_connector_manifests(
            paperless_base_url="http://127.0.0.1:18010",
            inventree_base_url="http://127.0.0.1:18011",
        )
    )

    assert PLATFORM_CONTRACT_VERSION == 1
    assert (
        public_contract_schema_digest()
        == "sha256:578e399402c7d4e589a60ed83f021c1ce3c0d2143c2754f8fdf0c21a850cdece"
    )
    assert [(item.connector_id, item.version) for item in manifests] == [
        ("paperless", 1),
        ("inventree", 1),
    ]
    operations = {operation.id for manifest in manifests for operation in manifest.operations}
    assert {
        "paperless.documents",
        "paperless.document.custom_fields.update",
        "paperless.document.custom_fields.restore",
        "inventree.purchase_orders",
        "inventree.purchase_order.metadata.update",
        "inventree.purchase_order.metadata.restore",
    } <= operations
    assert "inventree.purchase_order.attachment.create" not in operations
    assert "inventree.purchase_order.attachment.delete" not in operations
    assert all(
        "multipart or binary upload" in manifest.deployment_profiles[0].excluded_claims
        for manifest in manifests
    )
    by_operation = {
        operation.id: operation for manifest in manifests for operation in manifest.operations
    }
    ConnectorService.validate_operation_response(
        by_operation["paperless.documents"],
        {"count": 1, "results": []},
    )
    ConnectorService.validate_operation_response(
        by_operation["paperless.tasks"],
        [{"id": 1}],
    )
    with pytest.raises(ValueError, match="must be array"):
        ConnectorService.validate_operation_response(
            by_operation["paperless.tasks"],
            {"results": []},
        )
    for operation_id in (
        "inventree.companies",
        "inventree.parts",
        "inventree.purchase_orders",
        "inventree.purchase_order_lines",
    ):
        ConnectorService.validate_operation_response(
            by_operation[operation_id],
            [{"pk": 1}],
        )
        ConnectorService.validate_operation_response(
            by_operation[operation_id],
            {"count": 1, "results": [{"pk": 1}]},
        )
        with pytest.raises(ValueError, match="exactly one"):
            ConnectorService.validate_operation_response(
                by_operation[operation_id],
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

        first = client.portal.call(
            partial(
                _provision_standard_connectors,
                client.app.state.services,
                application_id=str(application_id),
            )
        )
        second = client.portal.call(
            partial(
                _provision_standard_connectors,
                client.app.state.services,
                application_id=str(application_id),
            )
        )
        assert first == second
        assert [item["connector_id"] for item in first] == [
            "inventree",
            "paperless",
        ]

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
        assert len(connectors) == 13
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
            item["input_schema"]["x-lilies-connector"]["execution_context"]["application_ids"]
            == [str(application_id)]
            for item in connectors
        )
        assert all(item["type"] == "core" for item in connectors)
        assert all(
            item["input_schema"]["x-lilies-connector"]["available"] is True for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["execution_modes"] == ["dry_run", "execute"]
            for item in connectors
        )
        assert all(
            item["input_schema"]["x-lilies-connector"]["authorization_required"]
            is (item["input_schema"]["x-lilies-connector"]["operation_kind"] != "read")
            for item in connectors
        )
        output_schemas = {
            item["input_schema"]["x-lilies-connector"]["operation_id"]: item["output_schema"]
            for item in connectors
        }
        assert output_schemas["paperless.tasks"]["type"] == "array"
        assert {item["type"] for item in output_schemas["inventree.purchase_orders"]["oneOf"]} == {
            "array",
            "object",
        }
        serialized = json.dumps(connectors, sort_keys=True)
        for forbidden in (
            "secret://",
            "base_url",
            "external_tenant_id",
            "source_provenance",
            ":18010",
            ":18011",
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
        disabled_headers["X-Lilies-Contract-Digest"] = disabled_contract.json()["data"][
            "contract_digest"
        ]
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
