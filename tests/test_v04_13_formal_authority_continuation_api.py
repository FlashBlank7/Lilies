from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.collaboration_storage import CollaborationUnauthorized
from agent_platform.config import Settings
from agent_platform.connector_sdk import (
    ConnectorIdentitySubject,
    ConnectorTenantBinding,
)
from agent_platform.external_builder_bootstrap import (
    ExternalBuilderBootstrapReceipt,
)
from agent_platform.formal_authority_continuation_api import (
    _rotation_identifiers,
)
from agent_platform.lilies_models import AssignmentMode
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxCredentialExpired,
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from tests.test_runtime import ScriptedProvider


ZERO_DIGEST = "sha256:" + "0" * 64


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="formal-owner-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
        platform_harness_secret_envelope_key="rotation-test-key-" + "x" * 48,
        lilies_collaboration_enabled=True,
        lilies_collaboration_developer_token="developer-test-token",
        lilies_collaboration_verifier_token="verifier-test-token",
    )


def _task_headers(
    *,
    access_token: str,
    assignment_id: UUID,
    session_id: UUID,
    key: str,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Lilies-Assignment-ID": str(assignment_id),
        "X-Lilies-Session-ID": str(session_id),
        "X-Lilies-Contract-Digest": ZERO_DIGEST,
        "X-Lilies-Idempotency-Key": key,
        "X-Lilies-Request-ID": str(uuid4()),
        "X-Lilies-Tool-Call-ID": key,
    }


def _setup_expiring_authority(
    client: TestClient,
    *,
    issued_at: datetime,
) -> dict[str, object]:
    application = client.post(
        "/api/v1/applications",
        headers={"Authorization": "Bearer formal-owner-test-token"},
        json={
            "name": "Synthetic authority continuation",
            "requirement": "Verify generic assignment authority continuation.",
        },
    )
    assert application.status_code == 201, application.text
    application_id = UUID(application.json()["id"])
    assignment_id = uuid4()
    session_id = uuid4()
    expires_at = issued_at + timedelta(hours=1)
    services = client.app.state.services
    services.platform_blackbox_auth._clock = lambda: issued_at
    services.collaboration._now = lambda: issued_at
    platform = client.portal.call(
        services.platform_blackbox_auth.issue_credential,
        TaskCredentialGrant(
            assignment_id=assignment_id,
            session_id=session_id,
                scopes=[
                    PlatformBlackboxScope.catalog_read,
                    PlatformBlackboxScope.application_write,
                ],
            application_ids=[application_id],
            allowed_actions_digest="sha256:" + "1" * 64,
            budget_digest="sha256:" + "2" * 64,
            allowed_network_hosts=["127.0.0.1"],
            model_access=False,
            file_access=True,
            connector_access=False,
            max_write_count=7,
            max_payload_bytes=65_536,
            max_report_evidence_rounds=3,
            stable_hidden_runs=3,
            expires_at=expires_at,
        ),
    )
    collaboration = client.portal.call(
        partial(
            services.collaboration.create_formal_channel,
            assignment_mode=AssignmentMode.formal_experiment,
            task_id="GENERIC-AUTHORITY-001",
            task_revision=1,
            assignment_id=assignment_id,
            lilies_session_id=session_id,
            application_ids=[application_id],
            collaboration_enabled=True,
            user_notified=True,
            expires_at=expires_at,
            retention_until=expires_at + timedelta(days=1),
            idempotency_key="formal-authority-create-0001",
            max_report_evidence_rounds=3,
        )
    )
    return {
        "application_id": application_id,
        "assignment_id": assignment_id,
        "session_id": session_id,
        "platform": platform,
        "collaboration": collaboration,
        "expires_at": expires_at,
    }


def _continuation_body(authority: dict[str, object]) -> tuple[dict[str, object], str, str]:
    platform_id = uuid4()
    collaboration_id = uuid4()
    platform_token = f"lpt_{platform_id.hex}_{secrets.token_urlsafe(32)}"
    channel_id = authority["collaboration"].channel.channel_id
    collaboration_token = f"lcc_{channel_id.hex}_{secrets.token_urlsafe(48)}"
    return (
        {
            "schema_version": "1.0",
            "session_id": str(authority["session_id"]),
            "channel_id": str(channel_id),
            "previous_platform_credential_ref": (
                authority["platform"].credential.credential_ref
            ),
            "previous_collaboration_credential_ref": (
                authority["collaboration"].credential_ref
            ),
            "new_platform_credential_id": str(platform_id),
            "new_platform_access_token": platform_token,
            "new_collaboration_credential_id": str(collaboration_id),
            "new_collaboration_access_token": collaboration_token,
            "ttl_seconds": 3600,
            "idempotency_key": "formal-authority-continue-0001",
            "reason": "resume the same frozen assignment after operator downtime",
        },
        platform_token,
        collaboration_token,
    )


def test_owner_rebinds_connector_credential_without_secret_disclosure(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        application_id = uuid4()
        binding = ConnectorTenantBinding(
            connector_id="synthetic-erp",
            connector_version=3,
            tenant_id="synthetic-customer",
            external_tenant_id="customer-001",
            profile_id="contract-test",
            secret_ref="secret://synthetic-owner/rotated-alias",
            application_ids=[str(application_id)],
            allowed_operations=["orders_list"],
            subjects=[
                ConnectorIdentitySubject(
                    external_subject="workflow-builder",
                    actor_id="workflow-builder",
                    roles=["builder"],
                )
            ],
            revision=7,
        )
        saved = binding.model_copy(
            update={
                "secret_ref": "secret://synthetic-owner/current-credential",
                "revision": 8,
            }
        )
        services = client.app.state.services
        services.connectors.list_bindings = AsyncMock(
            return_value=[binding]
        )
        services.connectors.upsert_binding = AsyncMock(return_value=saved)
        secret_value = "credential-material-must-not-cross-the-api"
        installed = client.post(
            "/api/v1/platform/secrets",
            headers={"Authorization": "Bearer formal-owner-test-token"},
            json={
                "owner_id": "synthetic-owner",
                "name": "current-credential",
                "value": secret_value,
                "description": "synthetic connector credential",
            },
        )
        assert installed.status_code == 201, installed.text

        response = client.put(
            (
                "/api/v1/platform/formal-environment/"
                "connector-bindings/secret-ref"
            ),
            headers={"Authorization": "Bearer formal-owner-test-token"},
            json={
                "connector_id": "synthetic-erp",
                "connector_version": 3,
                "tenant_id": "synthetic-customer",
                "application_id": str(application_id),
                "expected_binding_revision": 7,
                "current_secret_ref": (
                    "secret://synthetic-owner/rotated-alias"
                ),
                "replacement_secret_ref": (
                    "secret://synthetic-owner/current-credential"
                ),
                "reason": "refresh an owner-managed credential after reset",
            },
        )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["changed"] is True
        assert payload["previous_binding_revision"] == 7
        assert payload["binding_revision"] == 8
        assert payload["secret_ref"] == (
            "secret://synthetic-owner/current-credential"
        )
        assert secret_value not in response.text
        services.connectors.upsert_binding.assert_awaited_once()
        updated_binding = services.connectors.upsert_binding.await_args.args[0]
        assert updated_binding.secret_ref == (
            "secret://synthetic-owner/current-credential"
        )
        assert services.connectors.upsert_binding.await_args.kwargs == {
            "expected_revision": 7
        }

        cross_owner = client.put(
            (
                "/api/v1/platform/formal-environment/"
                "connector-bindings/secret-ref"
            ),
            headers={"Authorization": "Bearer formal-owner-test-token"},
            json={
                "connector_id": "synthetic-erp",
                "connector_version": 3,
                "tenant_id": "synthetic-customer",
                "application_id": str(application_id),
                "expected_binding_revision": 7,
                "current_secret_ref": (
                    "secret://synthetic-owner/rotated-alias"
                ),
                "replacement_secret_ref": (
                    "secret://another-owner/current-credential"
                ),
                "reason": "attempt a forbidden cross-owner rebind",
            },
        )
        assert cross_owner.status_code == 422
        assert cross_owner.json()["detail"]["code"] == (
            "connector_binding_secret_owner_mismatch"
        )


def test_expired_authority_continuation_preserves_policy_and_replays(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        issued_at = datetime.now(timezone.utc)
        authority = _setup_expiring_authority(client, issued_at=issued_at)
        body, platform_token, collaboration_token = _continuation_body(authority)
        continued_at = issued_at + timedelta(hours=2)
        services = client.app.state.services

        with patch(
            "agent_platform.formal_authority_continuation_api._utc_now",
            return_value=continued_at,
        ):
            response = client.post(
                (
                    "/api/v1/formal-assignments/"
                    f"{authority['assignment_id']}/authority/continue"
                ),
                headers={"Authorization": "Bearer formal-owner-test-token"},
                json=body,
            )
            replay = client.post(
                (
                    "/api/v1/formal-assignments/"
                    f"{authority['assignment_id']}/authority/continue"
                ),
                headers={"Authorization": "Bearer formal-owner-test-token"},
                json=body,
            )
        assert response.status_code == 200, response.text
        assert replay.status_code == 200, replay.text
        assert replay.json() == response.json()
        payload = response.json()
        assert payload["application_ids"] == [str(authority["application_id"])]
        assert "access_token" not in response.text

        previous = authority["platform"].credential
        continued = client.portal.call(
            services.platform_blackbox_auth.get_credential,
            payload["platform"]["credential_ref"],
        )
        assert continued.assignment_id == previous.assignment_id
        assert continued.session_id == previous.session_id
        assert continued.scopes == previous.scopes
        assert continued.application_ids == previous.application_ids
        assert continued.allowed_operations == previous.allowed_operations
        assert continued.allowed_actions_digest == previous.allowed_actions_digest
        assert continued.budget_digest == previous.budget_digest
        assert continued.allowed_network_hosts == previous.allowed_network_hosts
        assert continued.model_access is previous.model_access
        assert continued.file_access is previous.file_access
        assert continued.connector_access is previous.connector_access
        assert continued.max_write_count == previous.max_write_count
        assert continued.max_payload_bytes == previous.max_payload_bytes
        assert (
            continued.max_report_evidence_rounds
            == previous.max_report_evidence_rounds
        )
        assert continued.stable_hidden_runs == previous.stable_hidden_runs

        services.platform_blackbox_auth._clock = lambda: continued_at
        with pytest.raises(PlatformBlackboxCredentialExpired):
            client.portal.call(
                services.platform_blackbox_auth.authenticate_credential,
                authority["platform"].access_token.get_secret_value(),
            )
        accepted = client.get(
            "/api/v1/lilies/platform-contract",
            headers=_task_headers(
                access_token=platform_token,
                assignment_id=authority["assignment_id"],
                session_id=authority["session_id"],
                key="continued-contract-bootstrap-0001",
            ),
        )
        assert accepted.status_code == 200, accepted.text

        with patch(
            "agent_platform.collaboration_storage._now",
            return_value=continued_at,
        ):
            with pytest.raises(CollaborationUnauthorized):
                client.portal.call(
                    partial(
                        services.collaboration.store.authenticate_credential,
                        authority["collaboration"].access_token.get_secret_value(),
                        channel_id=authority["collaboration"].channel.channel_id,
                    )
                )
            collaboration_record = client.portal.call(
                partial(
                    services.collaboration.store.authenticate_credential,
                    collaboration_token,
                    channel_id=authority["collaboration"].channel.channel_id,
                )
            )
        assert collaboration_record["assignment_id"] == str(
            authority["assignment_id"]
        )
        assert collaboration_record["lilies_session_id"] == str(
            authority["session_id"]
        )


def test_revoked_or_unexpired_authority_cannot_be_continued(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        issued_at = datetime.now(timezone.utc)
        authority = _setup_expiring_authority(client, issued_at=issued_at)
        body, _, _ = _continuation_body(authority)
        path = (
            "/api/v1/formal-assignments/"
            f"{authority['assignment_id']}/authority/continue"
        )

        active = client.post(
            path,
            headers={"Authorization": "Bearer formal-owner-test-token"},
            json=body,
        )
        assert active.status_code == 409, active.text
        assert active.json()["detail"]["code"] == "authority_not_expired"

        client.portal.call(
            partial(
                client.app.state.services.platform_blackbox_auth.revoke_credential,
                authority["platform"].credential.credential_ref,
                reason="synthetic revocation must remain terminal",
            )
        )
        with patch(
            "agent_platform.formal_authority_continuation_api._utc_now",
            return_value=issued_at + timedelta(hours=2),
        ):
            revoked = client.post(
                path,
                headers={"Authorization": "Bearer formal-owner-test-token"},
                json=body,
            )
        assert revoked.status_code == 409, revoked.text
        assert (
            revoked.json()["detail"]["code"]
            == "revoked_authority_cannot_continue"
        )


def test_expired_authority_can_rotate_to_a_fresh_builder_attempt(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        issued_at = datetime.now(timezone.utc)
        authority = _setup_expiring_authority(client, issued_at=issued_at)
        rotated_at = issued_at + timedelta(hours=2)
        rotation_id = uuid4()
        identifiers = _rotation_identifiers(
            predecessor_assignment_id=authority["assignment_id"],
            rotation_id=rotation_id,
        )
        handoff_path = (
            tmp_path
            / "formal-authority-rotations"
            / f"{identifiers['assignment_id']}.json"
        )
        receipt = ExternalBuilderBootstrapReceipt(
            builder_actor="codex",
            task_id="GENERIC-AUTHORITY-001",
            revision=1,
            run_id="formal-run:rotation-test",
            assignment_id=identifiers["assignment_id"],
            application_id=authority["application_id"],
            build_id=identifiers["build_id"],
            session_id=identifiers["session_id"],
            connection_id=identifiers["connection_id"],
            environment_instance_id="generic-authority:r1:debug",
            channel_id=uuid4(),
            task_credential_ref=f"platform-task-credential:{uuid4()}",
            collaboration_credential_ref=f"collaboration_{uuid4().hex}",
            contract_digest="sha256:" + "3" * 64,
            assignment_bundle_digest="sha256:" + "4" * 64,
            workspace_manifest_digest="sha256:" + "5" * 64,
            workspace_policy_digest="sha256:" + "6" * 64,
            expires_at=rotated_at + timedelta(hours=1),
            handoff_path=handoff_path,
            handoff_digest="sha256:" + "7" * 64,
        )
        body = {
            "schema_version": "1.0",
            "session_id": str(authority["session_id"]),
            "channel_id": str(
                authority["collaboration"].channel.channel_id
            ),
            "previous_platform_credential_ref": (
                authority["platform"].credential.credential_ref
            ),
            "previous_collaboration_credential_ref": (
                authority["collaboration"].credential_ref
            ),
            "application_id": str(authority["application_id"]),
            "task_id": "GENERIC-AUTHORITY-001",
            "revision": 1,
            "environment_instance_id": "generic-authority:r1:debug",
            "retire_predecessor_channel": True,
            "rotation_id": str(rotation_id),
            "idempotency_key": "formal-authority-rotate-0001",
            "reason": "start a new accountable attempt after environment reset",
        }
        bootstrap = AsyncMock(return_value=receipt)
        with (
            patch(
                "agent_platform.formal_authority_continuation_api._utc_now",
                return_value=rotated_at,
            ),
            patch(
                (
                    "agent_platform.formal_authority_continuation_api."
                    "bootstrap_external_builder_async"
                ),
                bootstrap,
            ),
        ):
            response = client.post(
                (
                    "/api/v1/formal-assignments/"
                    f"{authority['assignment_id']}/authority/rotate"
                ),
                headers={"Authorization": "Bearer formal-owner-test-token"},
                json=body,
            )

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["predecessor_assignment_id"] == str(
            authority["assignment_id"]
        )
        assert payload["predecessor_channel_id"] == str(
            authority["collaboration"].channel.channel_id
        )
        assert payload["rotation_id"] == str(rotation_id)
        assert payload["bootstrap"]["assignment_id"] == str(
            identifiers["assignment_id"]
        )
        assert payload["bootstrap"]["application_id"] == str(
            authority["application_id"]
        )
        assert "access_token" not in response.text
        request = bootstrap.await_args.kwargs["request"]
        assert request.assignment_id == identifiers["assignment_id"]
        assert request.application_id == authority["application_id"]
        assert request.handoff_path == handoff_path
        assert callable(bootstrap.await_args.kwargs["task_token_factory"])


def test_active_authority_cannot_rotate(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        issued_at = datetime.now(timezone.utc)
        authority = _setup_expiring_authority(client, issued_at=issued_at)
        response = client.post(
            (
                "/api/v1/formal-assignments/"
                f"{authority['assignment_id']}/authority/rotate"
            ),
            headers={"Authorization": "Bearer formal-owner-test-token"},
            json={
                "schema_version": "1.0",
                "session_id": str(authority["session_id"]),
                "channel_id": str(
                    authority["collaboration"].channel.channel_id
                ),
                "previous_platform_credential_ref": (
                    authority["platform"].credential.credential_ref
                ),
                "previous_collaboration_credential_ref": (
                    authority["collaboration"].credential_ref
                ),
                "application_id": str(authority["application_id"]),
                "task_id": "GENERIC-AUTHORITY-001",
                "revision": 1,
                "environment_instance_id": "generic-authority:r1:debug",
                "retire_predecessor_channel": True,
                "rotation_id": str(uuid4()),
                "idempotency_key": "formal-authority-rotate-active-0001",
                "reason": "active authority must remain single owner",
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "authority_not_expired"
