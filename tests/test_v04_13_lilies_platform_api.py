from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
import httpx
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.lilies_platform_client import LiliesPlatformClient
from agent_platform.lilies_platform_tools import build_lilies_platform_registry
from agent_platform.lilies_tools import LiliesToolContext
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxOperation,
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from agent_platform.platform_contract_version import (
    PlatformContractSchemaDrift,
    PlatformContractVersionRollback,
    PlatformContractVersionStore,
)
from agent_platform.workflow_models import DraftOperation, WorkflowRunState
from tests.test_runtime import ScriptedProvider


ALL_SCOPES = list(PlatformBlackboxScope)
ZERO_DIGEST = "sha256:" + "0" * 64


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="internal-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )


def _issue(
    client: TestClient,
    *,
    scopes: list[PlatformBlackboxScope] | None = None,
    application_ids: list[UUID] | None = None,
) -> tuple[dict[str, str], str, UUID, UUID, str]:
    assignment_id = uuid4()
    session_id = uuid4()
    issued = client.portal.call(
        client.app.state.services.platform_blackbox_auth.issue_credential,
        TaskCredentialGrant(
            assignment_id=assignment_id,
            session_id=session_id,
            scopes=scopes or ALL_SCOPES,
            application_ids=application_ids or [],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    token = issued.access_token.get_secret_value()
    base = {
        "Authorization": f"Bearer {token}",
        "X-Lilies-Assignment-ID": str(assignment_id),
        "X-Lilies-Session-ID": str(session_id),
        "X-Lilies-Contract-Digest": ZERO_DIGEST,
    }
    contract = _request(
        client,
        "GET",
        "/api/v1/lilies/platform-contract",
        base,
        key="contract-bootstrap-0001",
    )
    assert contract.status_code == 200, contract.text
    digest = contract.json()["data"]["contract_digest"]
    base["X-Lilies-Contract-Digest"] = digest
    return base, token, assignment_id, session_id, digest


def _request(
    client: TestClient,
    method: str,
    path: str,
    base_headers: dict[str, str],
    *,
    key: str,
    json: dict | None = None,
    params: dict | None = None,
):
    headers = {
        **base_headers,
        "X-Lilies-Tool-Call-ID": f"tool-{key}",
        "X-Lilies-Idempotency-Key": key,
    }
    return client.request(method, path, headers=headers, json=json, params=params)


def _create_assigned_application(
    client: TestClient,
    headers: dict[str, str],
    *,
    key: str = "application-create-0001",
) -> dict:
    response = _request(
        client,
        "POST",
        "/api/v1/lilies/applications",
        headers,
        key=key,
        json={"name": "Scoped workflow", "requirement": "Build a bounded workflow."},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_contract_facade_enforces_scope_assignment_idempotency_and_internal_denial(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, token, _, _, digest = _issue(client)
        contract = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            {**headers, "X-Lilies-Contract-Digest": ZERO_DIGEST},
            key="contract-inspect-0002",
        ).json()["data"]
        assert contract["contract_digest"] == digest
        assert contract["generated_at"]
        assert all("$defs" not in str(item) for item in contract["operations"])

        created = _create_assigned_application(client, headers)
        application_id = created["id"]
        replay = _create_assigned_application(client, headers)
        assert replay["id"] == application_id

        second_create = _request(
            client,
            "POST",
            "/api/v1/lilies/applications",
            headers,
            key="application-create-0002",
            json={"name": "Second application is outside the one-app assignment"},
        )
        assert second_create.status_code == 404
        assert second_create.json()["error"]["code"] == "not_found"

        correlation_conflict = _request(
            client,
            "POST",
            "/api/v1/lilies/applications",
            headers,
            key="application-create-0003",
            json={
                "name": "Mismatched correlation",
                "idempotency_key": "different-body-key-0003",
            },
        )
        assert correlation_conflict.status_code == 409
        assert correlation_conflict.json()["error"]["code"] == "correlation_conflict"
        create_contract = next(
            item for item in contract["operations"] if item["name"] == "platform_application_create"
        )
        assert {"not_found", "correlation_conflict"} <= set(create_contract["error_codes"])

        conflict = _request(
            client,
            "POST",
            "/api/v1/lilies/applications",
            headers,
            key="application-create-0001",
            json={"name": "Different payload", "requirement": "Must conflict."},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

        internal = client.get(
            "/api/v1/applications",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert internal.status_code == 403
        assert internal.json()["error"]["code"] == "internal_endpoint_denied"

        for method, path in (
            ("GET", "/api/v1/lilies/private-or-unknown"),
            ("PUT", "/api/v1/lilies/platform-contract"),
            ("GET", "/api/v1/lilies/applications/not/an/operation"),
        ):
            denied_path = client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {token}"},
            )
            assert denied_path.status_code == 403, denied_path.text
            assert denied_path.json()["error"]["code"] == "internal_endpoint_denied"
        assert client.get("/api/v1/lilies/private-or-unknown").status_code == 404
        assert client.get(
            "/api/v1/lilies/private-or-unknown",
            headers={"Authorization": "Bearer internal-test-token"},
        ).status_code == 404

        other = client.post(
            "/api/v1/applications",
            headers={"Authorization": "Bearer internal-test-token"},
            json={"name": "Other", "requirement": "Outside assignment."},
        ).json()
        cross_app = _request(
            client,
            "GET",
            f"/api/v1/lilies/applications/{other['id']}",
            headers,
            key="cross-app-read-0001",
        )
        assert cross_app.status_code == 404
        assert cross_app.json()["error"]["code"] == "not_found"

        catalog_headers, _, _, _, _ = _issue(
            client,
            scopes=[PlatformBlackboxScope.catalog_read],
        )
        denied = _request(
            client,
            "POST",
            "/api/v1/lilies/applications",
            catalog_headers,
            key="scope-denial-0001",
            json={"name": "Denied"},
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "authorization_denied"


def test_draft_contract_drift_and_payload_aware_exact_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        headers, _, assignment_id, session_id, digest = _issue(client)
        application_id = _create_assigned_application(client, headers)["id"]
        body = {
            "expected_revision": 0,
            "op": "add_node",
            "data": {
                "node": {
                    "id": "start",
                    "type": "start",
                    "title": "Start",
                    "config": {"inputs": []},
                }
            },
        }
        first = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="draft-operation-0001",
            json=body,
        )
        assert first.status_code == 200, first.text
        replay = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="draft-operation-0001",
            json=body,
        )
        assert replay.json() == first.json()

        changed = {
            **body,
            "data": {"node": {**body["data"]["node"], "title": "Changed"}},
        }
        conflict = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="draft-operation-0001",
            json=changed,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "idempotency_conflict"

        settings.lilies_platform_contract_version = 2
        drift = _request(
            client,
            "GET",
            f"/api/v1/lilies/applications/{application_id}/draft",
            {**headers, "X-Lilies-Contract-Digest": digest},
            key="contract-drift-0001",
        )
        assert drift.status_code == 409
        assert drift.json()["error"]["code"] == "contract_drift"
        assert drift.json()["error"]["expected"] != digest

        refreshed_headers = {
            **headers,
            "X-Lilies-Contract-Digest": drift.json()["error"]["expected"],
        }
        cross_digest_replay = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            refreshed_headers,
            key="draft-operation-0001",
            json=body,
        )
        assert cross_digest_replay.status_code == 200
        assert cross_digest_replay.json() == first.json()
        assert cross_digest_replay.headers["X-Lilies-Idempotent-Replay"] == "true"

        audit = client.portal.call(
            partial(
                client.app.state.services.platform_blackbox_auth.list_audit,
                assignment_id=assignment_id,
                session_id=session_id,
            )
        )
        assert any(item.operation.value == "platform_draft_apply" for item in audit)
        assert all(item.assignment_id == assignment_id for item in audit)
        assert all(item.session_id == session_id for item in audit)


def test_draft_apply_enforces_each_conditional_data_branch_at_http_boundary(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(client, headers)["id"]
        invalid_payloads = [
            {
                "expected_revision": 0,
                "op": "remove_node",
                "data": {"node_id": "missing", "smuggled": True},
            },
            {"expected_revision": 0, "op": "set_metadata", "data": {}},
            {
                "expected_revision": 0,
                "op": "update_node",
                "data": {"node_id": "missing", "changes": {"private_field": "value"}},
            },
            {
                "expected_revision": 0,
                "op": "add_test",
                "data": {
                    "test": {
                        "name": "Strict test",
                        "requirement": "Reject private fields.",
                        "private_oracle": "must never be accepted",
                    }
                },
            },
        ]
        for index, payload in enumerate(invalid_payloads):
            response = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/draft",
                headers,
                key=f"draft-branch-invalid-{index:04d}",
                json=payload,
            )
            assert response.status_code == 422, response.text
            assert response.json()["error"]["code"] == "invalid_request"
            assert response.json()["error"]["failure_owner"] == "task_author"

        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        assert draft["revision"] == 0


def test_contract_version_gate_persists_upgrade_and_rejects_process_rollback(
    tmp_path: Path,
) -> None:
    first_settings = _settings(tmp_path)
    first_app = create_app(first_settings, ScriptedProvider())
    with TestClient(first_app) as client:
        headers, _, _, _, _ = _issue(client)
        first = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            {**headers, "X-Lilies-Contract-Digest": ZERO_DIGEST},
            key="contract-version-one-0001",
        )
        assert first.status_code == 200, first.text
        assert first.json()["data"]["contract_version"] == 1

    upgraded_settings = _settings(tmp_path)
    upgraded_settings.lilies_platform_contract_version = 2
    upgraded_app = create_app(upgraded_settings, ScriptedProvider())
    with TestClient(upgraded_app) as client:
        headers, _, _, _, _ = _issue(client)
        upgraded = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            {**headers, "X-Lilies-Contract-Digest": ZERO_DIGEST},
            key="contract-version-two-0001",
        )
        assert upgraded.status_code == 200, upgraded.text
        assert upgraded.json()["data"]["contract_version"] == 2

    rolled_back_app = create_app(_settings(tmp_path), ScriptedProvider())
    with pytest.raises(PlatformContractVersionRollback):
        with TestClient(rolled_back_app):
            pass


def test_contract_version_gate_rejects_same_version_schema_drift_at_startup(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    seeded = PlatformContractVersionStore(settings.data_dir / "agent_platform.db")
    asyncio.run(seeded.initialize())
    asyncio.run(seeded.observe(contract_version=1, schema_digest="sha256:" + "f" * 64))

    app = create_app(settings, ScriptedProvider())
    with pytest.raises(PlatformContractSchemaDrift):
        with TestClient(app):
            pass


def test_trace_and_artifact_projection_redacts_and_contains_task_data(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, token, assignment_id, session_id, digest = _issue(client)
        application_id = _create_assigned_application(client, headers)["id"]
        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        run_id = str(uuid4())
        workspace = (
            client.app.state.services.settings.workspace_root
            / ".lilies_tasks"
            / str(assignment_id)
            / str(session_id)
            / "run-test-fixture"
        )
        workspace.mkdir(parents=True)
        (workspace / "report.txt").write_text("bounded evidence", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("must not leak", encoding="utf-8")
        (workspace / "escape-link").symlink_to(outside)
        state = WorkflowRunState(
            run_id=run_id,
            application_id=application_id,
            snapshot=draft["snapshot"],
            inputs={},
            workspace_path=str(workspace),
            assignment_id=str(assignment_id),
            session_id=str(session_id),
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.update_run,
                run_id,
                status="succeeded",
                state=state,
                outputs={"result": "done"},
            )
        )
        client.portal.call(
            client.app.state.services.storage.append_event,
            run_id,
            "workflow.started",
            {
                "authorization": "Bearer secret-value",
                "workspace_path": str(workspace),
                "message": "token sk-super-secret-value",
            },
        )
        client.portal.call(
            client.app.state.services.storage.append_event,
            run_id,
            "node.agent.model.thinking.delta",
            {"thinking": "private chain"},
        )

        trace = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/trace",
            headers,
            key="trace-read-request-0001",
        )
        assert trace.status_code == 200, trace.text
        serialized = str(trace.json()["data"])
        assert "secret-value" not in serialized
        assert "private chain" not in serialized
        assert str(workspace) not in serialized

        run = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}",
            headers,
            key="run-read-artifacts-0001",
        )
        assert run.status_code == 200, run.text
        artifacts = run.json()["data"]["artifacts"]
        assert [item["relative_path"] for item in artifacts] == ["report.txt"]
        artifact_id = artifacts[0]["artifact_id"]

        artifact = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}",
            headers,
            key="artifact-read-0001",
        )
        assert artifact.status_code == 200, artifact.text
        artifact_data = artifact.json()["data"]
        assert artifact_data["content"] == "bounded evidence"
        assert artifact_data["sha256"].startswith("sha256:")
        assert artifact_data["offset_bytes"] == 0
        assert artifact_data["chunk_size_bytes"] == len(b"bounded evidence")
        assert artifact_data["next_offset_bytes"] is None
        assert artifact_data["complete"] is True

        chunk = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}",
            headers,
            key="artifact-read-chunk-0001",
            params={"offset_bytes": 8, "max_bytes": 4},
        )
        assert chunk.status_code == 200, chunk.text
        assert chunk.json()["data"] == {
            **artifact_data,
            "offset_bytes": 8,
            "chunk_size_bytes": 4,
            "next_offset_bytes": 12,
            "complete": False,
            "content": "evid",
        }

        async def invoke_real_tool():
            platform_client = LiliesPlatformClient(
                base_url="http://testserver",
                access_token=token,
                assignment_id=assignment_id,
                session_id=session_id,
                contract_digest=digest,
                transport=httpx.ASGITransport(app=client.app),
            )
            tool = build_lilies_platform_registry(
                platform_client,
                include_core_tools=False,
            ).get("platform_artifact_read")
            return await tool.execute(
                {
                    "run_id": run_id,
                    "artifact_id": artifact_id,
                    "offset_bytes": 12,
                    "max_bytes": 4,
                },
                LiliesToolContext(
                    session_id=str(session_id),
                    workspace=tmp_path,
                    tool_call_id="real-artifact-tool-call-0001",
                ),
            )

        tool_result = client.portal.call(invoke_real_tool)
        assert tool_result.is_error is False
        tool_data = json.loads(tool_result.content)["data"]
        assert tool_data["content"] == "ence"
        assert tool_data["offset_bytes"] == 12
        assert tool_data["chunk_size_bytes"] == 4
        assert tool_data["next_offset_bytes"] is None
        assert tool_data["complete"] is True
        assert tool_data["sha256"] == artifact_data["sha256"]

        invalid_range = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}",
            headers,
            key="artifact-read-range-invalid-0001",
            params={"offset_bytes": len(b"bounded evidence") + 1},
        )
        assert invalid_range.status_code == 416, invalid_range.text
        assert invalid_range.json()["error"]["code"] == "artifact_range_invalid"

        unknown = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{uuid4()}",
            headers,
            key="artifact-unknown-0001",
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "not_found"

        invalid_uuid = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/not-a-uuid",
            headers,
            key="artifact-invalid-uuid-0001",
        )
        assert invalid_uuid.status_code == 422
        assert invalid_uuid.json()["operation"] == "platform_artifact_read"
        assert invalid_uuid.json()["error"] == {
            "code": "invalid_request",
            "message": "request payload did not match the public operation schema",
            "retryable": False,
            "failure_owner": "task_author",
            "expected": "public operation request schema",
            "actual": [
                {
                    "location": ["path", "artifact_id"],
                    "type": "uuid_parsing",
                }
            ],
            "evidence_ref": None,
        }

        traversal = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/%2E%2E",
            headers,
            key="artifact-traversal-0001",
        )
        assert traversal.status_code in {404, 422}


def test_contract_hides_publish_without_scope_and_publish_route_denies(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers={"Authorization": "Bearer internal-test-token"},
            json={"name": "Assigned without publish", "requirement": "Remain unpublished."},
        )
        assert created.status_code == 201, created.text
        application_id = UUID(created.json()["id"])

        headers, _, _, _, _ = _issue(
            client,
            scopes=[
                PlatformBlackboxScope.catalog_read,
                PlatformBlackboxScope.application_write,
            ],
            application_ids=[application_id],
        )
        contract_response = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            {**headers, "X-Lilies-Contract-Digest": ZERO_DIGEST},
            key="contract-publish-filter-0001",
        )
        assert contract_response.status_code == 200, contract_response.text
        operations = contract_response.json()["data"]["operations"]
        assert "platform_publish" not in {operation["name"] for operation in operations}
        assert PlatformBlackboxScope.application_publish.value not in {
            operation["scope"] for operation in operations
        }

        denied = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="publish-scope-denial-0001",
            json={"acknowledge_warnings": True},
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "authorization_denied"


def test_revoked_task_credential_is_rejected_by_public_contract(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        assignment_id = uuid4()
        session_id = uuid4()
        issued = client.portal.call(
            client.app.state.services.platform_blackbox_auth.issue_credential,
            TaskCredentialGrant(
                assignment_id=assignment_id,
                session_id=session_id,
                scopes=[PlatformBlackboxScope.catalog_read],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
        token = issued.access_token.get_secret_value()
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Lilies-Assignment-ID": str(assignment_id),
            "X-Lilies-Session-ID": str(session_id),
            "X-Lilies-Contract-Digest": ZERO_DIGEST,
        }
        bootstrap = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers,
            key="contract-before-revoke-0001",
        )
        assert bootstrap.status_code == 200, bootstrap.text
        headers["X-Lilies-Contract-Digest"] = bootstrap.json()["data"]["contract_digest"]

        revoked = client.portal.call(
            partial(
                client.app.state.services.platform_blackbox_auth.revoke_credential,
                issued.credential.credential_ref,
                reason="T01B public-boundary revocation test",
            )
        )
        assert revoked.revoked_at is not None

        rejected = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers,
            key="contract-after-revoke-0001",
        )
        assert rejected.status_code == 401, rejected.text
        assert rejected.json()["error"]["code"] == "credential_revoked"


def test_multi_application_run_routes_audit_the_run_application(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_ids: list[UUID] = []
        for suffix in ("first", "second"):
            created = client.post(
                "/api/v1/applications",
                headers={"Authorization": "Bearer internal-test-token"},
                json={
                    "name": f"Multi-app {suffix}",
                    "requirement": "Exercise task-scoped run routes.",
                },
            )
            assert created.status_code == 201, created.text
            application_ids.append(UUID(created.json()["id"]))

        ordered_application_ids = sorted(application_ids, key=str)
        target_application_id = ordered_application_ids[-1]
        headers, token, assignment_id, session_id, _ = _issue(
            client,
            application_ids=application_ids,
        )
        credential = client.portal.call(
            client.app.state.services.platform_blackbox_auth.authenticate_credential,
            token,
        )
        assert credential.application_ids[0] == ordered_application_ids[0]
        assert credential.application_ids[0] != target_application_id

        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            str(target_application_id),
        )
        workspace = tmp_path / "workspaces" / "multi-app-run-fixtures"
        workspace.mkdir(parents=True)

        def create_run_state(
            run_id: str, *, waiting_node_id: str | None = None
        ) -> WorkflowRunState:
            return WorkflowRunState(
                run_id=run_id,
                application_id=str(target_application_id),
                snapshot=draft["snapshot"],
                    inputs={},
                    workspace_path=str(workspace),
                    waiting_node_id=waiting_node_id,
                    assignment_id=str(assignment_id),
                    session_id=str(session_id),
            )

        get_run_id = str(uuid4())
        get_state = create_run_state(get_run_id)
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                get_state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        fetched = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{get_run_id}",
            headers,
            key="multi-app-run-get-0001",
        )
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["data"]["application_id"] == str(target_application_id)

        resume_run_id = str(uuid4())
        resume_state = create_run_state(resume_run_id, waiting_node_id="human")
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                resume_state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.update_run,
                resume_run_id,
                status="paused",
                state=resume_state,
            )
        )
        with patch.object(
            client.app.state.services.workflow_runtime,
            "resume",
            new_callable=AsyncMock,
        ) as resume:
            resume.return_value = {"run_id": resume_run_id, "status": "queued"}
            resumed = _request(
                client,
                "POST",
                f"/api/v1/lilies/runs/{resume_run_id}/resume",
                headers,
                key="multi-app-run-resume-0001",
                json={"values": {"approved": True}},
            )
            assert resumed.status_code == 200, resumed.text
            resume.assert_awaited_once_with(resume_run_id, {"approved": True})

        cancel_run_id = str(uuid4())
        cancel_state = create_run_state(cancel_run_id)
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                cancel_state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        active_task = MagicMock()
        active_task.done.return_value = False
        client.app.state.services.workflow_runtime.active_tasks[cancel_run_id] = active_task
        cancelled = _request(
            client,
            "POST",
            f"/api/v1/lilies/runs/{cancel_run_id}/cancel",
            headers,
            key="multi-app-run-cancel-0001",
            json={},
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["data"] == {"run_id": cancel_run_id, "status": "cancelling"}
        active_task.cancel.assert_called_once_with()
        client.app.state.services.workflow_runtime.active_tasks.pop(cancel_run_id, None)

        audit = client.portal.call(
            partial(
                client.app.state.services.platform_blackbox_auth.list_audit,
                assignment_id=assignment_id,
                session_id=session_id,
            )
        )
        run_keys = {
            "multi-app-run-get-0001",
            "multi-app-run-resume-0001",
            "multi-app-run-cancel-0001",
        }
        run_audit = [item for item in audit if item.idempotency_key in run_keys]
        assert {item.operation for item in run_audit} == {
            PlatformBlackboxOperation.run_get,
            PlatformBlackboxOperation.run_resume,
            PlatformBlackboxOperation.run_cancel,
        }
        assert {item.application_id for item in run_audit} == {target_application_id}
        assert {item.idempotency_key for item in run_audit} == run_keys


def test_public_draft_and_input_contract_reject_hidden_blocks_and_reserved_keys(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(client, headers)["id"]
        contract = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers,
            key="policy-contract-inspect-0001",
        ).json()["data"]
        operations = {item["name"]: item for item in contract["operations"]}
        assert "runtime_tool_scope_denied" in operations["platform_draft_apply"][
            "error_codes"
        ]
        run_inputs_schema = operations["platform_run_start"]["request_schema"][
            "properties"
        ]["inputs"]
        assert run_inputs_schema["propertyNames"] == {"not": {"pattern": "^__"}}
        draft_branches = operations["platform_draft_apply"]["request_schema"]["allOf"][0][
            "oneOf"
        ]
        add_test_branch = next(
            branch
            for branch in draft_branches
            if branch["properties"]["op"].get("const") == "add_test"
        )
        test_inputs_schema = add_test_branch["properties"]["data"]["properties"][
            "test"
        ]["properties"]["inputs"]
        assert test_inputs_schema["propertyNames"] == {"not": {"pattern": "^__"}}
        boundary_codes = {item["code"] for item in contract["known_boundaries"]}
        assert {
            "assigned_runtime_policy",
            "current_acceptance_required_for_publish",
            "scheduled_publish_not_supported",
        } <= boundary_codes

        hidden_nodes = [
            {
                "id": "legacy-direct",
                "type": "claude_agent",
                "title": "Hidden direct block",
                "config": {"agent_id": str(uuid4()), "task": "Never run."},
            },
            {
                "id": "nested-hidden",
                "type": "iteration",
                "title": "Nested hidden block",
                "config": {
                    "items": [],
                    "workflow": {
                        "nodes": [
                            {
                                "id": "legacy-nested",
                                "type": "claude_agent",
                                "title": "Hidden nested agent",
                                "config": {
                                    "agent_id": str(uuid4()),
                                    "task": "Never run.",
                                },
                            }
                        ],
                        "edges": [],
                    },
                    "output_node_id": "legacy-nested",
                },
            },
        ]
        for index, node in enumerate(hidden_nodes):
            rejected = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/draft",
                headers,
                key=f"hidden-public-node-{index:04d}",
                json={
                    "expected_revision": 0,
                    "op": "add_node",
                    "data": {"node": node},
                },
            )
            assert rejected.status_code == 403, rejected.text
            assert rejected.json()["error"]["code"] == "runtime_tool_scope_denied"

        added = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="public-start-node-0001",
            json={
                "expected_revision": 0,
                "op": "add_node",
                "data": {
                    "node": {
                        "id": "start",
                        "type": "start",
                        "title": "Start",
                        "config": {"inputs": []},
                    }
                },
            },
        )
        assert added.status_code == 200, added.text
        hidden_update = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="hidden-public-update-0001",
            json={
                "expected_revision": 1,
                "op": "update_node",
                "data": {
                    "node_id": "start",
                    "changes": {
                        "type": "claude_agent",
                        "config": {"agent_id": str(uuid4()), "task": "Never run."},
                    },
                    "merge_config": False,
                },
            },
        )
        assert hidden_update.status_code == 403, hidden_update.text
        assert hidden_update.json()["error"]["code"] == "runtime_tool_scope_denied"

        for index, reserved_key in enumerate(("__governed_memory__", "__human__")):
            reserved_run = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/runs",
                headers,
                key=f"reserved-public-run-{index:04d}",
                json={"inputs": {reserved_key: {}}, "use_draft": True},
            )
            assert reserved_run.status_code == 422, reserved_run.text
            assert reserved_run.json()["error"]["code"] == "invalid_request"
        reserved_test = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="reserved-public-test-0001",
            json={
                "expected_revision": 1,
                "op": "add_test",
                "data": {
                    "test": {
                        "id": "reserved-human-test",
                        "name": "Reserved human bypass",
                        "requirement": "Must not self-sign a human response.",
                        "inputs": {"__human__": {"approved": True}},
                        "assertions": [],
                    }
                },
            },
        )
        assert reserved_test.status_code == 422, reserved_test.text
        assert reserved_test.json()["error"]["code"] == "invalid_request"
        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        assert draft["revision"] == 1
        assert client.portal.call(
            client.app.state.services.workflow_store.list_runs,
            application_id,
        ) == []


def test_existing_reserved_test_inputs_are_rejected_by_tests_and_publish(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers={"Authorization": "Bearer internal-test-token"},
            json={"name": "Legacy reserved test", "requirement": "Reject trusted-key forgery."},
        ).json()["id"]
        headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        result = client.portal.call(
            partial(
                client.app.state.services.applications.apply_operation,
                application_id,
                DraftOperation(
                    expected_revision=0,
                    idempotency_key=f"legacy-reserved-test-{uuid4()}",
                    op="add_test",
                    data={
                        "test": {
                            "id": "legacy-reserved",
                            "name": "Legacy reserved input",
                            "requirement": "Must be rejected before execution.",
                            "inputs": {"__job__": {"trusted": True}},
                            "assertions": [],
                            "mandatory": True,
                        }
                    },
                ),
            )
        )
        current = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        client.portal.call(
            client.app.state.services.workflow_store.mark_tested,
            application_id,
            result["revision"],
            current["content_hash"],
            {"passed": True, "validation": {"content_hash": current["content_hash"]}},
        )

        tests_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="legacy-reserved-tests-run-0001",
            json={},
        )
        assert tests_response.status_code == 422, tests_response.text
        assert tests_response.json()["error"]["code"] == "invalid_request"
        publish_response = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="legacy-reserved-publish-0001",
            json={"acknowledge_warnings": True},
        )
        assert publish_response.status_code == 422, publish_response.text
        assert publish_response.json()["error"]["code"] == "invalid_request"
        assert client.portal.call(
            client.app.state.services.workflow_store.list_runs,
            application_id,
        ) == []
        assert client.portal.call(
            client.app.state.services.workflow_store.list_versions,
            application_id,
        ) == []


def test_terminal_run_get_declares_and_returns_artifact_too_large(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers={"Authorization": "Bearer internal-test-token"},
            json={"name": "Large artifact", "requirement": "Reject oversized evidence."},
        ).json()["id"]
        headers, _, assignment_id, session_id, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        contract = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers,
            key="large-artifact-contract-0001",
        ).json()["data"]
        run_get_contract = next(
            item for item in contract["operations"] if item["name"] == "platform_run_get"
        )
        assert {
            "artifact_conflict",
            "artifact_error",
            "artifact_integrity_failed",
            "artifact_path_unsafe",
            "artifact_store_unavailable",
            "artifact_too_large",
        } <= set(run_get_contract["error_codes"])

        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        workspace = (
            client.app.state.services.settings.workspace_root
            / ".lilies_tasks"
            / str(assignment_id)
            / str(session_id)
            / "run-large-artifact"
        )
        workspace.mkdir(parents=True)
        (workspace / "oversized.bin").write_bytes(b"x" * 2_000_001)
        run_id = str(uuid4())
        state = WorkflowRunState(
            run_id=run_id,
            application_id=application_id,
            snapshot=draft["snapshot"],
            inputs={},
            workspace_path=str(workspace),
            assignment_id=str(assignment_id),
            session_id=str(session_id),
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.update_run,
                run_id,
                status="succeeded",
                state=state,
            )
        )
        response = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}",
            headers,
            key="large-artifact-run-get-0001",
        )
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "artifact_too_large"


def test_public_run_resources_require_exact_assignment_and_session_binding(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers={"Authorization": "Bearer internal-test-token"},
            json={"name": "Exact run owner", "requirement": "Bind runs to one task session."},
        ).json()["id"]
        owner_headers, _, assignment_id, session_id, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        other_headers, _, _, _, _ = _issue(
            client,
            application_ids=[UUID(application_id)],
        )
        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        workspace = (
            client.app.state.services.settings.workspace_root
            / ".lilies_tasks"
            / str(assignment_id)
            / str(session_id)
            / "run-binding-fixture"
        )
        workspace.mkdir(parents=True)
        run_id = str(uuid4())
        state = WorkflowRunState(
            run_id=run_id,
            application_id=application_id,
            snapshot=draft["snapshot"],
            inputs={},
            workspace_path=str(workspace),
            waiting_node_id="human",
            assignment_id=str(assignment_id),
            session_id=str(session_id),
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.update_run,
                run_id,
                status="paused",
                state=state,
            )
        )
        assert _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}",
            owner_headers,
            key="bound-owner-read-0001",
        ).status_code == 200

        active_task = MagicMock()
        active_task.done.return_value = False
        client.app.state.services.workflow_runtime.active_tasks[run_id] = active_task
        with patch.object(
            client.app.state.services.workflow_runtime,
            "resume",
            new_callable=AsyncMock,
        ) as resume:
            attempts = (
                ("GET", f"/api/v1/lilies/runs/{run_id}", None),
                ("GET", f"/api/v1/lilies/runs/{run_id}/trace", None),
                (
                    "GET",
                    f"/api/v1/lilies/runs/{run_id}/artifacts/{uuid4()}",
                    None,
                ),
                ("POST", f"/api/v1/lilies/runs/{run_id}/resume", {"values": {}}),
                ("POST", f"/api/v1/lilies/runs/{run_id}/cancel", {}),
            )
            for index, (method, path, body) in enumerate(attempts):
                denied = _request(
                    client,
                    method,
                    path,
                    other_headers,
                    key=f"cross-binding-attempt-{index:04d}",
                    json=body,
                )
                assert denied.status_code == 404, denied.text
                assert denied.json()["error"]["code"] == "not_found"
            resume.assert_not_awaited()
        active_task.cancel.assert_not_called()
        client.app.state.services.workflow_runtime.active_tasks.pop(run_id, None)

        legacy_id = str(uuid4())
        legacy_state = state.model_copy(
            update={"run_id": legacy_id, "assignment_id": None, "session_id": None}
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                legacy_state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        legacy = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{legacy_id}",
            owner_headers,
            key="legacy-unbound-read-0001",
        )
        assert legacy.status_code == 404, legacy.text
        assert legacy.json()["error"]["code"] == "not_found"


def test_blackbox_publish_requires_current_tests_and_rejects_schedules(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, _, _, _ = _issue(client)
        application_id = _create_assigned_application(client, headers)["id"]

        no_tests = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="publish-no-tests-0001",
            json={"acknowledge_warnings": True},
        )
        assert no_tests.status_code == 409, no_tests.text
        assert no_tests.json()["error"]["code"] == "publish_gate_failed"
        assert client.portal.call(
            client.app.state.services.workflow_store.list_versions,
            application_id,
        ) == []

        revision = 0
        operations = [
            (
                "add_node",
                {
                    "node": {
                        "id": "start",
                        "type": "start",
                        "title": "Start",
                        "config": {"inputs": [{"name": "name", "type": "string"}]},
                    }
                },
            ),
            (
                "add_node",
                {
                    "node": {
                        "id": "middle",
                        "type": "variable_assigner",
                        "title": "Assign",
                        "config": {
                            "assignments": {
                                "name": {"$ref": {"node_id": "start", "path": ["name"]}}
                            }
                        },
                    }
                },
            ),
            (
                "add_node",
                {
                    "node": {
                        "id": "end",
                        "type": "end",
                        "title": "End",
                        "config": {
                            "outputs": {
                                "name": {
                                    "$ref": {"node_id": "middle", "path": ["output", "name"]}
                                }
                            }
                        },
                    }
                },
            ),
            ("add_edge", {"edge": {"id": "s-m", "source": "start", "target": "middle"}}),
            ("add_edge", {"edge": {"id": "m-e", "source": "middle", "target": "end"}}),
            (
                "add_test",
                {
                    "test": {
                        "id": "publish-current",
                        "name": "Current acceptance",
                        "requirement": "Return the supplied name.",
                        "inputs": {"name": "Ada"},
                        "assertions": [
                            {"path": ["name"], "operator": "equals", "expected": "Ada"}
                        ],
                        "mandatory": True,
                    }
                },
            ),
        ]
        for index, (operation, data) in enumerate(operations):
            response = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/draft",
                headers,
                key=f"publish-current-draft-{index:04d}",
                json={"expected_revision": revision, "op": operation, "data": data},
            )
            assert response.status_code == 200, response.text
            revision = response.json()["data"]["revision"]
        tested = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="publish-current-tests-0001",
            json={},
        )
        assert tested.status_code == 200, tested.text
        assert tested.json()["data"]["passed"] is True
        published = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="publish-current-pass-0001",
            json={},
        )
        assert published.status_code == 200, published.text
        assert published.json()["data"]["version"] == 1
        refreshed_contract = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            {**headers, "X-Lilies-Contract-Digest": ZERO_DIGEST},
            key="publish-contract-refresh-0001",
        )
        assert refreshed_contract.status_code == 200, refreshed_contract.text
        headers["X-Lilies-Contract-Digest"] = refreshed_contract.json()["data"][
            "contract_digest"
        ]

        stale_edit = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="publish-stale-edit-0001",
            json={
                "expected_revision": revision,
                "op": "set_metadata",
                "data": {"description": "Acceptance is now stale."},
            },
        )
        assert stale_edit.status_code == 200, stale_edit.text
        revision = stale_edit.json()["data"]["revision"]
        stale = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="publish-stale-denied-0001",
            json={"acknowledge_warnings": True},
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "publish_gate_failed"

        schedule = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="publish-schedule-add-0001",
            json={
                "expected_revision": revision,
                "op": "add_node",
                "data": {
                    "node": {
                        "id": "schedule",
                        "type": "schedule_trigger",
                        "title": "Unsafe scheduler",
                        "config": {"timezone": "Asia/Tokyo", "hour": 8, "minute": 0},
                    }
                },
            },
        )
        assert schedule.status_code == 200, schedule.text
        current = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        client.portal.call(
            client.app.state.services.workflow_store.mark_tested,
            application_id,
            current["revision"],
            current["content_hash"],
            {"passed": True, "validation": {"content_hash": current["content_hash"]}},
        )
        schedule_publish = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="publish-schedule-denied-0001",
            json={},
        )
        assert schedule_publish.status_code == 403, schedule_publish.text
        assert schedule_publish.json()["error"]["code"] == "runtime_tool_scope_denied"
        versions = client.portal.call(
            client.app.state.services.workflow_store.list_versions,
            application_id,
        )
        assert [item["version"] for item in versions] == [1]
        application = client.portal.call(
            client.app.state.services.workflow_store.get_application,
            application_id,
        )
        assert application["active_version"] == 1


def _assert_matches_response_schema(value: object, schema: dict) -> None:
    if "const" in schema:
        assert value == schema["const"]
    if "enum" in schema:
        assert value in schema["enum"]
    if "anyOf" in schema:
        matches = 0
        for branch in schema["anyOf"]:
            try:
                _assert_matches_response_schema(value, branch)
            except AssertionError:
                continue
            matches += 1
        assert matches >= 1, (value, schema)
    if "oneOf" in schema:
        matches = 0
        for branch in schema["oneOf"]:
            try:
                _assert_matches_response_schema(value, branch)
            except AssertionError:
                continue
            matches += 1
        assert matches == 1, (value, schema)
    for branch in schema.get("allOf", []):
        _assert_matches_response_schema(value, branch)

    value_type = schema.get("type")
    if value_type == "null":
        assert value is None
    elif value_type == "boolean":
        assert isinstance(value, bool)
    elif value_type == "integer":
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value >= schema.get("minimum", value)
        assert value <= schema.get("maximum", value)
    elif value_type == "number":
        assert isinstance(value, (int, float)) and not isinstance(value, bool)
        assert value >= schema.get("minimum", value)
        assert value <= schema.get("maximum", value)
    elif value_type == "string":
        assert isinstance(value, str)
        assert len(value) >= schema.get("minLength", len(value))
        assert len(value) <= schema.get("maxLength", len(value))
        if "pattern" in schema:
            assert re.search(schema["pattern"], value)
        if schema.get("format") == "uuid":
            UUID(value)
    elif value_type == "array":
        assert isinstance(value, list)
        assert len(value) >= schema.get("minItems", len(value))
        assert len(value) <= schema.get("maxItems", len(value))
        for item in value:
            _assert_matches_response_schema(item, schema.get("items", {}))
    elif value_type == "object" or "properties" in schema or "required" in schema:
        assert isinstance(value, dict)
        assert len(value) >= schema.get("minProperties", len(value))
        properties = schema.get("properties", {})
        assert set(schema.get("required", ())) <= set(value)
        extra = set(value) - set(properties)
        additional = schema.get("additionalProperties", True)
        if additional is False:
            assert not extra, (extra, value, schema)
        elif isinstance(additional, dict):
            for field in extra:
                _assert_matches_response_schema(value[field], additional)
        for field in set(value) & set(properties):
            _assert_matches_response_schema(value[field], properties[field])


def _assert_actual_success_matches_contract(operation: dict, response: object) -> None:
    payload = response.json()  # type: ignore[attr-defined]
    assert set(payload) == {
        "ok",
        "operation",
        "request_id",
        "status_code",
        "contract_digest",
        "data",
        "error",
        "evidence_refs",
    }
    assert payload["ok"] is True
    assert payload["operation"] == operation["name"]
    assert payload["error"] is None
    success_schema = operation["response_schema"]["oneOf"][0]["properties"]["data"]
    data = payload["data"]
    if success_schema.get("type") == "array":
        assert isinstance(data, list)
    _assert_matches_response_schema(data, success_schema)


def test_all_sixteen_public_response_schemas_match_real_http_success_data(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        headers, _, assignment_id, session_id, _ = _issue(client)
        actual: dict[str, object] = {}

        actual["platform_contract_get"] = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers,
            key="response-contract-get-0001",
        )
        contract = actual["platform_contract_get"].json()["data"]  # type: ignore[attr-defined]
        operation_contracts = {item["name"]: item for item in contract["operations"]}

        actual["platform_block_search"] = _request(
            client,
            "GET",
            "/api/v1/lilies/blocks",
            headers,
            key="response-block-search-0001",
            params={"query": "template"},
        )
        actual["platform_block_get"] = _request(
            client,
            "GET",
            "/api/v1/lilies/blocks/template_transform",
            headers,
            key="response-block-get-0001",
        )
        actual["platform_tool_catalog"] = _request(
            client,
            "GET",
            "/api/v1/lilies/tools",
            headers,
            key="response-tool-catalog-0001",
        )
        actual["platform_application_create"] = _request(
            client,
            "POST",
            "/api/v1/lilies/applications",
            headers,
            key="response-application-create-0001",
            json={
                "name": "Response contract workflow",
                "requirement": "Validate every public success response schema.",
            },
        )
        application_id = actual["platform_application_create"].json()["data"]["id"]  # type: ignore[attr-defined]
        actual["platform_application_get"] = _request(
            client,
            "GET",
            f"/api/v1/lilies/applications/{application_id}",
            headers,
            key="response-application-get-0001",
        )

        revision = 0
        draft_operations = [
            (
                "add_node",
                {
                    "node": {
                        "id": "start",
                        "type": "start",
                        "title": "Input",
                        "config": {"inputs": [{"name": "name", "type": "string"}]},
                    }
                },
            ),
            (
                "add_node",
                {
                    "node": {
                        "id": "template",
                        "type": "template_transform",
                        "title": "Greeting",
                        "config": {
                            "template": "Hello {{ name }}",
                            "variables": {
                                "name": {"$ref": {"node_id": "start", "path": ["name"]}}
                            },
                        },
                    }
                },
            ),
            (
                "add_node",
                {
                    "node": {
                        "id": "end",
                        "type": "end",
                        "title": "End",
                        "config": {
                            "outputs": {
                                "greeting": {
                                    "$ref": {"node_id": "template", "path": ["text"]}
                                }
                            }
                        },
                    }
                },
            ),
            (
                "add_edge",
                {"edge": {"id": "start-template", "source": "start", "target": "template"}},
            ),
            (
                "add_edge",
                {
                    "edge": {
                        "id": "template-end",
                        "source": "template",
                        "target": "end",
                        "source_port": "text",
                    }
                },
            ),
            (
                "add_test",
                {
                    "test": {
                        "id": "response-schema-test",
                        "name": "Response schema acceptance",
                        "requirement": "Return a deterministic greeting.",
                        "inputs": {"name": "Ada"},
                        "assertions": [
                            {
                                "path": ["greeting"],
                                "operator": "equals",
                                "expected": "Hello Ada",
                            }
                        ],
                        "mandatory": True,
                    }
                },
            ),
        ]
        for index, (operation_name, data) in enumerate(draft_operations):
            applied = _request(
                client,
                "POST",
                f"/api/v1/lilies/applications/{application_id}/draft",
                headers,
                key=f"response-draft-apply-{index:04d}",
                json={"expected_revision": revision, "op": operation_name, "data": data},
            )
            assert applied.status_code == 200, applied.text
            revision = applied.json()["data"]["revision"]
        actual["platform_draft_apply"] = applied
        actual["platform_draft_inspect"] = _request(
            client,
            "GET",
            f"/api/v1/lilies/applications/{application_id}/draft",
            headers,
            key="response-draft-inspect-0001",
        )
        actual["platform_tests_run"] = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/tests/run",
            headers,
            key="response-tests-run-0001",
            json={},
        )
        assert actual["platform_tests_run"].json()["data"]["passed"] is True, actual[
            "platform_tests_run"
        ].text  # type: ignore[attr-defined]

        run_start_key = "response-run-start-0001"
        actual["platform_run_start"] = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/runs",
            headers,
            key=run_start_key,
            json={"inputs": {"name": "Ada"}, "use_draft": True},
        )
        run_id = actual["platform_run_start"].json()["data"]["run_id"]  # type: ignore[attr-defined]
        for index in range(100):
            run_get = _request(
                client,
                "GET",
                f"/api/v1/lilies/runs/{run_id}",
                headers,
                key=f"response-run-get-poll-{index:04d}",
            )
            if run_get.json()["data"]["status"] in {"succeeded", "failed", "cancelled"}:
                break
        assert run_get.json()["data"]["status"] == "succeeded", run_get.text

        run_record = client.portal.call(
            client.app.state.services.workflow_store.get_run,
            run_id,
        )
        artifact_path = Path(run_record["state"].workspace_path) / "response-schema.txt"
        artifact_path.write_text("response schema evidence", encoding="utf-8")
        actual["platform_run_get"] = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}",
            headers,
            key="response-run-get-artifacts-0001",
        )
        artifact_id = actual["platform_run_get"].json()["data"]["artifacts"][0][  # type: ignore[attr-defined]
            "artifact_id"
        ]
        actual["platform_trace_get"] = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/trace",
            headers,
            key="response-trace-get-0001",
        )
        actual["platform_artifact_read"] = _request(
            client,
            "GET",
            f"/api/v1/lilies/runs/{run_id}/artifacts/{artifact_id}",
            headers,
            key="response-artifact-read-0001",
        )

        draft = client.portal.call(
            client.app.state.services.workflow_store.get_draft,
            application_id,
        )
        fixture_workspace = (
            client.app.state.services.settings.workspace_root
            / ".lilies_tasks"
            / str(assignment_id)
            / str(session_id)
            / "run-response-control-fixture"
        )
        fixture_workspace.mkdir(parents=True)

        resume_run_id = str(uuid4())
        resume_state = WorkflowRunState(
            run_id=resume_run_id,
            application_id=application_id,
            snapshot=draft["snapshot"],
            inputs={},
            workspace_path=str(fixture_workspace),
            waiting_node_id="human",
            assignment_id=str(assignment_id),
            session_id=str(session_id),
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                resume_state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.update_run,
                resume_run_id,
                status="paused",
                state=resume_state,
            )
        )
        with patch.object(
            client.app.state.services.workflow_runtime,
            "resume",
            new_callable=AsyncMock,
            return_value={"run_id": resume_run_id, "status": "queued"},
        ):
            actual["platform_run_resume"] = _request(
                client,
                "POST",
                f"/api/v1/lilies/runs/{resume_run_id}/resume",
                headers,
                key="response-run-resume-0001",
                json={"values": {"approved": True}},
            )

        cancel_run_id = str(uuid4())
        cancel_state = resume_state.model_copy(
            update={"run_id": cancel_run_id, "waiting_node_id": None}
        )
        client.portal.call(
            partial(
                client.app.state.services.workflow_store.create_run,
                cancel_state,
                version=None,
                draft_revision=draft["revision"],
            )
        )
        active_task = MagicMock()
        active_task.done.return_value = False
        client.app.state.services.workflow_runtime.active_tasks[cancel_run_id] = active_task
        actual["platform_run_cancel"] = _request(
            client,
            "POST",
            f"/api/v1/lilies/runs/{cancel_run_id}/cancel",
            headers,
            key="response-run-cancel-0001",
            json={},
        )
        client.app.state.services.workflow_runtime.active_tasks.pop(cancel_run_id, None)

        actual["platform_publish"] = _request(
            client,
            "POST",
            f"/api/v1/lilies/applications/{application_id}/versions",
            headers,
            key="response-publish-0001",
            json={"acknowledge_warnings": True},
        )

        assert set(actual) == set(operation_contracts)
        for name, response in actual.items():
            assert response.status_code < 300, response.text  # type: ignore[attr-defined]
            _assert_actual_success_matches_contract(operation_contracts[name], response)

        refreshed_contract = _request(
            client,
            "GET",
            "/api/v1/lilies/platform-contract",
            headers,
            key="response-contract-refresh-0001",
        )
        headers["X-Lilies-Contract-Digest"] = refreshed_contract.json()["data"][
            "contract_digest"
        ]
        missing_block = _request(
            client,
            "GET",
            "/api/v1/lilies/blocks/not-a-real-block",
            headers,
            key="response-error-envelope-0001",
        )
        error_payload = missing_block.json()
        assert missing_block.status_code == 404
        assert error_payload["data"] == {}
        assert error_payload["error"]["code"] == "not_found"
        _assert_matches_response_schema(
            error_payload,
            operation_contracts["platform_block_get"]["response_schema"],
        )
