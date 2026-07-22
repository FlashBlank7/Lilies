from __future__ import annotations

import asyncio
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

import pytest

from agent_platform.collaboration_models import (
    ApprovalDecisionRequest,
    ChannelCloseRequest,
    CollaborationReportPayload,
    LeaseAcquireRequest,
    LeaseReleaseRequest,
    ReaderAckRequest,
    ReportRevisionRequest,
    ReportSubmitRequest,
    SenderRole,
)
from agent_platform.collaboration_service import (
    CollaborationPrincipal,
    CollaborationService,
    CollaborationSubscriberOverflow,
)
from agent_platform.collaboration_storage import (
    CollaborationConflict,
    CollaborationStore,
    CollaborationUnauthorized,
)
from agent_platform.lilies_models import CollaborationScope
from tests.test_v04_13_collaboration_sqlite_integration import (
    DIGEST_B,
    _control_message,
    _report_payload,
    _report_write,
    _store_with_channel,
    _verification_claim_message,
)


def _principals(
    channel: dict[str, Any],
) -> tuple[CollaborationPrincipal, CollaborationPrincipal, CollaborationPrincipal]:
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=str(channel["lilies_session_id"]),
        scopes=frozenset(scope.value for scope in CollaborationScope),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="studio-user",
        scopes=frozenset(),
    )
    developer = CollaborationPrincipal(
        role=SenderRole.codex,
        sender_id="codex-developer",
        scopes=frozenset({"collaboration.developer"}),
    )
    return lilies, user, developer


def _services(database: Path, *, count: int = 16) -> list[CollaborationService]:
    operation_time = datetime.now(timezone.utc) + timedelta(seconds=1)
    return [
        CollaborationService(
            store=CollaborationStore(database),
            enabled=True,
            now=lambda operation_time=operation_time: operation_time,
        )
        for _ in range(count)
    ]


async def _concurrent_identical(
    services: list[CollaborationService],
    mutation: Callable[[CollaborationService], Awaitable[dict[str, Any]]],
) -> list[dict[str, Any]]:
    return await asyncio.gather(*(mutation(service) for service in services))


@pytest.mark.asyncio
async def test_one_hundred_subscriber_overflows_reconnect_from_durable_cursor(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="fault-reader",
        scopes=frozenset(),
    )

    service = CollaborationService(store=store, enabled=True)
    cursor = await service.ack_events(
        principal=user,
        channel_id=channel_id,
        request=ReaderAckRequest(
            idempotency_key="fault-reader-activation-ack-0001",
            expected_cursor_revision=0,
            reader_role=SenderRole.user,
            reader_id=user.sender_id,
            ack_seq=1,
        ),
    )
    assert (cursor["ack_seq"], cursor["revision"]) == (1, 1)

    recovered_ids: list[str] = []
    for iteration in range(1, 101):
        # Each iteration uses a fresh in-memory subscriber, then loses it to an
        # overflow. The only state allowed to bridge the reconnect is SQLite.
        stream = CollaborationService(
            store=CollaborationStore(database),
            enabled=True,
        )
        subscription = stream.subscribe_events(channel_id, max_queue=1)
        persisted = await store.append_message(
            _control_message(channel_id, f"fault-overflow-{iteration:03d}")
        )
        stream._notify_events(channel_id)
        stream._notify_events(channel_id)
        with pytest.raises(CollaborationSubscriberOverflow):
            await stream.wait_for_event(subscription, timeout=0.01)
        stream.unsubscribe_events(subscription)

        reconnected = CollaborationService(
            store=CollaborationStore(database),
            enabled=True,
        )
        resume_after = await reconnected.resolve_event_cursor(
            principal=user,
            channel_id=channel_id,
            # A rendered Last-Event-ID may be ahead of the durable ack. It must
            # never skip the event whose notification was lost on overflow.
            requested_after=int(persisted["seq"]),
            durable=True,
        )
        assert resume_after == iteration
        recovered = await reconnected.list_events(
            principal=user,
            channel_id=channel_id,
            after=resume_after,
            limit=7,
        )
        assert [event["seq"] for event in recovered] == [iteration + 1]
        assert recovered[0]["message_id"] == persisted["message_id"]
        recovered_ids.append(recovered[0]["message_id"])
        cursor = await reconnected.ack_events(
            principal=user,
            channel_id=channel_id,
            request=ReaderAckRequest(
                idempotency_key=f"fault-reader-ack-{iteration:03d}",
                expected_cursor_revision=iteration,
                reader_role=SenderRole.user,
                reader_id=user.sender_id,
                ack_seq=iteration + 1,
            ),
        )

    assert (cursor["ack_seq"], cursor["revision"]) == (101, 101)
    assert len(recovered_ids) == len(set(recovered_ids)) == 100
    persisted_messages = await store.list_messages(
        channel_id,
        after_seq=0,
        limit=1_000,
    )
    assert [message["seq"] for message in persisted_messages] == list(range(1, 102))
    assert [message["message_id"] for message in persisted_messages[1:]] == recovered_ids
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT ack_seq,revision FROM collaboration_reader_cursors "
            "WHERE channel_id=? AND reader_role='user' AND reader_id=?",
            (str(channel_id), user.sender_id),
        ).fetchone() == (101, 101)


@pytest.mark.asyncio
async def test_one_hundred_lease_fault_retry_expiry_cycles_reject_stale_owners(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    report_id = uuid4()
    report = await store.create_report(*_report_write(channel_id, report_id))
    expected_report_revision = int(report["revision"])
    start = datetime.now(timezone.utc) + timedelta(seconds=1)

    for iteration in range(100):
        cycle_time = start + timedelta(minutes=4 * iteration)
        stale_owner = f"codex-stale-{iteration:03d}"
        stale_acquire = {
            "lease_id": str(uuid4()),
            "report_id": str(report_id),
            "report_revision": expected_report_revision,
            "owner_id": stale_owner,
            "idempotency_key": f"fault-stale-acquire-{iteration:03d}",
        }
        leased = await store.acquire_developer_lease(
            stale_acquire,
            ttl_seconds=60,
            now=cycle_time,
            next_report_status="implementing",
        )
        # Retry with a regenerated server-side identity: the receipt, not that
        # generated UUID or the current report state, defines the first result.
        assert (
            await CollaborationStore(database).acquire_developer_lease(
                {**stale_acquire, "lease_id": str(uuid4())},
                ttl_seconds=60,
                now=cycle_time + timedelta(seconds=1),
                next_report_status="implementing",
            )
            == leased
        )
        renewed = await store.renew_developer_lease(
            leased["lease_id"],
            owner_id=stale_owner,
            expected_revision=1,
            ttl_seconds=60,
            idempotency_key=f"fault-stale-renew-{iteration:03d}",
            now=cycle_time + timedelta(seconds=10),
        )
        assert renewed["revision"] == 2
        assert (
            await CollaborationStore(database).renew_developer_lease(
                leased["lease_id"],
                owner_id=stale_owner,
                expected_revision=1,
                ttl_seconds=60,
                idempotency_key=f"fault-stale-renew-{iteration:03d}",
                now=cycle_time + timedelta(seconds=11),
            )
            == renewed
        )

        assert (
            await store.expire_developer_leases(
                now=cycle_time + timedelta(seconds=71)
            )
            == 1
        )
        expired_report = await store.get_report(report_id)
        assert (expired_report["status"], expired_report["revision"]) == (
            "approved_for_codex",
            expected_report_revision + 2,
        )
        with pytest.raises(CollaborationConflict):
            await store.renew_developer_lease(
                leased["lease_id"],
                owner_id=stale_owner,
                expected_revision=2,
                ttl_seconds=60,
                idempotency_key=f"fault-expired-renew-{iteration:03d}",
                now=cycle_time + timedelta(seconds=72),
            )
        with pytest.raises(CollaborationConflict):
            await store.acquire_developer_lease(
                {
                    "lease_id": str(uuid4()),
                    "report_id": str(report_id),
                    "report_revision": expected_report_revision,
                    "owner_id": stale_owner,
                    "idempotency_key": f"fault-stale-cas-{iteration:03d}",
                },
                ttl_seconds=60,
                now=cycle_time + timedelta(seconds=73),
                next_report_status="implementing",
            )

        recovery_owner = f"codex-recovery-{iteration:03d}"
        recovered = await store.acquire_developer_lease(
            {
                "lease_id": str(uuid4()),
                "report_id": str(report_id),
                "report_revision": expected_report_revision + 2,
                "owner_id": recovery_owner,
                "idempotency_key": f"fault-recovery-acquire-{iteration:03d}",
            },
            ttl_seconds=60,
            now=cycle_time + timedelta(seconds=74),
            next_report_status="implementing",
        )
        assert recovered["report_revision"] == expected_report_revision + 3
        with pytest.raises(CollaborationUnauthorized):
            await store.release_developer_lease(
                recovered["lease_id"],
                owner_id=stale_owner,
                expected_revision=1,
                idempotency_key=f"fault-old-owner-release-{iteration:03d}",
                reason="stale worker must not release the replacement lease",
                now=cycle_time + timedelta(seconds=75),
            )
        released = await store.release_developer_lease(
            recovered["lease_id"],
            owner_id=recovery_owner,
            expected_revision=1,
            idempotency_key=f"fault-recovery-release-{iteration:03d}",
            reason="return the recovered report to the developer inbox",
            now=cycle_time + timedelta(seconds=76),
        )
        assert (released["status"], released["revision"]) == ("released", 2)
        expected_report_revision += 4
        available = await store.get_report(report_id)
        assert (available["status"], available["revision"]) == (
            "approved_for_codex",
            expected_report_revision,
        )

    assert await store.get_active_lease(report_id) is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_developer_leases WHERE report_id=?",
            (str(report_id),),
        ).fetchone() == (200,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_developer_leases "
            "WHERE report_id=? AND status='expired'",
            (str(report_id),),
        ).fetchone() == (100,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_developer_leases "
            "WHERE report_id=? AND status='released'",
            (str(report_id),),
        ).fetchone() == (100,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_report_revisions WHERE report_id=?",
            (str(report_id),),
        ).fetchone() == (401,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (2,)


@pytest.mark.asyncio
async def test_receipts_serialize_concurrent_revise_approval_release_and_close(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    lilies, user, developer = _principals(channel)
    report_id = uuid4()
    incomplete_payload = _report_payload(report_id)
    incomplete_payload.pop("manuals_checked")
    initial = await CollaborationService(
        store=store,
        enabled=True,
    ).submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="fault-concurrent-report-submit-0001",
            expected_channel_revision=int(channel["revision"]),
            report=CollaborationReportPayload.model_validate(incomplete_payload),
        ),
    )
    assert (initial["status"], initial["revision"]) == ("needs_more_evidence", 3)
    services = _services(database)

    revised_payload = CollaborationReportPayload.model_validate(
        {
            **_report_payload(report_id),
            "summary": "Complete evidence now proves the generic capability gap.",
        }
    )
    revise_request = ReportRevisionRequest(
        idempotency_key="fault-concurrent-report-revise-0001",
        expected_report_revision=int(initial["revision"]),
        report=revised_payload,
    )
    revised = await _concurrent_identical(
        services,
        lambda service: service.revise_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=revise_request,
        ),
    )
    assert all(item == revised[0] for item in revised)
    assert (revised[0]["status"], revised[0]["revision"]) == (
        "awaiting_user_review",
        int(initial["revision"]) + 2,
    )
    drifted_revision = revise_request.model_copy(
        update={
            "report": revised_payload.model_copy(
                update={"summary": "The same key now carries different evidence."}
            )
        }
    )
    with pytest.raises(CollaborationConflict) as revision_conflict:
        await services[0].revise_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=drifted_revision,
        )
    assert revision_conflict.value.status_code == 409

    approval_request = ApprovalDecisionRequest(
        idempotency_key="fault-concurrent-report-approval-0001",
        expected_report_revision=int(revised[0]["revision"]),
        decision="approve",
    )
    approvals = await _concurrent_identical(
        services,
        lambda service: service.decide_report(
            principal=user,
            report_id=report_id,
            request=approval_request,
        ),
    )
    assert all(item == approvals[0] for item in approvals)
    drifted_approval = approval_request.model_copy(
        update={"reason": "The same approval key has a different request body."}
    )
    with pytest.raises(CollaborationConflict) as approval_conflict:
        await services[0].decide_report(
            principal=user,
            report_id=report_id,
            request=drifted_approval,
        )
    assert approval_conflict.value.status_code == 409

    lease = await services[0].acquire_developer_lease(
        principal=developer,
        report_id=report_id,
        request=LeaseAcquireRequest(
            idempotency_key="fault-concurrent-release-acquire-0001",
            expected_report_revision=int(approvals[0]["resulting_report_revision"]),
            owner_id=developer.sender_id,
            ttl_seconds=900,
        ),
    )
    release_request = LeaseReleaseRequest(
        idempotency_key="fault-concurrent-lease-release-0001",
        expected_lease_revision=1,
        owner_id=developer.sender_id,
        reason="Return this work item to the durable developer inbox.",
    )
    releases = await _concurrent_identical(
        services,
        lambda service: service.release_developer_lease(
            principal=developer,
            report_id=report_id,
            request=release_request,
        ),
    )
    assert all(item == releases[0] for item in releases)
    assert releases[0]["lease_id"] == lease["lease_id"]
    assert (releases[0]["status"], releases[0]["revision"]) == ("released", 2)
    with pytest.raises(CollaborationConflict) as release_conflict:
        await services[0].release_developer_lease(
            principal=developer,
            report_id=report_id,
            request=release_request.model_copy(
                update={"reason": "The same release key now has a different reason."}
            ),
        )
    assert release_conflict.value.status_code == 409

    close_request = ChannelCloseRequest(
        idempotency_key="fault-concurrent-channel-close-0001",
        expected_channel_revision=int((await store.get_channel(channel_id))["revision"]),
        reason="Freeze this deterministic collaboration fault run.",
    )
    closes = await _concurrent_identical(
        services,
        lambda service: service.close_channel(
            principal=user,
            channel_id=channel_id,
            request=close_request,
        ),
    )
    assert all(item == closes[0] for item in closes)
    assert (closes[0]["status"], closes[0]["revision"]) == ("closed", 3)
    with pytest.raises(CollaborationConflict) as close_conflict:
        await services[0].close_channel(
            principal=user,
            channel_id=channel_id,
            request=close_request.model_copy(
                update={"reason": "The same close key now has a different reason."}
            ),
        )
    assert close_conflict.value.status_code == 409

    with sqlite3.connect(database) as connection:
        for operation, expected in (
            ("report.revise", 1),
            ("report.approval", 1),
            ("lease.release", 1),
            ("channel.close", 1),
        ):
            assert connection.execute(
                "SELECT COUNT(*) FROM collaboration_operation_receipts "
                "WHERE operation=?",
                (operation,),
            ).fetchone() == (expected,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (5,)


@pytest.mark.asyncio
async def test_frozen_claim_blocks_reports_then_invalidation_allows_new_claim(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    application_id = UUID(channel["application_ids"][0])

    first_message = _verification_claim_message(
        channel_id,
        assignment_id,
        "fault-first-freeze",
    )
    first_record = {
        **deepcopy(first_message["payload"]),
        "idempotency_key": first_message["idempotency_key"],
        "expected_channel_revision": 1,
        "payload": deepcopy(first_message["payload"]),
    }
    first_claim = await store.create_verification_claim(first_record, first_message)
    assert first_claim["status"] == "frozen"

    blocked_report_id = uuid4()
    blocked_write = _report_write(
        channel_id,
        blocked_report_id,
        expected_channel_revision=2,
    )
    with pytest.raises(CollaborationConflict, match="reports are frozen"):
        await store.create_report(*blocked_write)
    assert await store.list_reports(channel_id=channel_id) == []

    invalidated = await store.invalidate_verification_claims(
        application_id=application_id,
        assignment_id=assignment_id,
        current_draft_revision=2,
        current_content_hash=DIGEST_B,
        reason="the application draft changed after the first frozen claim",
    )
    assert len(invalidated) == 1
    assert (invalidated[0]["status"], invalidated[0]["claim_revision"]) == (
        "invalidated",
        2,
    )

    recovered_report_id = uuid4()
    recovered_record, recovered_message = _report_write(
        channel_id,
        recovered_report_id,
        expected_channel_revision=2,
    )
    recovered_record.update(
        status="withdrawn",
        route="capability_approval",
        visibility="user_and_lilies",
    )
    recovered_report = await store.create_report(recovered_record, recovered_message)
    assert (recovered_report["status"], recovered_report["revision"]) == (
        "withdrawn",
        1,
    )

    second_message = _verification_claim_message(
        channel_id,
        assignment_id,
        "fault-second-freeze",
    )
    second_payload = deepcopy(second_message["payload"])
    second_payload.update(
        application_id=str(application_id),
        draft_revision=2,
        content_hash=DIGEST_B,
        resolved_report_ids=[str(recovered_report_id)],
    )
    second_message["payload"] = second_payload
    second_record = {
        **second_payload,
        "idempotency_key": second_message["idempotency_key"],
        "expected_channel_revision": 3,
        "payload": second_payload,
    }
    second_claim = await store.create_verification_claim(second_record, second_message)
    assert (second_claim["status"], second_claim["claim_revision"]) == ("frozen", 1)
    assert second_claim["draft_revision"] == 2
    assert second_claim["content_hash"] == DIGEST_B

    claims = await store.list_claims(channel_id=channel_id)
    assert [(claim["status"], claim["claim_revision"]) for claim in claims] == [
        ("invalidated", 2),
        ("frozen", 1),
    ]
    messages = await store.list_messages(channel_id, after_seq=0, limit=100)
    assert [message["seq"] for message in messages] == [1, 2, 3, 4, 5]
    assert [message["message_type"] for message in messages] == [
        "control",
        "verification_claim",
        "control",
        "report",
        "verification_claim",
    ]
    assert messages[2]["causal_parent_id"] == messages[1]["message_id"]
    assert messages[4]["payload"]["resolved_report_ids"] == [
        str(recovered_report_id)
    ]
