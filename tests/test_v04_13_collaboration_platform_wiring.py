from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import _git_tree_contains_blob, create_app
from agent_platform.collaboration_models import (
    CollaborationReportPayload,
    VerificationClaimPayload,
    frozen_claim_context_digest,
)
from agent_platform.collaboration_service import CollaborationConflict
from agent_platform.config import Settings
from agent_platform.formal_run_archiver import (
    FormalRunArchiveIntentInvalid,
    FormalRunArchivePreparationRequest,
    FormalRunArchivePreparationResult,
)
from agent_platform.lilies_models import AssignmentMode
from agent_platform.platform_blackbox_auth import (
    PlatformBlackboxScope,
    TaskCredentialGrant,
)
from agent_platform.task_packages import ArchiveClaimBinding
from tests.test_runtime import ScriptedProvider
from tests.test_v04_13_collaboration_sqlite_integration import (
    _developer_response_payload,
    _report_payload,
)


API_TOKEN = "platform-collaboration-api-token"
DEVELOPER_TOKEN = "collaboration-developer-token"
VERIFIER_TOKEN = "collaboration-verifier-token"
DEVELOPMENT_SIGNING_KEY = "development-signing-key-" + "s" * 32
ZERO_DIGEST = "sha256:" + "0" * 64


def settings(tmp_path: Path, *, enabled: bool) -> Settings:
    return Settings(
        api_token=API_TOKEN,
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3_600,
        lilies_local_agent_enabled=False,
        lilies_collaboration_enabled=enabled,
        lilies_collaboration_developer_token=DEVELOPER_TOKEN,
        lilies_collaboration_verifier_token=VERIFIER_TOKEN,
    )


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_git_tree_parser_does_not_accept_a_blob_marker_from_path_bytes() -> None:
    real_blob = "1" * 40
    spoofed_blob = "2" * 40
    tree_output = (
        b"100644 blob "
        + real_blob.encode("ascii")
        + b"\treport blob "
        + spoofed_blob.encode("ascii")
        + b"\tspoof.txt\0"
    )

    assert _git_tree_contains_blob(tree_output, real_blob)
    assert not _git_tree_contains_blob(tree_output, spoofed_blob)


@pytest.mark.parametrize(
    ("developer_token", "verifier_token"),
    [
        ("", VERIFIER_TOKEN),
        (DEVELOPER_TOKEN, ""),
        (API_TOKEN, VERIFIER_TOKEN),
        (DEVELOPER_TOKEN, DEVELOPER_TOKEN),
    ],
)
def test_enabled_collaboration_requires_distinct_role_tokens(
    tmp_path: Path,
    developer_token: str,
    verifier_token: str,
) -> None:
    configured = settings(tmp_path, enabled=True).model_copy(
        update={
            "lilies_collaboration_developer_token": developer_token,
            "lilies_collaboration_verifier_token": verifier_token,
        }
    )
    with pytest.raises(ValueError, match="role tokens|all three"):
        create_app(configured, ScriptedProvider())


@pytest.mark.parametrize(
    "signing_key",
    (
        "",
        "too-short",
        API_TOKEN,
        DEVELOPER_TOKEN,
        VERIFIER_TOKEN,
    ),
)
def test_enabled_collaborative_development_requires_a_distinct_signing_key(
    tmp_path: Path,
    signing_key: str,
) -> None:
    configured = settings(tmp_path, enabled=True).model_copy(
        update={
            "lilies_collaborative_development_enabled": True,
            "lilies_collaborative_development_signing_key": signing_key,
        }
    )
    with pytest.raises(ValueError, match="signing key"):
        create_app(configured, ScriptedProvider())


def test_feature_off_makes_every_malformed_collaboration_surface_the_same_404(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path, enabled=False), ScriptedProvider())
    malformed_routes = (
        ("POST", "/api/v1/collaboration/channels/not-a-uuid/reports"),
        (
            "POST",
            "/api/v1/collaboration/channels/not-a-uuid/reports/not-a-uuid/revisions",
        ),
        (
            "POST",
            "/api/v1/collaboration/channels/not-a-uuid/reports/not-a-uuid/evidence",
        ),
        (
            "POST",
            "/api/v1/collaboration/channels/not-a-uuid/reports/not-a-uuid/reprobes",
        ),
        ("GET", "/api/v1/collaboration/channels/not-a-uuid/events"),
        ("POST", "/api/v1/collaboration/channels/not-a-uuid/acks"),
        ("POST", "/api/v1/collaboration/channels/not-a-uuid/formal-run-archives"),
        ("POST", "/api/v1/collaboration/channels/not-a-uuid/verification-claims"),
        ("GET", "/api/v1/studio/collaboration/channels"),
        ("GET", "/api/v1/studio/collaboration/channels/not-a-uuid"),
        ("GET", "/api/v1/studio/collaboration/channels/not-a-uuid/events"),
        ("GET", "/api/v1/studio/collaboration/channels/not-a-uuid/export"),
        ("POST", "/api/v1/studio/collaboration/reports/not-a-uuid/decision"),
        ("PATCH", "/api/v1/studio/collaboration/channels/not-a-uuid/settings"),
        ("POST", "/api/v1/studio/collaboration/channels/not-a-uuid/close"),
        ("GET", "/api/v1/developer/collaboration/inbox"),
        ("POST", "/api/v1/developer/collaboration/reports/not-a-uuid/lease"),
        ("POST", "/api/v1/developer/collaboration/reports/not-a-uuid/lease/renew"),
        ("POST", "/api/v1/developer/collaboration/reports/not-a-uuid/lease/release"),
        ("POST", "/api/v1/developer/collaboration/reports/not-a-uuid/responses"),
        (
            "POST",
            "/api/v1/developer/collaboration/reports/not-a-uuid/task-amendments",
        ),
        (
            "POST",
            "/api/v1/developer/collaboration/reports/not-a-uuid/environment-responses",
        ),
        (
            "POST",
            "/api/v1/developer/collaboration/claims/not-a-uuid/verification-results",
        ),
    )

    with TestClient(app) as client:
        for method, path in malformed_routes:
            response = client.request(
                method,
                path,
                params={"ToKeN": "must-not-change-feature-off-projection"},
                json={"unexpected": "malformed"},
            )
            assert response.status_code == 404, (method, path, response.text)
            assert response.json() == {"detail": "Not Found"}


def test_enabled_platform_separates_user_developer_and_verifier_credentials(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path, enabled=True), ScriptedProvider())
    claim_id = UUID("22222222-2222-4222-8222-222222222222")
    channel_id = UUID("11111111-1111-4111-8111-111111111111")
    now = datetime.now(timezone.utc)
    frozen_claim = {
        "schema_version": "1.0",
        "claim_id": str(claim_id),
        "channel_id": str(channel_id),
        "assignment_id": str(uuid4()),
        "application_id": str(uuid4()),
        "claim_revision": 1,
        "draft_revision": 1,
        "content_hash": ZERO_DIGEST,
        "test_run_ids": ["test_run_0001"],
        "business_run_ids": ["business_run_0001"],
        "artifact_refs": [],
        "host_receipt_refs": [],
        "resolved_report_ids": [],
        "remaining_limits": [],
        "claim": "ready_for_independent_verification",
        "status": "frozen",
        "created_at": now.isoformat(),
    }

    with TestClient(app) as client:
        studio_path = "/api/v1/studio/collaboration/channels"
        assert client.get(studio_path).status_code == 404
        assert client.get(studio_path, headers=auth(DEVELOPER_TOKEN)).status_code == 404
        assert client.get(studio_path, headers=auth(VERIFIER_TOKEN)).status_code == 404
        studio = client.get(studio_path, headers=auth(API_TOKEN))
        assert studio.status_code == 200, studio.text
        assert studio.json() == {"channels": [], "count": 0}

        developer_path = "/api/v1/developer/collaboration/inbox"
        assert client.get(developer_path).status_code == 404
        assert client.get(developer_path, headers=auth(API_TOKEN)).status_code == 404
        assert client.get(developer_path, headers=auth(VERIFIER_TOKEN)).status_code == 404
        developer = client.get(developer_path, headers=auth(DEVELOPER_TOKEN))
        assert developer.status_code == 200, developer.text
        assert developer.json() == {
            "reports": [],
            "claims": [],
            "pending_user_action": False,
            "next_cursor": 0,
        }

        # No channel or claim creation is needed for this wiring assertion.  A
        # frozen typed projection lets the correct configured verifier bearer
        # pass authentication and reach request validation; every other role's
        # bearer is rejected before the body is considered.
        client.app.state.services.collaboration.store.get_claim = AsyncMock(
            return_value=frozen_claim
        )
        verifier_path = (
            f"/api/v1/developer/collaboration/claims/{claim_id}/verification-results"
        )
        assert client.post(verifier_path, headers=auth(API_TOKEN), json={}).status_code == 404
        assert (
            client.post(verifier_path, headers=auth(DEVELOPER_TOKEN), json={}).status_code
            == 404
        )
        correct_verifier = client.post(
            verifier_path,
            headers=auth(VERIFIER_TOKEN),
            json={},
        )
        assert correct_verifier.status_code == 422
        assert correct_verifier.json()["detail"]["code"] == "invalid_collaboration_request"


@pytest.mark.asyncio
async def test_platform_formal_archive_intent_callback_targets_the_running_bridge(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path, enabled=True).model_copy(
        update={"lilies_local_agent_enabled": True}
    )
    app = create_app(configured, ScriptedProvider())
    services = app.state.services
    provider = services.collaboration._formal_archive_provider
    assert provider is not None
    intent_validator = (
        services.local_lilies_bridge.formal_archive_intent_validator
    )
    assert intent_validator is not None
    channel = SimpleNamespace(channel_id=uuid4())
    request = object()
    services.formal_run_archiver.validate_success_archive_intent = AsyncMock(
        return_value=None
    )

    await intent_validator(channel.channel_id, request)
    services.formal_run_archiver.validate_success_archive_intent.assert_awaited_once_with(
        channel_id=channel.channel_id,
        request=request,
    )
    services.formal_run_archiver.validate_success_archive_intent.reset_mock()

    async def freeze_with_preflight(
        *,
        channel: object,
        request: object,
        actor_id: str,
    ) -> dict[str, str]:
        await services.formal_run_archiver.validate_success_archive_intent(
            channel_id=channel.channel_id,
            request=request,
        )
        return {"state": "awaiting_daemon_completion"}

    services.local_lilies_bridge.freeze_formal_run_archive_intent = AsyncMock(
        side_effect=freeze_with_preflight
    )

    result = await provider(channel, request, "authenticated-lilies-actor")

    assert result == {"state": "awaiting_daemon_completion"}
    services.formal_run_archiver.validate_success_archive_intent.assert_awaited_once_with(
        channel_id=channel.channel_id,
        request=request,
    )
    services.local_lilies_bridge.freeze_formal_run_archive_intent.assert_awaited_once_with(
        channel=channel,
        request=request,
        actor_id="authenticated-lilies-actor",
    )
    services.formal_run_archiver.validate_success_archive_intent = AsyncMock(
        side_effect=FormalRunArchiveIntentInvalid(
            "formal_archive_required_artifact_missing",
            "formal archive must select registered evidence for required deliverables",
        )
    )
    with pytest.raises(CollaborationConflict) as rejected:
        await provider(channel, request, "authenticated-lilies-actor")
    assert rejected.value.code == "formal_archive_required_artifact_missing"
    assert (
        str(rejected.value)
        == "formal archive must select registered evidence for required deliverables"
    )
    assert (
        services.local_lilies_bridge.freeze_formal_run_archive_intent.await_count
        == 2
    )
    assert services.formal_run_archiver is not None
    services.formal_run_archiver.prepare_success_archive = AsyncMock(
        return_value={"archive_manifest_digest": ZERO_DIGEST}
    )
    success_provider = services.local_lilies_bridge.formal_success_archive_provider
    assert success_provider is not None
    channel_id = uuid4()

    archived = await success_provider(channel_id, request)

    assert archived == {"archive_manifest_digest": ZERO_DIGEST}
    services.formal_run_archiver.prepare_success_archive.assert_awaited_once_with(
        channel_id=channel_id,
        request=request,
    )


@pytest.mark.asyncio
async def test_platform_formal_claim_callback_uses_the_frozen_lilies_actor_binding(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path, enabled=True).model_copy(
        update={"lilies_local_agent_enabled": True}
    )
    app = create_app(configured, ScriptedProvider())
    services = app.state.services
    callback = services.local_lilies_bridge.formal_verification_claim_provider
    assert callback is not None

    channel_id = uuid4()
    assignment_id = uuid4()
    application_id = uuid4()
    claim_id = uuid4()
    test_run_id = "test-run:platform-claim-callback-0001"
    business_run_id = "business-run:platform-claim-callback-0001"
    claim_data = {
        "schema_version": "1.1",
        "claim_id": str(claim_id),
        "application_id": str(application_id),
        "draft_revision": 3,
        "content_hash": ZERO_DIGEST,
        "test_run_ids": [test_run_id],
        "business_run_ids": [business_run_id],
        "artifact_refs": [],
        "host_receipt_refs": [],
        "resolved_report_ids": [],
        "remaining_limits": ["controlled local evidence only"],
        "task_package_digest": ZERO_DIGEST,
        "environment_ready_digest": ZERO_DIGEST,
        "archive_manifest_digest": ZERO_DIGEST,
        "verification_process_digest": ZERO_DIGEST,
        "validation_mode": "real_host",
        "claim": "ready_for_independent_verification",
    }
    claim_data["frozen_context_digest"] = frozen_claim_context_digest(claim_data)
    claim = VerificationClaimPayload.model_validate(claim_data)
    binding = ArchiveClaimBinding(
        claim_id=claim_id,
        assignment_id=assignment_id,
        application_id=application_id,
        draft_revision=3,
        content_hash=ZERO_DIGEST,
        test_run_ids=[test_run_id],
        business_run_ids=[business_run_id],
        remaining_limits=["controlled local evidence only"],
    )
    archived = FormalRunArchivePreparationResult(
        task_id="EXP-LILIES-PLATFORM-CLAIM-001",
        revision=1,
        run_id="run:platform-claim-callback-0001",
        assignment_id=assignment_id,
        channel_id=channel_id,
        public_summary_digest=ZERO_DIGEST,
        environment_ready_digest=ZERO_DIGEST,
        workspace_mount_digest=ZERO_DIGEST,
        archive_manifest_digest=ZERO_DIGEST,
        claim_binding=binding,
        verification_claim=claim,
    )
    archive_request = FormalRunArchivePreparationRequest(
        expected_channel_revision=4,
        claim_id=claim_id,
        test_run_ids=[test_run_id],
        business_run_ids=[business_run_id],
        remaining_limits=["controlled local evidence only"],
        summary="Freeze the complete platform-owned formal evidence denominator.",
        idempotency_key="platform-formal-claim-callback-0001",
    )
    services.collaboration.submit_verification_claim = AsyncMock(
        return_value={"claim_id": str(claim_id), "status": "frozen"}
    )

    result = await callback(
        channel_id,
        "frozen-lilies-actor",
        archive_request,
        archived,
    )

    assert result["claim_id"] == str(claim_id)
    call = services.collaboration.submit_verification_claim.await_args
    principal = call.kwargs["principal"]
    submitted = call.kwargs["request"]
    assert principal.role.value == "lilies"
    assert principal.sender_id == "frozen-lilies-actor"
    assert principal.channel_id == channel_id
    assert principal.assignment_id == assignment_id
    assert principal.scopes == frozenset({"collaboration.report:write"})
    assert call.kwargs["channel_id"] == channel_id
    assert submitted.idempotency_key == f"formal.claim.{claim_id.hex}"
    assert submitted.expected_channel_revision == 4
    assert submitted.claim == claim


def test_collaboration_migration_is_safe_across_repeated_platform_lifespans(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path, enabled=True)

    for _ in range(2):
        app = create_app(configured, ScriptedProvider())
        with TestClient(app) as client:
            response = client.get("/health")
            assert response.status_code == 200, response.text

    database = configured.data_dir / "agent_platform.db"
    with sqlite3.connect(database) as connection:
        versions = connection.execute(
            "SELECT version FROM collaboration_schema ORDER BY version"
        ).fetchall()
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name LIKE 'collaboration_%'"
            ).fetchall()
        }
    assert versions == [(1,)]
    assert {
        "collaboration_channels",
        "collaboration_messages",
        "collaboration_reports",
        "collaboration_reader_cursors",
        "collaboration_developer_leases",
        "collaboration_verification_claims",
        "collaboration_verifications",
    } <= table_names


def test_http_formal_developer_response_fails_closed_when_local_agent_is_off(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    commit_sha = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    evidence_path = "docs/evidence/v0.4.13/t01c/deterministic-tests.txt"
    blob_oid = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", f"{commit_sha}:{evidence_path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    blob_bytes = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "blob", blob_oid],
        check=True,
        capture_output=True,
    ).stdout
    now = datetime.now(timezone.utc)
    valid_evidence = {
        "evidence_id": f"gitblob:{commit_sha}:{blob_oid}",
        "kind": "test_run",
        "digest": "sha256:" + hashlib.sha256(blob_bytes).hexdigest(),
        "media_type": "text/plain",
        "label": "Committed deterministic collaboration regression output",
        "captured_at": now.isoformat(),
    }

    app = create_app(settings(tmp_path, enabled=True), ScriptedProvider())
    with TestClient(app) as client:
        service = client.app.state.services.collaboration
        assert service._developer_promotion_resolver is None  # noqa: SLF001
        assert service._developer_commit_resolver is not None  # noqa: SLF001
        issued = client.portal.call(
            partial(
                service.create_formal_channel,
                assignment_mode=AssignmentMode.formal_experiment,
                task_id="EXP-LILIES-HTTP-RESPONSE-001",
                task_revision=1,
                assignment_id=uuid4(),
                lilies_session_id=uuid4(),
                application_ids=[uuid4()],
                collaboration_enabled=True,
                user_notified=True,
                expires_at=now + timedelta(hours=1),
                    retention_until=now + timedelta(days=30),
                    idempotency_key="http-response-channel-activation-0001",
                    max_report_evidence_rounds=3,
                )
        )
        channel_id = issued.channel.channel_id
        report_id = uuid4()
        report = CollaborationReportPayload.model_validate(_report_payload(report_id))
        submitted = client.post(
            f"/api/v1/collaboration/channels/{channel_id}/reports",
            headers=auth(issued.access_token.get_secret_value()),
            json={
                "idempotency_key": "http-response-report-submit-0001",
                "expected_channel_revision": 1,
                "report": report.model_dump(mode="json", exclude_none=True),
            },
        )
        assert submitted.status_code == 201, submitted.text
        assert submitted.json()["status"] == "awaiting_user_review"
        approved = client.post(
            f"/api/v1/studio/collaboration/reports/{report_id}/decision",
            headers=auth(API_TOKEN),
            json={
                "idempotency_key": "http-response-report-approval-0001",
                "expected_report_revision": 3,
                "decision": "approve",
            },
        )
        assert approved.status_code == 200, approved.text
        leased = client.post(
            f"/api/v1/developer/collaboration/reports/{report_id}/lease",
            headers=auth(DEVELOPER_TOKEN),
            json={
                "idempotency_key": "http-response-lease-acquire-0001",
                "expected_report_revision": 4,
                "owner_id": "codex-developer",
                "ttl_seconds": 900,
            },
        )
        assert leased.status_code == 200, leased.text
        lease = leased.json()
        assert (lease["status"], lease["report_revision"]) == ("active", 5)

        def response_request(
            *,
            marker: str,
            response_commit: str,
            evidence_ref: dict[str, object],
        ) -> dict[str, object]:
            payload = _developer_response_payload(
                response_id=uuid4(),
                channel_id=channel_id,
                report_id=report_id,
                report_revision=5,
                created_at=now,
            )
            payload["commit_sha"] = response_commit
            payload["tests_run"][0]["evidence_ref"] = evidence_ref
            return {
                "idempotency_key": f"http-response-{marker}-0001",
                "lease_id": lease["lease_id"],
                "lease_owner_id": "codex-developer",
                "expected_report_revision": 5,
                "response": {
                    key: value
                    for key, value in payload.items()
                    if key
                    not in {
                        "channel_id",
                        "report_id",
                        "report_revision",
                        "created_at",
                    }
                },
            }

        missing_commit = client.post(
            f"/api/v1/developer/collaboration/reports/{report_id}/responses",
            headers=auth(DEVELOPER_TOKEN),
            json=response_request(
                marker="missing-commit",
                response_commit="0" * len(commit_sha),
                evidence_ref=valid_evidence,
            ),
        )
        assert missing_commit.status_code == 409, missing_commit.text
        assert (
            missing_commit.json()["detail"]["code"]
            == "developer_worker_receipt_required"
        )

        missing_evidence = {
            **valid_evidence,
            "evidence_id": f"gitblob:{commit_sha}:{'f' * len(blob_oid)}",
        }
        absent = client.post(
            f"/api/v1/developer/collaboration/reports/{report_id}/responses",
            headers=auth(DEVELOPER_TOKEN),
            json=response_request(
                marker="missing-evidence",
                response_commit=commit_sha,
                evidence_ref=missing_evidence,
            ),
        )
        assert absent.status_code == 409, absent.text
        assert (
            absent.json()["detail"]["code"]
            == "developer_worker_receipt_required"
        )

        mismatched = client.post(
            f"/api/v1/developer/collaboration/reports/{report_id}/responses",
            headers=auth(DEVELOPER_TOKEN),
            json=response_request(
                marker="mismatched-evidence",
                response_commit=commit_sha,
                evidence_ref={**valid_evidence, "digest": ZERO_DIGEST},
            ),
        )
        assert mismatched.status_code == 409, mismatched.text
        assert (
            mismatched.json()["detail"]["code"]
            == "developer_worker_receipt_required"
        )

        database = service.store.db_path
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT status FROM collaboration_reports WHERE report_id=?",
                (str(report_id),),
            ).fetchone() == ("implementing",)
            assert connection.execute(
                "SELECT COUNT(*) FROM collaboration_developer_responses "
                "WHERE report_id=?",
                (str(report_id),),
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT status FROM collaboration_developer_leases WHERE lease_id=?",
                (lease["lease_id"],),
            ).fetchone() == ("active",)

        completed = client.post(
            f"/api/v1/developer/collaboration/reports/{report_id}/responses",
            headers=auth(DEVELOPER_TOKEN),
            json=response_request(
                marker="valid-evidence",
                response_commit=commit_sha,
                evidence_ref=valid_evidence,
            ),
        )
        assert completed.status_code == 409, completed.text
        assert (
            completed.json()["detail"]["code"]
            == "developer_worker_receipt_required"
        )
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT status FROM collaboration_reports WHERE report_id=?",
                (str(report_id),),
            ).fetchone() == ("implementing",)
            assert connection.execute(
                "SELECT COUNT(*) FROM collaboration_developer_responses "
                "WHERE report_id=?",
                (str(report_id),),
            ).fetchone() == (0,)


def test_platform_wires_configured_secrets_into_collaboration_redaction(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path, enabled=True), ScriptedProvider())

    with TestClient(app) as client:
        store = client.app.state.services.collaboration.store
        redacted = store._safe_payload(  # noqa: SLF001 - verify the final boundary
            {
                "lilies_collaboration_developer_token": DEVELOPER_TOKEN,
                "note": f"configured verifier value: {VERIFIER_TOKEN}",
            }
        )

    serialized = json.dumps(redacted, sort_keys=True)
    assert DEVELOPER_TOKEN not in serialized
    assert VERIFIER_TOKEN not in serialized
    assert serialized.count("[REDACTED]") == 2


def test_ordinary_blackbox_contract_never_lists_collaboration_routes(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path, enabled=True).model_copy(
        update={
            "lilies_collaborative_development_enabled": True,
            "lilies_collaborative_development_signing_key": (
                DEVELOPMENT_SIGNING_KEY
            ),
        }
    )
    app = create_app(configured, ScriptedProvider())

    with TestClient(app) as client:
        openapi = client.get("/openapi.json")
        assert openapi.status_code == 200
        paths = openapi.json()["paths"]
        assert not any(
            path.startswith("/api/v1/collaborative-development/")
            for path in paths
        )
        assert "collaboration" not in json.dumps(
            openapi.json(), ensure_ascii=False, sort_keys=True
        ).casefold()
        hidden_development = client.get(
            "/api/v1/collaborative-development/assignments/"
            f"{uuid4()}"
        )
        assert hidden_development.status_code == 404
        assert hidden_development.text == "Not Found"
        assignment_id = uuid4()
        session_id = uuid4()
        issued = client.portal.call(
            client.app.state.services.platform_blackbox_auth.issue_credential,
            TaskCredentialGrant(
                assignment_id=assignment_id,
                session_id=session_id,
                scopes=list(PlatformBlackboxScope),
                application_ids=[],
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            ),
        )
        headers = {
            "Authorization": f"Bearer {issued.access_token.get_secret_value()}",
            "X-Lilies-Assignment-ID": str(assignment_id),
            "X-Lilies-Session-ID": str(session_id),
            "X-Lilies-Contract-Digest": ZERO_DIGEST,
            "X-Lilies-Tool-Call-ID": "ordinary-contract-tool-0001",
            "X-Lilies-Idempotency-Key": "ordinary-contract-read-0001",
        }
        response = client.get("/api/v1/lilies/platform-contract", headers=headers)

    assert response.status_code == 200, response.text
    serialized = json.dumps(response.json(), ensure_ascii=False, sort_keys=True).casefold()
    assert "collaboration" not in serialized
    assert "/api/v1/collaboration" not in serialized
