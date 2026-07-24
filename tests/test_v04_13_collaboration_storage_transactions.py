from __future__ import annotations

import asyncio
import sqlite3
import stat
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from agent_platform.collaboration_service import CollaborationService
from agent_platform.collaboration_storage import (
    CollaborationConflict,
    CollaborationCredentialAlreadyIssued,
    CollaborationStore,
    CollaborationStorageError,
)
from agent_platform.lilies_models import AssignmentMode
from agent_platform.storage import Storage
from agent_platform.workflow_models import ApplicationCreateRequest
from agent_platform.workflow_storage import RevisionConflict, WorkflowStorage
from tests.test_v04_13_collaboration_sqlite_integration import (
    DIGEST_A,
    DIGEST_B,
    NOW,
    _control_message,
    _report_write,
    _store_with_channel,
    _verification_claim_message,
)


def _claim_write(
    *,
    channel_id: UUID,
    assignment_id: UUID,
    expected_channel_revision: int,
    resolved_report_ids: list[UUID],
    marker: str,
) -> tuple[dict[str, object], dict[str, object]]:
    message = _verification_claim_message(channel_id, assignment_id, marker)
    payload = deepcopy(message["payload"])
    payload["resolved_report_ids"] = [str(item) for item in resolved_report_ids]
    message["payload"] = payload
    return (
        {
            **payload,
            "idempotency_key": message["idempotency_key"],
            "expected_channel_revision": expected_channel_revision,
            "payload": payload,
        },
        message,
    )


def _verification_write(
    *,
    channel_id: UUID,
    claim_id: UUID,
    claim_message_id: UUID,
    verdict: str,
    marker: str,
) -> tuple[dict[str, object], dict[str, object]]:
    verification_id = uuid4()
    evidence = {
        "evidence_id": f"evidence:independent-verification-{marker}",
        "kind": "test_run",
        "digest": DIGEST_A,
        "media_type": "application/json",
        "label": "Independent verification evidence",
        "captured_at": NOW.isoformat(),
    }
    differences = (
        []
        if verdict == "independently_verified"
        else [
            {
                "check_id": f"check:independent-verification-{marker}",
                "expected": "The protected oracle succeeds.",
                "actual": "The protected oracle found a mismatch.",
                "evidence_refs": [evidence],
            }
        ]
    )
    payload = {
        "schema_version": "1.0",
        "verification_id": str(verification_id),
        "channel_id": str(channel_id),
        "claim_id": str(claim_id),
        "claim_revision": 1,
        "verdict": verdict,
        "oracle_digest": DIGEST_B,
        "differences": differences,
        "evidence_refs": [evidence],
        "verifier_id": "independent-verifier",
        "created_at": NOW.isoformat(),
    }
    idempotency_key = f"independent-verification-{marker}-0001"
    record = {
        **payload,
        "idempotency_key": idempotency_key,
        "expected_claim_revision": 1,
        "payload": payload,
    }
    message = {
        "schema_version": "1.0",
        "message_id": str(uuid4()),
        "channel_id": str(channel_id),
        "message_type": "verification_result",
        "sender_role": "verifier",
        "sender_id": "independent-verifier",
        "correlation_id": str(claim_id),
        "causal_parent_id": str(claim_message_id),
        "idempotency_key": idempotency_key,
        "visibility": "user_and_lilies",
        "payload_schema": "collaboration.verification_result.v1",
        "payload": payload,
        "evidence_refs": [evidence],
        "client_request_digest": DIGEST_B,
        "created_at": NOW.isoformat(),
    }
    return record, message


@pytest.mark.asyncio
async def test_message_replay_compares_internal_full_client_request_digest(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    request = _control_message(channel_id, "client-request-cas")
    request["client_request_digest"] = DIGEST_A

    created = await store.append_message(request)
    assert "client_request_digest" not in created
    assert (
        await store.get_message_by_idempotency(
            channel_id,
            "platform",
            "platform",
            request["idempotency_key"],
            client_request_digest=DIGEST_A,
        )
        == created
    )

    retry = deepcopy(request)
    retry["message_id"] = str(uuid4())
    assert await store.append_message(retry) == created
    with pytest.raises(CollaborationConflict):
        await store.get_message_by_idempotency(
            channel_id,
            "platform",
            "platform",
            request["idempotency_key"],
            client_request_digest=DIGEST_B,
        )
    retry["client_request_digest"] = DIGEST_B
    with pytest.raises(CollaborationConflict):
        await store.append_message(retry)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT client_request_digest FROM collaboration_messages "
            "WHERE message_id=?",
            (created["message_id"],),
        ).fetchone() == (DIGEST_A,)


@pytest.mark.asyncio
async def test_activation_replay_requires_the_original_prepared_bearer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "activation-replay.db"
    store = CollaborationStore(database)
    await store.initialize()
    base = datetime.now(timezone.utc) + timedelta(minutes=1)
    ticks = iter(
        (
            base,
            base + timedelta(seconds=1),
            base + timedelta(minutes=1),
            base + timedelta(minutes=1, seconds=1),
            base + timedelta(minutes=2),
            base + timedelta(minutes=2, seconds=1),
        )
    )
    service = CollaborationService(store=store, enabled=True, now=lambda: next(ticks))
    assignment_id = uuid4()
    session_id = uuid4()
    prepared = SecretStr("prepared-unpatterned-collaboration-token-00000001")
    request = {
        "assignment_mode": AssignmentMode.formal_experiment,
        "task_id": "EXP-ACTIVATION-REPLAY-001",
        "task_revision": 1,
        "assignment_id": assignment_id,
        "lilies_session_id": session_id,
        "application_ids": [uuid4()],
        "collaboration_enabled": True,
        "user_notified": True,
        "expires_at": base + timedelta(hours=2),
        "retention_until": base + timedelta(days=30),
        "idempotency_key": "activation-prepared-replay-0001",
        "max_report_evidence_rounds": 3,
        "prepared_access_token": prepared,
    }
    first = await service.create_formal_channel(**request)
    replay = await service.create_formal_channel(**request)
    assert replay == first
    assert replay.access_token.get_secret_value() == prepared.get_secret_value()
    with pytest.raises(
        CollaborationConflict,
        match="different activation bindings",
    ):
        await service.create_formal_channel(
            **{**request, "max_report_evidence_rounds": 4}
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_credentials"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_operation_receipts "
            "WHERE operation='channel.activate'"
        ).fetchone() == (1,)

    second_store = CollaborationStore(tmp_path / "activation-unrecoverable.db")
    await second_store.initialize()
    fixed_now = base + timedelta(hours=1)
    second_service = CollaborationService(
        store=second_store, enabled=True, now=lambda: fixed_now
    )
    random_token_request = {
        **request,
        "task_id": "EXP-ACTIVATION-REPLAY-002",
        "assignment_id": uuid4(),
        "lilies_session_id": uuid4(),
        "application_ids": [uuid4()],
        "expires_at": fixed_now + timedelta(hours=2),
        "retention_until": fixed_now + timedelta(days=30),
    }
    random_token_request.pop("prepared_access_token")
    await second_service.create_formal_channel(**random_token_request)
    with pytest.raises(CollaborationCredentialAlreadyIssued):
        await second_service.create_formal_channel(**random_token_request)


@pytest.mark.asyncio
async def test_lease_receipts_compare_exact_client_fields_and_release_report(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    report_id = uuid4()
    report = await store.create_report(*_report_write(channel_id, report_id))
    base = datetime.now(timezone.utc) + timedelta(seconds=1)
    acquire_record = {
        "lease_id": str(uuid4()),
        "report_id": str(report_id),
        "report_revision": report["revision"],
        "owner_id": "codex-receipt-owner",
        "idempotency_key": "lease-exact-acquire-0001",
    }
    acquired = await store.acquire_developer_lease(
        acquire_record,
        ttl_seconds=60,
        now=base,
        next_report_status="implementing",
    )

    generated_identity_retry = {**acquire_record, "lease_id": str(uuid4())}
    assert (
        await store.acquire_developer_lease(
            generated_identity_retry,
            ttl_seconds=60,
            now=base + timedelta(seconds=5),
            next_report_status="implementing",
        )
        == acquired
    )
    assert (
        await store.get_developer_lease_receipt(
            report_id,
            "acquire",
            "codex-receipt-owner",
            "lease-exact-acquire-0001",
            expected_revision=1,
            ttl_seconds=60,
        )
        == acquired
    )
    with pytest.raises(CollaborationConflict):
        await store.get_developer_lease_receipt(
            report_id,
            "acquire",
            "codex-receipt-owner",
            "lease-exact-acquire-0001",
            expected_revision=1,
            ttl_seconds=61,
        )

    renewed = await store.renew_developer_lease(
        acquired["lease_id"],
        owner_id="codex-receipt-owner",
        expected_revision=1,
        ttl_seconds=60,
        idempotency_key="lease-exact-renew-0001",
        now=base + timedelta(seconds=10),
    )
    assert (
        await store.get_developer_lease_receipt(
            report_id,
            "renew",
            "codex-receipt-owner",
            "lease-exact-renew-0001",
            expected_revision=1,
            ttl_seconds=60,
        )
        == renewed
    )
    with pytest.raises(CollaborationConflict):
        await store.get_developer_lease_receipt(
            report_id,
            "renew",
            "codex-receipt-owner",
            "lease-exact-renew-0001",
            expected_revision=2,
            ttl_seconds=60,
        )

    released = await store.release_developer_lease(
        renewed["lease_id"],
        owner_id="codex-receipt-owner",
        expected_revision=2,
        idempotency_key="lease-exact-release-0001",
        reason="return work to the durable developer inbox",
        now=base + timedelta(seconds=20),
    )
    assert released["status"] == "released"
    assert (
        await store.get_developer_lease_receipt(
            report_id,
            "release",
            "codex-receipt-owner",
            "lease-exact-release-0001",
            expected_revision=2,
            reason="return work to the durable developer inbox",
        )
        == released
    )
    with pytest.raises(CollaborationConflict):
        await store.get_developer_lease_receipt(
            report_id,
            "release",
            "codex-receipt-owner",
            "lease-exact-release-0001",
            expected_revision=2,
            reason="different release reason",
        )
    available = await store.get_report(report_id)
    assert (available["status"], available["revision"]) == (
        "approved_for_codex",
        3,
    )
    assert (
        await store.get_developer_lease_receipt(
            report_id,
            "acquire",
            "codex-receipt-owner",
            "lease-exact-acquire-0001",
            expected_revision=1,
            ttl_seconds=60,
        )
        == acquired
    )
    assert (
        await store.get_developer_lease_receipt(
            report_id,
            "renew",
            "codex-receipt-owner",
            "lease-exact-renew-0001",
            expected_revision=1,
            ttl_seconds=60,
        )
        == renewed
    )


@pytest.mark.asyncio
async def test_task_report_revision_cannot_strand_an_active_lease(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    report_id = uuid4()
    task_record, task_message = _report_write(channel_id, report_id)
    task_record["payload"]["category"] = "task_spec_gap"
    task_record.update(
        category="task_spec_gap",
        route="task_author",
        status="routed_to_task_author",
        visibility="user_and_lilies",
    )
    report = await store.create_report(task_record, task_message)
    lease_record = {
        "lease_id": str(uuid4()),
        "report_id": str(report_id),
        "report_revision": report["revision"],
        "owner_id": "codex-task-owner",
        "idempotency_key": "task-active-lease-acquire-0001",
    }
    lease = await store.acquire_developer_lease(
        lease_record,
        ttl_seconds=300,
        now=datetime.now(timezone.utc),
        next_report_status=None,
    )

    revised_payload = deepcopy(task_record["payload"])
    revised_payload["summary"] = "The task package needs a revised public requirement."
    revise_message = deepcopy(task_message)
    revise_message.update(
        message_id=str(uuid4()),
        causal_parent_id=task_message["message_id"],
        idempotency_key="task-revise-during-lease-0001",
        payload=revised_payload,
    )
    revise_changes = {
        "payload": revised_payload,
        "status": "routed_to_task_author",
        "route": "task_author",
        "visibility": "user_and_lilies",
        "phase": revised_payload["phase"],
        "severity": revised_payload["severity"],
    }
    with pytest.raises(CollaborationConflict, match="developer lease is active"):
        await store.revise_report(
            report_id,
            expected_revision=1,
            idempotency_key="task-revise-during-lease-0001",
            actor_role="lilies",
            actor_id="lilies-sqlite",
            changes=revise_changes,
            message=revise_message,
        )
    assert await store.get_active_lease(report_id) == lease

    released = await store.release_developer_lease(
        lease["lease_id"],
        owner_id="codex-task-owner",
        expected_revision=1,
        idempotency_key="task-active-lease-release-0001",
        reason="task author requested a package revision",
        now=datetime.now(timezone.utc),
    )
    assert released["status"] == "released"
    revised = await store.revise_report(
        report_id,
        expected_revision=1,
        idempotency_key="task-revise-during-lease-0001",
        actor_role="lilies",
        actor_id="lilies-sqlite",
        changes=revise_changes,
        message=revise_message,
    )
    assert revised["revision"] == 2

    # On a fresh report, two independent processes can race, but the SQLite
    # write transaction permits only the lease or the revision to win.
    raced_report_id = uuid4()
    raced_record, raced_message = _report_write(
        channel_id,
        raced_report_id,
        expected_channel_revision=2,
    )
    raced_record["payload"]["category"] = "task_spec_gap"
    raced_record.update(
        category="task_spec_gap",
        route="task_author",
        status="routed_to_task_author",
        visibility="user_and_lilies",
    )
    await store.create_report(raced_record, raced_message)
    raced_payload = deepcopy(raced_record["payload"])
    raced_payload["summary"] = "Concurrent task revision is serialized with lease acquisition."
    raced_revision_message = deepcopy(raced_message)
    raced_revision_message.update(
        message_id=str(uuid4()),
        causal_parent_id=raced_message["message_id"],
        idempotency_key="task-race-revision-0001",
        payload=raced_payload,
    )
    acquire_store = CollaborationStore(database)
    revise_store = CollaborationStore(database)
    outcomes = await asyncio.gather(
        acquire_store.acquire_developer_lease(
            {
                "lease_id": str(uuid4()),
                "report_id": str(raced_report_id),
                "report_revision": 1,
                "owner_id": "codex-race-owner",
                "idempotency_key": "task-race-acquire-0001",
            },
            ttl_seconds=300,
            now=datetime.now(timezone.utc),
            next_report_status=None,
        ),
        revise_store.revise_report(
            raced_report_id,
            expected_revision=1,
            idempotency_key="task-race-revision-0001",
            actor_role="lilies",
            actor_id="lilies-sqlite",
            changes={
                "payload": raced_payload,
                "status": "routed_to_task_author",
                "route": "task_author",
                "visibility": "user_and_lilies",
                "phase": raced_payload["phase"],
                "severity": raced_payload["severity"],
            },
            message=raced_revision_message,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum(isinstance(item, CollaborationConflict) for item in outcomes) == 1
    final_report = await store.get_report(raced_report_id)
    active = await store.get_active_lease(raced_report_id)
    assert (final_report["revision"], active is not None) in {(1, True), (2, False)}


@pytest.mark.asyncio
async def test_completed_revision_auto_forwards_with_approval_and_outbox_atomically(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE collaboration_channels SET approval_mode='auto_forward' WHERE channel_id=?",
            (str(channel_id),),
        )
    channel = await store.get_channel(channel_id)
    report_id = uuid4()
    initial_record, initial_message = _report_write(channel_id, report_id)
    initial_record.update(
        status="needs_more_evidence",
        route="capability_approval",
        visibility="user_and_lilies",
    )
    await store.create_report(initial_record, initial_message)
    channel = await store.get_channel(channel_id)

    revised_payload = deepcopy(initial_record["payload"])
    revised_payload["summary"] = "The completed evidence now supports auto-forward."
    revision_message = deepcopy(initial_message)
    revision_message.update(
        message_id=str(uuid4()),
        causal_parent_id=initial_message["message_id"],
        idempotency_key="auto-forward-revision-0001",
        payload=revised_payload,
        client_request_digest=DIGEST_A,
    )
    approval_id = uuid4()
    approval_key = "auto-forward-revision-approval-0001"
    approval_payload = {
        "schema_version": "1.0",
        "approval_id": str(approval_id),
        "channel_id": str(channel_id),
        "report_id": str(report_id),
        "expected_report_revision": 2,
        "resulting_report_revision": 3,
        "decision": "approve",
        "actor_id": "platform-auto-forward",
        "idempotency_key": approval_key,
        "created_at": NOW.isoformat(),
    }
    approval_message_id = uuid4()
    approval_message = {
        "schema_version": "1.0",
        "message_id": str(approval_message_id),
        "channel_id": str(channel_id),
        "message_type": "approval",
        "sender_role": "platform",
        "sender_id": "platform-auto-forward",
        "correlation_id": str(report_id),
        "causal_parent_id": revision_message["message_id"],
        "idempotency_key": approval_key,
        "visibility": "user_and_lilies",
        "payload_schema": "collaboration.approval.v1",
        "payload": approval_payload,
        "evidence_refs": [],
        "created_at": NOW.isoformat(),
    }
    auto_forward = {
        "approval": approval_payload,
        "message": approval_message,
        "audit": {
            "audit_id": str(uuid4()),
            "channel_id": str(channel_id),
            "entity_kind": "report",
            "entity_id": str(report_id),
            "event_type": "collaboration.report_auto_forwarded",
            "actor_role": "platform",
            "actor_id": "platform-auto-forward",
            "idempotency_key": approval_key,
            "details": {"task_revision": channel["task_revision"]},
        },
        "outbox": {
            "outbox_id": str(uuid4()),
            "channel_id": str(channel_id),
            "message_id": str(approval_message_id),
            "destination": "developer_inbox",
            "idempotency_key": approval_key,
            "payload": {"report_id": str(report_id)},
        },
        "next_report_status": "approved_for_codex",
        "next_report_route": "developer",
        "next_visibility": "approved_developer",
    }
    changes = {
        "payload": revised_payload,
        "status": "awaiting_user_review",
        "route": "capability_approval",
        "visibility": "user_and_lilies",
        "phase": revised_payload["phase"],
        "severity": revised_payload["severity"],
    }
    first = await store.revise_report(
        report_id,
        expected_revision=1,
        idempotency_key="auto-forward-revision-0001",
        actor_role="lilies",
        actor_id="lilies-sqlite",
        changes=changes,
        message=revision_message,
        auto_forward=auto_forward,
        expected_channel_revision=channel["revision"],
        expected_approval_mode="auto_forward",
    )
    assert (first["status"], first["route"], first["revision"]) == (
        "approved_for_codex",
        "developer",
        3,
    )
    assert (
        await store.revise_report(
            report_id,
            expected_revision=1,
            idempotency_key="auto-forward-revision-0001",
            actor_role="lilies",
            actor_id="lilies-sqlite",
            changes=changes,
            message=revision_message,
            auto_forward=auto_forward,
            expected_channel_revision=channel["revision"],
            expected_approval_mode="auto_forward",
        )
        == first
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_approvals WHERE report_id=?",
            (str(report_id),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_outbox WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_audit WHERE entity_id=?",
            (str(report_id),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_report_revisions WHERE report_id=?",
            (str(report_id),),
        ).fetchone() == (3,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected_status"),
    [
        ("independently_verified", "independently_verified"),
        ("verification_failed", "verification_failed"),
    ],
)
async def test_closed_claim_verification_cas_resolves_only_capability_reports(
    tmp_path: Path,
    verdict: str,
    expected_status: str,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])

    capability_id = uuid4()
    capability_record, capability_message = _report_write(
        channel_id, capability_id, expected_channel_revision=1
    )
    capability_record.update(status="lilies_verified", route="developer")
    initial_capability = await store.create_report(
        capability_record, capability_message
    )

    terminal_capability_id = uuid4()
    terminal_record, terminal_message = _report_write(
        channel_id, terminal_capability_id, expected_channel_revision=2
    )
    terminal_record.update(status="independently_verified", route="verifier")
    await store.create_report(terminal_record, terminal_message)

    task_report_id = uuid4()
    task_record, task_message = _report_write(
        channel_id, task_report_id, expected_channel_revision=3
    )
    task_record["payload"]["category"] = "task_spec_gap"
    task_record.update(
        category="task_spec_gap",
        route="task_author",
        status="lilies_rechecks",
        visibility="user_and_lilies",
    )
    await store.create_report(task_record, task_message)

    claim_record, claim_message = _claim_write(
        channel_id=channel_id,
        assignment_id=assignment_id,
        expected_channel_revision=4,
        resolved_report_ids=[
            capability_id,
            terminal_capability_id,
            task_report_id,
        ],
        marker=f"closed-{verdict}",
    )
    claim = await store.create_verification_claim(claim_record, claim_message)
    closed = await store.close_channel(
        channel_id,
        expected_revision=5,
        idempotency_key=f"close-before-{verdict}-0001",
        actor_id="studio-user",
        reason="freeze the completed channel for independent verification",
    )

    verification_record, verification_message = _verification_write(
        channel_id=channel_id,
        claim_id=UUID(claim["claim_id"]),
        claim_message_id=UUID(claim_message["message_id"]),
        verdict=verdict,
        marker=f"closed-{verdict}",
    )
    first = await store.record_verification(
        verification_record, verification_message
    )
    assert await store.get_verification(first["verification_id"]) == first
    assert await store.record_verification(
        verification_record, verification_message
    ) == first
    assert (
        await store.create_report(capability_record, capability_message)
        == initial_capability
    )
    assert await store.create_verification_claim(claim_record, claim_message) == claim

    transitioned = await store.get_report(capability_id)
    assert (
        transitioned["status"],
        transitioned["route"],
        transitioned["revision"],
    ) == (expected_status, "verifier", 2)
    preserved_terminal = await store.get_report(terminal_capability_id)
    assert (
        preserved_terminal["status"],
        preserved_terminal["route"],
        preserved_terminal["revision"],
    ) == ("independently_verified", "verifier", 1)
    preserved_task = await store.get_report(task_report_id)
    assert (preserved_task["status"], preserved_task["revision"]) == (
        "lilies_rechecks",
        1,
    )
    completed_claim = await store.get_claim(claim["claim_id"])
    assert (completed_claim["status"], completed_claim["claim_revision"]) == (
        verdict,
        2,
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_report_revisions "
            "WHERE report_id=?",
            (str(capability_id),),
        ).fetchone() == (2,)
    archived = await store.set_channel_state(
        channel_id, "archived", expected_status="closed"
    )
    assert archived["status"] == "archived"
    assert (
        await store.close_channel(
            channel_id,
            expected_revision=5,
            idempotency_key=f"close-before-{verdict}-0001",
            actor_id="studio-user",
            reason="freeze the completed channel for independent verification",
        )
        == closed
    )


@pytest.mark.asyncio
async def test_claim_and_verification_recheck_the_managed_draft_in_the_write_tx(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    application_id = str(channel["application_ids"][0])
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE application_drafts(
              application_id TEXT PRIMARY KEY,
              revision INTEGER NOT NULL,
              content_hash TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO application_drafts VALUES(?,?,?)",
            (application_id, 1, DIGEST_A),
        )

    claim_record, claim_message = _claim_write(
        channel_id=channel_id,
        assignment_id=assignment_id,
        expected_channel_revision=1,
        resolved_report_ids=[],
        marker="managed-draft-claim-race",
    )
    # This read models the service/provider precheck. A separate workflow
    # connection wins the write before collaboration storage begins its tx.
    with sqlite3.connect(database) as provider:
        assert provider.execute(
            "SELECT revision,content_hash FROM application_drafts "
            "WHERE application_id=?",
            (application_id,),
        ).fetchone() == (1, DIGEST_A)
    with sqlite3.connect(database) as workflow_writer:
        workflow_writer.execute(
            "UPDATE application_drafts SET revision=2,content_hash=? "
            "WHERE application_id=?",
            (DIGEST_B, application_id),
        )
    with pytest.raises(CollaborationConflict, match="current application draft"):
        await store.create_verification_claim(claim_record, claim_message)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_verification_claims"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT revision FROM collaboration_channels WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (1,)

    with sqlite3.connect(database) as workflow_writer:
        workflow_writer.execute(
            "UPDATE application_drafts SET revision=1,content_hash=? "
            "WHERE application_id=?",
            (DIGEST_A, application_id),
        )
    claim = await store.create_verification_claim(claim_record, claim_message)
    latest_claim = await store.get_latest_claim(
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    assert latest_claim is not None
    assert latest_claim["claim_id"] == claim["claim_id"]
    assert latest_claim["test_run_ids"] == claim["test_run_ids"]
    assert latest_claim["business_run_ids"] == claim["business_run_ids"]
    verification_record, verification_message = _verification_write(
        channel_id=channel_id,
        claim_id=UUID(claim["claim_id"]),
        claim_message_id=UUID(claim_message["message_id"]),
        verdict="independently_verified",
        marker="managed-draft-result-race",
    )
    with sqlite3.connect(database) as provider:
        assert provider.execute(
            "SELECT revision,content_hash FROM application_drafts "
            "WHERE application_id=?",
            (application_id,),
        ).fetchone() == (1, DIGEST_A)
    with sqlite3.connect(database) as workflow_writer:
        workflow_writer.execute(
            "UPDATE application_drafts SET revision=2,content_hash=? "
            "WHERE application_id=?",
            (DIGEST_B, application_id),
        )
    with pytest.raises(CollaborationConflict, match="current application draft"):
        await store.record_verification(verification_record, verification_message)
    assert (await store.get_claim(claim["claim_id"]))["status"] == "frozen"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_verifications"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages "
            "WHERE message_type='verification_result'"
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_latest_claim_uses_channel_sequence_when_frozen_timestamps_tie(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])

    def claim_write(
        *,
        claim_id: str,
        marker: str,
        expected_channel_revision: int,
        draft_revision: int,
        content_hash: str,
    ) -> tuple[dict[str, object], dict[str, object]]:
        record, message = _claim_write(
            channel_id=channel_id,
            assignment_id=assignment_id,
            expected_channel_revision=expected_channel_revision,
            resolved_report_ids=[],
            marker=marker,
        )
        payload = deepcopy(message["payload"])
        payload.update(
            claim_id=claim_id,
            draft_revision=draft_revision,
            content_hash=content_hash,
        )
        message.update(correlation_id=claim_id, payload=payload)
        record.update(payload)
        record["payload"] = payload
        return record, message

    first_record, first_message = claim_write(
        claim_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        marker="same-time-first",
        expected_channel_revision=1,
        draft_revision=1,
        content_hash=DIGEST_A,
    )
    first = await store.create_verification_claim(first_record, first_message)
    second_record, second_message = claim_write(
        claim_id="00000000-0000-4000-8000-000000000001",
        marker="same-time-second",
        expected_channel_revision=2,
        draft_revision=2,
        content_hash=DIGEST_B,
    )
    second = await store.create_verification_claim(second_record, second_message)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE collaboration_verification_claims SET frozen_at=? "
            "WHERE channel_id=?",
            (second["created_at"], str(channel_id)),
        )

    latest = await store.get_latest_claim(
        channel_id=channel_id,
        assignment_id=assignment_id,
    )

    assert (await store.get_claim(first["claim_id"]))["status"] == "invalidated"
    assert latest is not None
    assert latest["claim_id"] == second["claim_id"]
    assert latest["draft_revision"] == 2
    assert latest["content_hash"] == DIGEST_B


@pytest.mark.asyncio
async def test_workflow_draft_commit_atomically_invalidates_claim_and_uses_sql_cas(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "integrated-draft-storage"
    platform_storage = Storage(data_dir)
    await platform_storage.initialize()
    workflow = WorkflowStorage(platform_storage)
    await workflow.initialize()
    application = await workflow.create_application(
        ApplicationCreateRequest(
            name="Atomic claim invalidation",
            description="Initial application snapshot",
            requirement="Persist draft changes and claim invalidation atomically.",
        )
    )
    application_id = str(application["id"])
    initial_draft = await workflow.get_draft(application_id)

    store = CollaborationStore(platform_storage.db_path)
    await store.initialize()
    activation_time = datetime.now(timezone.utc)
    service = CollaborationService(
        store=store,
        enabled=True,
        now=lambda: activation_time,
    )
    issued = await service.create_formal_channel(
        assignment_mode=AssignmentMode.formal_experiment,
        task_id="EXP-ATOMIC-DRAFT-INVALIDATION-001",
        task_revision=1,
        assignment_id=uuid4(),
        lilies_session_id=uuid4(),
        application_ids=[UUID(application_id)],
        collaboration_enabled=True,
        user_notified=True,
        expires_at=activation_time + timedelta(hours=2),
        retention_until=activation_time + timedelta(days=30),
        idempotency_key="atomic-draft-channel-activation-0001",
        max_report_evidence_rounds=3,
    )
    channel = issued.channel.model_dump(mode="json", exclude_none=True)
    channel_id = UUID(channel["channel_id"])
    claim_record, claim_message = _claim_write(
        channel_id=channel_id,
        assignment_id=UUID(channel["assignment_id"]),
        expected_channel_revision=1,
        resolved_report_ids=[],
        marker="atomic-workflow-draft",
    )
    canonical_hash = f"sha256:{initial_draft['content_hash']}"
    for target in (claim_record, claim_record["payload"], claim_message["payload"]):
        target["application_id"] = application_id
        target["draft_revision"] = initial_draft["revision"]
        target["content_hash"] = canonical_hash
    claim = await store.create_verification_claim(claim_record, claim_message)

    workflow.on_draft_changed_in_transaction = (
        lambda connection, changed_application_id, draft: (
            store.invalidate_verification_claims_in_transaction(
                connection,
                application_id=changed_application_id,
                current_draft_revision=int(draft["revision"]),
                current_content_hash=str(draft["content_hash"]),
                reason="application draft revision or content changed",
            )
        )
    )

    async def simulate_post_commit_process_failure(
        _application_id: str, _draft: dict[str, object]
    ) -> None:
        raise RuntimeError("simulated crash at the former post-commit callback boundary")

    workflow.on_draft_changed = simulate_post_commit_process_failure
    changed_snapshot = initial_draft["snapshot"].model_copy(
        update={"description": "Changed application snapshot"}
    )
    with pytest.raises(RuntimeError, match="former post-commit callback"):
        await workflow.save_draft(
            application_id,
            changed_snapshot,
            expected_revision=0,
            idempotency_key="atomic-draft-save-0001",
        )

    # The post-commit failure cannot split the draft fact from invalidation:
    # both were committed by the earlier shared SQLite transaction.
    assert (await workflow.get_draft(application_id))["revision"] == 1
    persisted_claim = await store.get_claim(claim["claim_id"])
    assert (persisted_claim["status"], persisted_claim["claim_revision"]) == (
        "invalidated",
        2,
    )
    with sqlite3.connect(platform_storage.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_audit "
            "WHERE event_type='claim_invalidated' AND entity_id=?",
            (claim["claim_id"],),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages "
            "WHERE payload_schema='collaboration.control.v1' "
            "AND correlation_id=?",
            (claim["claim_id"],),
        ).fetchone() == (1,)

    workflow.on_draft_changed = None
    first_writer = WorkflowStorage(platform_storage)
    second_writer = WorkflowStorage(platform_storage)
    first_writer.on_draft_changed_in_transaction = (
        workflow.on_draft_changed_in_transaction
    )
    second_writer.on_draft_changed_in_transaction = (
        workflow.on_draft_changed_in_transaction
    )
    current = await workflow.get_draft(application_id)
    first_snapshot = current["snapshot"].model_copy(
        update={"description": "Concurrent writer one"}
    )
    second_snapshot = current["snapshot"].model_copy(
        update={"description": "Concurrent writer two"}
    )
    results = await asyncio.gather(
        first_writer.save_draft(
            application_id,
            first_snapshot,
            expected_revision=1,
            idempotency_key="concurrent-draft-writer-one-0001",
        ),
        second_writer.save_draft(
            application_id,
            second_snapshot,
            expected_revision=1,
            idempotency_key="concurrent-draft-writer-two-0001",
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert (await workflow.get_draft(application_id))["revision"] == 2


@pytest.mark.asyncio
async def test_claim_write_tx_rechecks_report_accounting_after_concurrent_revision(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    report_id = uuid4()
    report_record, report_message = _report_write(channel_id, report_id)
    report_record.update(status="lilies_verified", route="developer")
    report = await store.create_report(report_record, report_message)
    claim_record, claim_message = _claim_write(
        channel_id=channel_id,
        assignment_id=UUID(channel["assignment_id"]),
        expected_channel_revision=2,
        resolved_report_ids=[report_id],
        marker="report-accounting-race",
    )

    # The service has observed a complete report set, but an independent store
    # revises that report without changing the channel CAS revision.
    assert (await store.list_reports(channel_id=channel_id))[0]["status"] == (
        "lilies_verified"
    )
    concurrent_store = CollaborationStore(database)
    await concurrent_store.revise_report(
        report_id,
        expected_revision=report["revision"],
        idempotency_key="report-accounting-race-revision-0001",
        actor_role="lilies",
        actor_id="lilies-sqlite",
        changes={
            "status": "evidence_collecting",
            "route": "capability_approval",
            "visibility": "user_and_lilies",
        },
    )
    with pytest.raises(CollaborationConflict, match="allowed terminal"):
        await store.create_verification_claim(claim_record, claim_message)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_verification_claims"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT revision FROM collaboration_channels WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (2,)

    # User rejection is terminal for capability accounting and therefore does
    # not deadlock the assignment's final independent-verification claim.
    await concurrent_store.revise_report(
        report_id,
        expected_revision=2,
        idempotency_key="report-accounting-terminal-revision-0001",
        actor_role="user",
        actor_id="studio-user",
        changes={
            "status": "rejected",
            "route": "capability_approval",
            "visibility": "user_and_lilies",
        },
    )
    frozen_claim = await store.create_verification_claim(claim_record, claim_message)
    assert frozen_claim["status"] == "frozen"
    with pytest.raises(CollaborationConflict, match="reports are frozen"):
        await concurrent_store.revise_report(
            report_id,
            expected_revision=3,
            idempotency_key="report-write-after-claim-revision-0001",
            actor_role="user",
            actor_id="studio-user",
            changes={
                "status": "withdrawn",
                "route": "capability_approval",
                "visibility": "user_and_lilies",
            },
        )
    new_report_id = uuid4()
    new_report_record, new_report_message = _report_write(
        channel_id,
        new_report_id,
        expected_channel_revision=3,
    )
    with pytest.raises(CollaborationConflict, match="reports are frozen"):
        await concurrent_store.create_report(new_report_record, new_report_message)


@pytest.mark.asyncio
async def test_verified_claim_is_invalidated_after_draft_change_but_archive_is_immutable(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path / "closed")
    channel_id = UUID(channel["channel_id"])
    claim_record, claim_message = _claim_write(
        channel_id=channel_id,
        assignment_id=UUID(channel["assignment_id"]),
        expected_channel_revision=1,
        resolved_report_ids=[],
        marker="verified-then-draft-change",
    )
    claim = await store.create_verification_claim(claim_record, claim_message)
    await store.close_channel(
        channel_id,
        expected_revision=2,
        idempotency_key="close-before-draft-invalidation-0001",
        actor_id="studio-user",
        reason="independent verification may finish after close",
    )
    verification_record, verification_message = _verification_write(
        channel_id=channel_id,
        claim_id=UUID(claim["claim_id"]),
        claim_message_id=UUID(claim_message["message_id"]),
        verdict="independently_verified",
        marker="verified-then-draft-change",
    )
    verification = await store.record_verification(
        verification_record, verification_message
    )
    invalidated = await store.invalidate_verification_claims(
        application_id=claim["application_id"],
        current_draft_revision=claim["draft_revision"] + 1,
        current_content_hash=DIGEST_B,
        reason="the application draft changed after independent verification",
    )
    assert len(invalidated) == 1
    assert (invalidated[0]["status"], invalidated[0]["claim_revision"]) == (
        "invalidated",
        3,
    )
    assert await store.get_verification(verification["verification_id"]) == verification
    invalidation_messages = [
        message
        for message in await store.list_messages(channel_id, after_seq=0, limit=100)
        if message["payload_schema"] == "collaboration.control.v1"
        and message["payload"].get("kind") == "claim_invalidated"
    ]
    assert len(invalidation_messages) == 1
    assert invalidation_messages[0]["causal_parent_id"] == verification_message[
        "message_id"
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_verifications WHERE claim_id=?",
            (claim["claim_id"],),
        ).fetchone() == (1,)

    archived_store, _, archived_channel = await _store_with_channel(
        tmp_path / "archived"
    )
    archived_channel_id = UUID(archived_channel["channel_id"])
    archived_record, archived_message = _claim_write(
        channel_id=archived_channel_id,
        assignment_id=UUID(archived_channel["assignment_id"]),
        expected_channel_revision=1,
        resolved_report_ids=[],
        marker="archived-draft-change",
    )
    archived_claim = await archived_store.create_verification_claim(
        archived_record, archived_message
    )
    await archived_store.close_channel(
        archived_channel_id,
        expected_revision=2,
        idempotency_key="close-before-archive-immutability-0001",
    )
    await archived_store.set_channel_state(
        archived_channel_id, "archived", expected_status="closed"
    )
    before = await archived_store.list_messages(
        archived_channel_id, after_seq=0, limit=100
    )
    assert await archived_store.invalidate_verification_claims(
        application_id=archived_claim["application_id"],
        current_draft_revision=archived_claim["draft_revision"] + 1,
        current_content_hash=DIGEST_B,
        reason="archived channels retain immutable historical facts",
    ) == []
    assert (await archived_store.get_claim(archived_claim["claim_id"]))[
        "status"
    ] == "frozen"
    assert await archived_store.list_messages(
        archived_channel_id, after_seq=0, limit=100
    ) == before


@pytest.mark.asyncio
async def test_registered_secret_fields_and_values_are_redacted_at_all_sinks(
    tmp_path: Path,
) -> None:
    _, database, channel = await _store_with_channel(tmp_path)
    field_secret = "opaque-field-secret-7db562"
    value_secret = "opaque-value-secret-901fca"
    store = CollaborationStore(
        database,
        registered_secret_fields=("tenant_passphrase",),
        registered_secret_values=(value_secret,),
    )
    await store.initialize()
    channel_id = UUID(channel["channel_id"])

    message = _control_message(channel_id, "registered-secret-message")
    message["payload"]["reason"] = f"redact this unpatterned value: {value_secret}"
    stored_message = await store.append_message(message)
    assert stored_message["payload"]["reason"].endswith("[REDACTED]")

    audit = await store.append_audit(
        {
            "audit_id": str(uuid4()),
            "channel_id": str(channel_id),
            "entity_kind": "collaboration_channel",
            "entity_id": str(channel_id),
            "event_type": "registered_secret_redaction",
            "actor_role": "platform",
            "actor_id": "storage-test",
            "idempotency_key": "registered-secret-audit-0001",
            "details": {
                "tenant_passphrase": field_secret,
                "note": f"also redact {value_secret}",
            },
        }
    )
    assert audit["details"] == {
        "tenant_passphrase": "[REDACTED]",
        "note": "also redact [REDACTED]",
    }

    outbox = await store.enqueue_outbox(
        {
            "outbox_id": str(uuid4()),
            "channel_id": str(channel_id),
            "destination": "security-test",
            "idempotency_key": "registered-secret-outbox-0001",
            "payload": {
                "tenant_passphrase": field_secret,
                "note": value_secret,
            },
        }
    )
    assert outbox["payload"] == {
        "tenant_passphrase": "[REDACTED]",
        "note": "[REDACTED]",
    }

    report_id = uuid4()
    report_record, report_message = _report_write(channel_id, report_id)
    report_record.update(route="capability_approval", status="withdrawn")
    report_record["payload"]["summary"] = (
        f"A generic capability is blocked by {value_secret}."
    )
    persisted_report = await store.create_report(report_record, report_message)
    assert value_secret not in persisted_report["summary"]
    assert "[REDACTED]" in persisted_report["summary"]

    claim_record, claim_message = _claim_write(
        channel_id=channel_id,
        assignment_id=UUID(channel["assignment_id"]),
        expected_channel_revision=2,
        resolved_report_ids=[report_id],
        marker="registered-secret-invalidation",
    )
    claim = await store.create_verification_claim(claim_record, claim_message)
    invalidated = await store.invalidate_verification_claims(
        application_id=claim["application_id"],
        assignment_id=claim["assignment_id"],
        current_draft_revision=claim["draft_revision"] + 1,
        current_content_hash=claim["content_hash"],
        reason=f"draft changed because {value_secret}",
    )
    assert invalidated[0]["invalidation_reason"] == (
        "draft changed because [REDACTED]"
    )

    for path in (database, Path(f"{database}-wal"), Path(f"{database}-shm")):
        if path.exists():
            raw = path.read_bytes()
            assert field_secret.encode() not in raw
            assert value_secret.encode() not in raw


@pytest.mark.asyncio
async def test_zero_ack_is_a_stable_initial_cursor_projection(tmp_path: Path) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    first = await store.ack_reader(
        channel["channel_id"],
        "reader-zero",
        0,
        idempotency_key="reader-zero-ack-0001",
        reader_role="user",
        expected_cursor_revision=0,
    )
    replay = await store.ack_reader(
        channel["channel_id"],
        "reader-zero",
        0,
        idempotency_key="reader-zero-ack-0001",
        reader_role="user",
        expected_cursor_revision=0,
    )
    assert first == replay == {
        "schema_version": "1.0",
        "channel_id": channel["channel_id"],
        "reader_role": "user",
        "reader_id": "reader-zero",
        "ack_seq": 0,
        "revision": 0,
        "updated_at": None,
    }


@pytest.mark.asyncio
async def test_schema_guard_wal_and_private_database_modes(tmp_path: Path) -> None:
    database = tmp_path / "private-collaboration.db"
    store = CollaborationStore(database)
    assert await store.initialize() == {"schema_version": 1}
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert stat.S_IMODE(database.stat().st_mode) == 0o600
    for path in (Path(f"{database}-wal"), Path(f"{database}-shm")):
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    future_database = tmp_path / "future-collaboration.db"
    with sqlite3.connect(future_database) as connection:
        connection.execute(
            "CREATE TABLE collaboration_schema(version INTEGER PRIMARY KEY, applied_at TEXT)"
        )
        connection.execute(
            "INSERT INTO collaboration_schema(version,applied_at) VALUES(999,?)",
            (NOW.isoformat(),),
        )
    with pytest.raises(CollaborationStorageError, match="newer than supported"):
        await CollaborationStore(future_database).initialize()
