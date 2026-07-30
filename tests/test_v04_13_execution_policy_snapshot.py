from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_platform.api import create_app
from agent_platform.connector_sdk import ConnectorAdapterError
from agent_platform.execution_policy import (
    ExecutionPolicyExpansionDenied,
    ExecutionPolicySnapshot,
)
from agent_platform.workflow_models import WorkflowRunRequest
from tests.test_runtime import ScriptedProvider
from tests.test_v04_13_lilies_platform_api import (
    ZERO_DIGEST,
    _issue,
    _request,
    _settings,
)
from tests.test_v04_13_lilies_platform_runtime_isolation import (
    _apply_operations,
    _create_internal_application,
    _linear_operations,
)
from tests.test_v04_13_builder_connector_public_api import (
    _authorization_request,
    _connector_contract,
    _post,
    _register_generic_connector,
)


def _policy(tmp_path: Path) -> ExecutionPolicySnapshot:
    return ExecutionPolicySnapshot.build(
        workspace_boundary=str(tmp_path / "assignment-workspace"),
        assignment_id=uuid4(),
        session_id=uuid4(),
        allowed_nested_application_ids=[str(uuid4()), str(uuid4())],
        allowed_runtime_tools=["Read", "Write"],
        allowed_network_hosts=["API.EXAMPLE.TEST", "api.example.test."],
        model_access=True,
        allowed_connector_operations=[
            "neutral.records.fetch",
            "neutral.records.update",
            "neutral.records.rollback",
        ],
        writable_connector_operations=[
            "neutral.records.update",
            "neutral.records.archive",
        ],
        permission_required_connector_operations=["neutral.records.update"],
        compensation_connector_operations=["neutral.records.rollback"],
        max_connector_write_count=9,
        max_connector_payload_bytes=4_096,
        governed_host_actions=True,
    )


def test_execution_policy_snapshot_is_canonical_digest_bound_and_redacted(
    tmp_path: Path,
) -> None:
    snapshot = _policy(tmp_path)
    restored = ExecutionPolicySnapshot.model_validate_json(
        snapshot.model_dump_json()
    )

    assert restored == snapshot
    assert snapshot.allowed_network_hosts == ("api.example.test",)
    assert snapshot.allowed_runtime_tools == ("Read", "Write")
    public = snapshot.public_projection()
    assert "workspace_boundary" not in public
    assert public["workspace_scope"] == {
        "kind": "assignment_session",
        "digest": snapshot.workspace_scope_digest,
    }
    assert public["policy_digest"] == snapshot.policy_digest

    tampered = snapshot.model_dump(mode="json")
    tampered["allowed_network_hosts"].append("attacker.invalid")
    with pytest.raises(ValidationError, match="execution policy digest mismatch"):
        ExecutionPolicySnapshot.model_validate(tampered)


def test_published_policy_can_only_be_narrowed(tmp_path: Path) -> None:
    snapshot = _policy(tmp_path)
    child = Path(snapshot.workspace_boundary) / "run-1"
    child.mkdir(parents=True)
    narrowed = snapshot.constrained_by(
        workspace_boundary=str(child),
        assignment_id=str(snapshot.assignment_id),
        session_id=str(snapshot.session_id),
        allowed_nested_application_ids=[snapshot.allowed_nested_application_ids[0]],
        allowed_runtime_tools=["Read", "UnassignedTool"],
        allowed_network_hosts=["api.example.test", "attacker.invalid"],
        model_access=False,
        allowed_connector_operations=[
            "neutral.records.fetch",
            "neutral.records.update",
            "unassigned.operation",
        ],
        writable_connector_operations=["neutral.records.update"],
        permission_required_connector_operations=["neutral.records.update"],
        compensation_connector_operations=[],
        max_connector_write_count=2,
        max_connector_payload_bytes=512,
        governed_host_actions=False,
    )

    assert narrowed.workspace_boundary == str(child.resolve())
    assert narrowed.allowed_nested_application_ids == (
        snapshot.allowed_nested_application_ids[0],
    )
    assert narrowed.allowed_runtime_tools == ("Read",)
    assert narrowed.allowed_network_hosts == ("api.example.test",)
    assert narrowed.model_access is False
    assert narrowed.allowed_connector_operations == (
        "neutral.records.fetch",
        "neutral.records.update",
    )
    assert narrowed.writable_connector_operations == (
        "neutral.records.update",
    )
    assert narrowed.permission_required_connector_operations == (
        "neutral.records.update",
    )
    assert narrowed.compensation_connector_operations == ()
    assert narrowed.max_connector_write_count == 2
    assert narrowed.max_connector_payload_bytes == 512
    assert narrowed.governed_host_actions is True
    assert narrowed.policy_digest != snapshot.policy_digest

    attempted_reexpand = narrowed.constrained_by(
        workspace_boundary=None,
        assignment_id=None,
        session_id=None,
        allowed_nested_application_ids=None,
        allowed_runtime_tools=["Read", "Write"],
        allowed_network_hosts=["api.example.test", "attacker.invalid"],
        model_access=True,
        allowed_connector_operations=[
            "neutral.records.fetch",
            "neutral.records.update",
            "neutral.records.rollback",
        ],
        writable_connector_operations=[
            "neutral.records.update",
            "neutral.records.archive",
        ],
        permission_required_connector_operations=[],
        compensation_connector_operations=["neutral.records.rollback"],
        max_connector_write_count=999,
        max_connector_payload_bytes=999_999,
        governed_host_actions=False,
    )
    assert attempted_reexpand.allowed_runtime_tools == ("Read",)
    assert attempted_reexpand.allowed_network_hosts == ("api.example.test",)
    assert attempted_reexpand.model_access is False
    assert attempted_reexpand.allowed_connector_operations == (
        "neutral.records.fetch",
        "neutral.records.update",
    )
    assert attempted_reexpand.max_connector_write_count == 2
    assert attempted_reexpand.max_connector_payload_bytes == 512

    with pytest.raises(
        ExecutionPolicyExpansionDenied,
        match="caller assignment differs",
    ):
        snapshot.constrained_by(
            workspace_boundary=None,
            assignment_id=str(uuid4()),
            session_id=None,
            allowed_nested_application_ids=None,
            allowed_runtime_tools=None,
            allowed_network_hosts=None,
            model_access=None,
            allowed_connector_operations=None,
            writable_connector_operations=None,
            permission_required_connector_operations=None,
            compensation_connector_operations=None,
            max_connector_write_count=None,
            max_connector_payload_bytes=None,
            governed_host_actions=False,
        )

    with pytest.raises(
        ExecutionPolicyExpansionDenied,
        match="caller session differs",
    ):
        snapshot.constrained_by(
            workspace_boundary=None,
            assignment_id=None,
            session_id=str(uuid4()),
            allowed_nested_application_ids=None,
            allowed_runtime_tools=None,
            allowed_network_hosts=None,
            model_access=None,
            allowed_connector_operations=None,
            writable_connector_operations=None,
            permission_required_connector_operations=None,
            compensation_connector_operations=None,
            max_connector_write_count=None,
            max_connector_payload_bytes=None,
            governed_host_actions=False,
        )

    with pytest.raises(
        ExecutionPolicyExpansionDenied,
        match="caller workspace exceeds",
    ):
        snapshot.constrained_by(
            workspace_boundary=str(tmp_path / "other-workspace"),
            assignment_id=None,
            session_id=None,
            allowed_nested_application_ids=None,
            allowed_runtime_tools=None,
            allowed_network_hosts=None,
            model_access=None,
            allowed_connector_operations=None,
            writable_connector_operations=None,
            permission_required_connector_operations=None,
            compensation_connector_operations=None,
            max_connector_write_count=None,
            max_connector_payload_bytes=None,
            governed_host_actions=False,
        )


def test_published_policy_can_rebind_trusted_continuation_authority(
    tmp_path: Path,
) -> None:
    snapshot = _policy(tmp_path)
    continuation_workspace = tmp_path / "continuation-workspace"
    continuation_assignment = uuid4()
    continuation_session = uuid4()

    rebound = snapshot.constrained_by(
        workspace_boundary=str(continuation_workspace),
        assignment_id=str(continuation_assignment),
        session_id=str(continuation_session),
        allowed_nested_application_ids=[
            snapshot.allowed_nested_application_ids[0],
        ],
        allowed_runtime_tools=["Read", "UnassignedTool"],
        allowed_network_hosts=["api.example.test", "attacker.invalid"],
        model_access=False,
        allowed_connector_operations=[
            "neutral.records.fetch",
            "unassigned.operation",
        ],
        writable_connector_operations=["neutral.records.update"],
        permission_required_connector_operations=["neutral.records.update"],
        compensation_connector_operations=["neutral.records.rollback"],
        max_connector_write_count=2,
        max_connector_payload_bytes=512,
        governed_host_actions=False,
        allow_authority_rebind=True,
    )

    assert rebound.assignment_id == continuation_assignment
    assert rebound.session_id == continuation_session
    assert rebound.workspace_boundary == str(continuation_workspace.resolve())
    assert rebound.allowed_nested_application_ids == (
        snapshot.allowed_nested_application_ids[0],
    )
    assert rebound.allowed_runtime_tools == ("Read",)
    assert rebound.allowed_network_hosts == ("api.example.test",)
    assert rebound.model_access is False
    assert rebound.allowed_connector_operations == ("neutral.records.fetch",)
    assert rebound.writable_connector_operations == ()
    assert rebound.permission_required_connector_operations == ()
    assert rebound.compensation_connector_operations == ()
    assert rebound.max_connector_write_count == 2
    assert rebound.max_connector_payload_bytes == 512
    assert rebound.governed_host_actions is True
    assert rebound.policy_digest != snapshot.policy_digest


def test_public_connector_publish_persists_and_restores_execution_policy(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            "Immutable connector execution policy",
        )
        headers, _, assignment_id, session_id, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
            grant_policy={
                "allowed_actions_digest": ZERO_DIGEST,
                "budget_digest": ZERO_DIGEST,
                "connector_access": True,
                "allowed_network_hosts": ["neutral.connector.invalid"],
                "readable_host_objects": ["neutral_records.records.fetch"],
                "writable_host_operations": ["neutral_records.records.update"],
                "permission_required_actions": [
                    "neutral_records.records.update"
                ],
                "compensation_actions": ["neutral_records.records.rollback"],
                "max_write_count": 7,
                "max_payload_bytes": 2_048,
            },
        )
        _apply_operations(
            client,
            application_id,
            _linear_operations(
                {
                    "id": "middle",
                    "type": "connector_action",
                    "title": "Fetch neutral record",
                    "config": {
                        "connector_id": "neutral_records",
                        "connector_version": 1,
                        "operation_id": "records.fetch",
                        "tenant_id": "tenant-neutral",
                        "actor_id": "actor-neutral",
                        "actor_roles": ["reader"],
                        "profile_id": "profile-neutral",
                        "payload": {"record_id": "record-1"},
                        "idempotency_key": "neutral-fetch-published-0001",
                        "execution_mode": "dry_run",
                    },
                },
                tests=1,
            ),
        )
        services = client.app.state.services
        draft = client.portal.call(
            services.workflow_store.get_draft,
            application_id,
        )
        client.portal.call(
            services.workflow_store.mark_tested,
            application_id,
            draft["revision"],
            draft["content_hash"],
            {
                "passed": True,
                "validation": {"content_hash": draft["content_hash"]},
            },
        )

        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="publish-connector-execution-policy-0001",
            json={"acknowledge_warnings": False},
        )
        assert response.status_code == 200, response.text
        published_data = response.json()["data"]
        public_policy = published_data["publication_decision"][
            "execution_policy_snapshot"
        ]
        assert "workspace_boundary" not in public_policy
        assert public_policy["assignment_id"] == str(assignment_id)
        assert public_policy["session_id"] == str(session_id)
        assert public_policy["allowed_network_hosts"] == [
            "neutral.connector.invalid"
        ]
        assert public_policy["allowed_connector_operations"] == [
            "neutral_records.records.fetch",
            "neutral_records.records.rollback",
            "neutral_records.records.update",
        ]
        assert public_policy["max_connector_write_count"] == 7
        assert public_policy["max_connector_payload_bytes"] == 2_048
        assert "[REDACTED_PATH]" not in json.dumps(public_policy)

        version = client.portal.call(
            services.workflow_store.get_version,
            application_id,
            published_data["version"],
        )
        stored_policy = ExecutionPolicySnapshot.model_validate(
            version["publication_decision"]["execution_policy_snapshot"]
        )
        assert stored_policy.policy_digest == public_policy["policy_digest"]

        operation = MagicMock()
        operation.kind = "read"
        operation.mutating = False
        manifest = MagicMock()
        manifest.operation.return_value = operation
        execution = MagicMock()
        execution.id = "restored-policy-read-0001"
        execution.operation_id = "records.fetch"
        execution.status = "succeeded"
        execution.replayed = False
        execution.response = {"record_id": "record-1"}
        execution.public_receipt.return_value = {
            "execution_id": execution.id,
            "status": execution.status,
        }
        services.workflow_runtime.connector_service.get_manifest = AsyncMock(
            return_value=manifest
        )
        services.workflow_runtime.connector_service.execute = AsyncMock(
            return_value=execution
        )

        # The task bearer has expired, so it cannot contribute any ephemeral
        # caller authority. A non-mutating published read still runs under the
        # immutable version snapshot; mutating actions remain subject to their
        # separate live one-use authorization checks.
        services.platform_blackbox_auth._clock = lambda: (
            datetime.now(timezone.utc) + timedelta(hours=2)
        )
        expired = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key="expired-caller-cannot-run-0001",
            json={"inputs": {}, "version": published_data["version"]},
        )
        assert expired.status_code == 401, expired.text
        assert expired.json()["error"]["code"] == "credential_expired"

        async def execute_without_task_caller() -> dict:
            run_result = await services.workflow_runtime.create_run(
                application_id,
                WorkflowRunRequest(
                    inputs={"name": "Ada"},
                    version=published_data["version"],
                ),
            )
            await services.workflow_runtime.active_tasks[run_result["run_id"]]
            return await services.workflow_store.get_run(run_result["run_id"])

        run = client.portal.call(execute_without_task_caller)
        state = run["state"]
        assert run["status"] == "succeeded", run["error"]
        assert state.published_execution_policy_digest == stored_policy.policy_digest
        assert state.execution_policy_digest == stored_policy.policy_digest
        assert state.workspace_boundary == stored_policy.workspace_boundary
        assert state.allowed_network_hosts == ["neutral.connector.invalid"]
        assert state.allowed_connector_operations == [
            "neutral_records.records.fetch",
            "neutral_records.records.rollback",
            "neutral_records.records.update",
        ]
        assert state.assignment_id == str(assignment_id)
        assert state.session_id == str(session_id)
        assert state.governed_host_actions is True
        execution_request = (
            services.workflow_runtime.connector_service.execute.await_args.args[0]
        )
        assert execution_request.assignment_id == str(assignment_id)
        assert execution_request.session_id == str(session_id)
        assert execution_request.allowed_network_hosts == [
            "neutral.connector.invalid"
        ]


def test_published_connector_write_requires_live_one_use_authorization(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            "Published governed connector write",
        )
        headers, _, assignment_id, session_id, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
            grant_policy={
                "allowed_actions_digest": "sha256:" + "2" * 64,
                "budget_digest": "sha256:" + "3" * 64,
                "connector_access": True,
                "allowed_network_hosts": ["127.0.0.1"],
                "readable_host_objects": ["warehouse.records.list"],
                "writable_host_operations": ["warehouse.records.update"],
                "permission_required_actions": ["warehouse.records.update"],
                "compensation_actions": ["warehouse.records.restore"],
                "max_write_count": 18,
                "max_payload_bytes": 4 * 1024 * 1024,
            },
        )
        services = client.app.state.services
        client.portal.call(
            partial(
                _register_generic_connector,
                services,
                application_id=application_id,
            )
        )
        contract = _connector_contract(
            client,
            headers,
            operation_id="records.update",
            key="published-write",
        )
        payload = {"record_id": 7, "body": {"status": "matched"}}
        authorization_path = (
            f"/api/v1/lilies/applications/{application_id}/"
            "connector-authorizations"
        )
        live_authorization = _post(
            client,
            authorization_path,
            headers,
            key="published-write-live-authorization",
            payload=_authorization_request(
                application_id=application_id,
                contract=contract,
                payload=payload,
            ),
        )
        assert live_authorization.status_code == 201, live_authorization.text
        live_authorization_id = live_authorization.json()["data"][
            "authorization_id"
        ]

        def connector_config(
            *,
            authorization_id: str,
            idempotency_key: str,
        ) -> dict:
            return {
                "connector_id": "warehouse",
                "connector_version": 1,
                "operation_id": "records.update",
                "tenant_id": "test-tenant",
                "actor_id": "builder",
                "actor_roles": ["operator"],
                "profile_id": "private",
                "payload": payload,
                "idempotency_key": idempotency_key,
                "authorization_id": authorization_id,
                "execution_mode": "execute",
            }

        _apply_operations(
            client,
            application_id,
            _linear_operations(
                {
                    "id": "middle",
                    "type": "connector_action",
                    "title": "Update governed record",
                    "config": connector_config(
                        authorization_id=live_authorization_id,
                        idempotency_key="published-write-live-execution",
                    ),
                },
                tests=1,
            ),
        )

        def mark_current_tested() -> dict:
            draft = client.portal.call(
                services.workflow_store.get_draft,
                application_id,
            )
            client.portal.call(
                services.workflow_store.mark_tested,
                application_id,
                draft["revision"],
                draft["content_hash"],
                {
                    "passed": True,
                    "validation": {"content_hash": draft["content_hash"]},
                },
            )
            return draft

        mark_current_tested()
        version_one_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="published-write-version-one",
            json={"acknowledge_warnings": False},
        )
        assert version_one_response.status_code == 200, version_one_response.text
        version_one = version_one_response.json()["data"]
        _connector_contract(
            client,
            headers,
            operation_id="records.update",
            key="published-write-run-refresh",
        )
        services.connectors._call_adapter = AsyncMock(return_value={})

        async def terminal_run(run_id: str) -> dict:
            for _ in range(300):
                run = await services.workflow_store.get_run(run_id)
                if run["status"] in {
                    "succeeded",
                    "failed",
                    "cancelled",
                }:
                    return run
                await asyncio.sleep(0.01)
            raise AssertionError("published connector run did not become terminal")

        live_run_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key="published-write-live-run",
            json={
                "inputs": {"name": "Ada"},
                "version": version_one["version"],
            },
        )
        assert live_run_response.status_code == 202, live_run_response.text
        live_run = client.portal.call(
            terminal_run,
            live_run_response.json()["data"]["run_id"],
        )
        assert live_run["status"] == "succeeded", live_run["error"]
        assert live_run["state"].published_execution_policy_digest == (
            version_one["publication_decision"]["execution_policy_snapshot"][
                "policy_digest"
            ]
        )
        assert live_run["state"].assignment_id == str(assignment_id)
        assert live_run["state"].session_id == str(session_id)
        assert services.connectors._call_adapter.await_count == 1

        expiring_authorization = _post(
            client,
            authorization_path,
            headers,
            key="published-write-expiring-authorization",
            payload=_authorization_request(
                application_id=application_id,
                contract=contract,
                payload=payload,
                expires_in_seconds=1,
            ),
        )
        assert (
            expiring_authorization.status_code == 201
        ), expiring_authorization.text
        updated = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="published-write-expiring-draft",
            json={
                "expected_revision": mark_current_tested()["revision"],
                "op": "update_node",
                "data": {
                    "node_id": "middle",
                    "changes": {
                        "config": connector_config(
                            authorization_id=expiring_authorization.json()[
                                "data"
                            ]["authorization_id"],
                            idempotency_key=(
                                "published-write-expiring-execution"
                            ),
                        )
                    },
                    "merge_config": False,
                },
            },
        )
        assert updated.status_code == 200, updated.text
        mark_current_tested()
        version_two_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="published-write-version-two",
            json={"acknowledge_warnings": False},
        )
        assert version_two_response.status_code == 200, version_two_response.text
        _connector_contract(
            client,
            headers,
            operation_id="records.update",
            key="published-write-expired-run-refresh",
        )
        time.sleep(1.1)
        expired_run_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key="published-write-expired-run",
            json={
                "inputs": {"name": "Ada"},
                "version": version_two_response.json()["data"]["version"],
            },
        )
        assert expired_run_response.status_code == 202, expired_run_response.text
        expired_run = client.portal.call(
            terminal_run,
            expired_run_response.json()["data"]["run_id"],
        )
        assert expired_run["status"] == "failed"
        assert "expired" in str(expired_run["error"]).casefold()
        assert services.connectors._call_adapter.await_count == 1


def test_runtime_exact_connector_authorization_resolves_dynamic_payload(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            "Runtime exact connector authorization",
        )
        headers, _, assignment_id, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
            grant_policy={
                "allowed_actions_digest": "sha256:" + "2" * 64,
                "budget_digest": "sha256:" + "3" * 64,
                "connector_access": True,
                "allowed_network_hosts": ["127.0.0.1"],
                "writable_host_operations": ["warehouse.records.update"],
                "permission_required_actions": ["warehouse.records.update"],
                "compensation_actions": ["warehouse.records.restore"],
                "max_write_count": 2,
                "max_payload_bytes": 4 * 1024 * 1024,
            },
        )
        services = client.app.state.services
        client.portal.call(
            partial(
                _register_generic_connector,
                services,
                application_id=application_id,
                request_key_writes=True,
            )
        )
        contract = _connector_contract(
            client,
            headers,
            operation_id="records.update",
            key="runtime-exact-write",
        )
        assert contract["authorization_modes"] == ["explicit", "runtime_exact"]
        operations = _linear_operations(
            {
                "id": "middle",
                "type": "connector_action",
                "title": "Update a dynamically selected record",
                "config": {
                    "connector_id": "warehouse",
                    "connector_version": 1,
                    "operation_id": "records.update",
                    "tenant_id": "test-tenant",
                    "actor_id": "builder",
                    "actor_roles": ["operator"],
                    "profile_id": "private",
                    "payload": {
                        "record_id": 7,
                        "body": {
                            "status": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output", "name"],
                                }
                            }
                        },
                    },
                    "idempotency_key": "runtime-exact-write-record-7",
                    "authorization_mode": "runtime_exact",
                    "execution_mode": "execute",
                },
                "retry": {
                    "enabled": True,
                    "max_attempts": 3,
                    "delay_seconds": 0,
                },
            }
        )
        _apply_operations(client, application_id, operations)
        services.connectors._call_adapter = AsyncMock(
            side_effect=[
                ConnectorAdapterError(
                    "temporary upstream response",
                    retryable=True,
                    side_effect_state="unknown",
                    adapter_called=True,
                    failure_disposition="retryable",
                    retry_safety="idempotency_key",
                ),
                {},
            ]
        )

        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key="runtime-exact-write-run",
            json={"inputs": {"name": "matched"}, "use_draft": True},
        )
        assert response.status_code == 202, response.text

        async def terminal_run(run_id: str) -> dict:
            for _ in range(300):
                run = await services.workflow_store.get_run(run_id)
                if run["status"] in {"succeeded", "failed", "cancelled"}:
                    return run
                await asyncio.sleep(0.01)
            raise AssertionError("runtime exact connector run did not become terminal")

        run_id = response.json()["data"]["run_id"]
        run = client.portal.call(terminal_run, run_id)
        assert run["status"] == "succeeded", run["error"]
        assert services.connectors._call_adapter.await_count == 2
        budget = client.portal.call(
            services.connectors.export_assignment_budget,
            str(assignment_id),
        )
        assert budget.write_count == 1
        assert budget.writes[0].operation_id == "records.update"
        assert budget.writes[0].attempt_count == 2
        events = client.portal.call(services.storage.list_events, run_id)
        assert [
            event.type
            for event in events
            if event.type in {"permission.requested", "permission.resolved"}
        ] == ["permission.requested", "permission.resolved"]
        state = run["state"]
        assert state.task_credential_ref_digest.startswith("sha256:")
        assert state.task_policy_digest.startswith("sha256:")
        assert state.connector_descriptor_digests[
            "warehouse.records.update"
        ] == contract["descriptor_digest"]


def test_public_publish_keeps_raw_http_fail_closed(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            "Raw network publication remains closed",
        )
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
            grant_policy={
                "connector_access": True,
                "allowed_network_hosts": ["api.example.test"],
            },
        )
        _apply_operations(
            client,
            application_id,
            _linear_operations(
                {
                    "id": "middle",
                    "type": "http_request",
                    "title": "Raw host call",
                    "config": {
                        "method": "GET",
                        "url": "https://api.example.test/records",
                    },
                },
                tests=1,
            ),
        )
        services = client.app.state.services
        draft = client.portal.call(
            services.workflow_store.get_draft,
            application_id,
        )
        client.portal.call(
            services.workflow_store.mark_tested,
            application_id,
            draft["revision"],
            draft["content_hash"],
            {
                "passed": True,
                "validation": {"content_hash": draft["content_hash"]},
            },
        )

        response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="publish-raw-http-denied-0001",
            json={"acknowledge_warnings": False},
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "runtime_network_scope_denied"
        assert client.portal.call(
            services.workflow_store.list_versions,
            application_id,
        ) == []


def test_internal_versions_without_snapshot_keep_legacy_runtime_semantics(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            "Legacy internal version compatibility",
        )
        _apply_operations(
            client,
            application_id,
            _linear_operations(
                {
                    "id": "middle",
                    "type": "variable_assigner",
                    "title": "Legacy deterministic step",
                    "config": {
                        "assignments": {"value": "legacy"},
                    },
                }
            ),
        )
        services = client.app.state.services
        published = client.portal.call(
            partial(
                services.workflow_store.publish,
                application_id,
                acknowledge_warnings=True,
            )
        )
        version = client.portal.call(
            services.workflow_store.get_version,
            application_id,
            published["version"],
        )
        assert "execution_policy_snapshot" not in version["publication_decision"]

        with patch.object(services.workflow_runtime, "_start"):
            created = client.portal.call(
                partial(
                    services.workflow_runtime.create_run,
                    application_id,
                    WorkflowRunRequest(
                        inputs={"name": "Ada"},
                        version=published["version"],
                    ),
                )
            )
        run = client.portal.call(
            services.workflow_store.get_run,
            created["run_id"],
        )
        assert run["state"].published_execution_policy_digest is None
        assert run["state"].execution_policy_digest is None
        assert run["state"].workspace_boundary is None
        assert run["state"].assignment_id is None
        assert run["state"].session_id is None


def test_owner_run_runtime_exact_authorization_is_run_bound(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = _create_internal_application(
            client,
            "Owner runtime exact connector authorization",
        )
        services = client.app.state.services
        client.portal.call(
            partial(
                _register_generic_connector,
                services,
                application_id=application_id,
            )
        )
        _apply_operations(
            client,
            application_id,
            _linear_operations(
                {
                    "id": "middle",
                    "type": "connector_action",
                    "title": "Update one owner-approved record",
                    "config": {
                        "connector_id": "warehouse",
                        "connector_version": 1,
                        "operation_id": "records.update",
                        "tenant_id": "test-tenant",
                        "actor_id": "builder",
                        "actor_roles": ["operator"],
                        "profile_id": "private",
                        "payload": {
                            "record_id": 7,
                            "body": {
                                "status": {
                                    "$ref": {
                                        "node_id": "start",
                                        "path": ["name"],
                                    }
                                }
                            },
                        },
                        "idempotency_key": "owner-runtime-exact-record-7",
                        "authorization_mode": "runtime_exact",
                        "execution_mode": "execute",
                    },
                }
            ),
        )
        services.connectors._call_adapter = AsyncMock(return_value={})
        owner_workspace = tmp_path / "workspaces" / "owner-runtime-exact"
        owner_workspace.mkdir(parents=True)

        created = client.post(
            f"/api/v1/applications/{application_id}/runs",
            headers={"Authorization": "Bearer internal-test-token"},
            json={
                "inputs": {"name": "approved"},
                "use_draft": True,
                "workspace_path": "owner-runtime-exact",
            },
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(200):
            run = client.get(
                f"/api/v1/runs/{run_id}",
                headers={"Authorization": "Bearer internal-test-token"},
            ).json()
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)

        assert run["status"] == "succeeded", run
        authorization_ids = run["state"]["runtime_connector_authorization_ids"]
        assert len(authorization_ids) == 1
        authorization_id = next(iter(authorization_ids.values()))
        assert authorization_id.startswith("cauth-")
        execution_id = run["state"]["outputs"]["middle"]["receipt"][
            "execution_id"
        ]
        execution = client.portal.call(
            services.connectors.get_execution,
            execution_id,
        )
        assert execution.authorization_id == authorization_id
        assert execution.run_id == run_id
        assert execution.application_id == application_id
        assert run["state"]["workspace_path"] == str(owner_workspace.resolve())
        assert services.connectors._call_adapter.await_count == 1
