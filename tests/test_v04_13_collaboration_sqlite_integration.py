from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from agent_platform.collaboration_models import (
    CollaborationReportPayload,
    ReaderAckRequest,
    ReportSubmitRequest,
    SenderRole,
)
from agent_platform.collaboration_service import (
    CollaborationPrincipal,
    CollaborationService,
)
from agent_platform.collaboration_storage import (
    CollaborationConflict,
    CollaborationStore,
    CollaborationUnauthorized,
)
from agent_platform.formal_run_archiver import FormalRunArchivePreparationRequest
from agent_platform.collaboration_qualification import command_specs_by_id
from agent_platform.lilies_models import (
    AssignmentMode,
    CollaborationScope,
)
from agent_platform.qualification_fault_recorder import record_fault_iteration


NOW = datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


async def _store_with_channel(
    tmp_path: Path,
) -> tuple[CollaborationStore, Path, dict[str, Any]]:
    database = tmp_path / "collaboration.db"
    store = CollaborationStore(database)
    await store.initialize()
    activation_time = datetime.now(timezone.utc)
    service = CollaborationService(
        store=store,
        enabled=True,
        now=lambda: activation_time,
    )
    task_id = "EXP-LILIES-SQLITE-001"
    assignment_id = uuid4()
    expected_channel_id = uuid5(
        NAMESPACE_URL,
        f"lilies:collaboration:{task_id}:1:{assignment_id}",
    )
    application_id = uuid5(
        NAMESPACE_URL,
        f"lilies:sqlite-application:{expected_channel_id}",
    )
    issued = await service.create_formal_channel(
        assignment_mode=AssignmentMode.formal_experiment,
        task_id=task_id,
        task_revision=1,
        assignment_id=assignment_id,
        lilies_session_id=uuid4(),
        application_ids=[application_id],
        collaboration_enabled=True,
        user_notified=True,
        expires_at=activation_time + timedelta(hours=2),
        retention_until=activation_time + timedelta(days=30),
        idempotency_key="sqlite-formal-channel-activation-0001",
        max_report_evidence_rounds=3,
    )
    channel = issued.channel.model_dump(mode="json", exclude_none=True)
    return store, database, channel


def _control_message(
    channel_id: UUID,
    marker: str,
    *,
    visibility: str = "user_and_lilies",
    causal_parent_id: UUID | None = None,
) -> dict[str, Any]:
    message_id = uuid5(
        NAMESPACE_URL,
        f"lilies:sqlite-control-message:{channel_id}:{marker}",
    )
    control_id = uuid5(
        NAMESPACE_URL,
        f"lilies:sqlite-control-payload:{channel_id}:{marker}",
    )
    return {
        "schema_version": "1.0",
        "message_id": str(message_id),
        "channel_id": str(channel_id),
        "message_type": "control",
        "sender_role": "platform",
        "sender_id": "platform",
        "correlation_id": str(channel_id),
        "causal_parent_id": (
            str(causal_parent_id) if causal_parent_id is not None else None
        ),
        "idempotency_key": f"sqlite-control-{marker}-request",
        "visibility": visibility,
        "payload_schema": "collaboration.control.v1",
        "payload": {
            "schema_version": "1.0",
            "control_id": str(control_id),
            "channel_id": str(channel_id),
            "kind": "channel_reconnected",
            "actor_id": "platform",
            "reason": f"SQLite contract marker {marker} was persisted.",
            "created_at": NOW.isoformat(),
        },
        "evidence_refs": [],
        "created_at": NOW.isoformat(),
    }


def _verification_claim_message(
    channel_id: UUID,
    assignment_id: UUID,
    marker: str,
) -> dict[str, Any]:
    claim_id = uuid5(
        NAMESPACE_URL,
        f"lilies:sqlite-verification-claim:{channel_id}:{marker}",
    )
    payload = {
        "schema_version": "1.0",
        "claim_id": str(claim_id),
        "channel_id": str(channel_id),
        "assignment_id": str(assignment_id),
        "application_id": str(
            uuid5(NAMESPACE_URL, f"lilies:sqlite-application:{channel_id}")
        ),
        "claim_revision": 1,
        "draft_revision": 1,
        "content_hash": DIGEST_A,
        "test_run_ids": ["test-run:sqlite-verifier-0001"],
        "business_run_ids": ["business-run:sqlite-verifier-0001"],
        "artifact_refs": [],
        "host_receipt_refs": [],
        "resolved_report_ids": [],
        "remaining_limits": [],
        "claim": "ready_for_independent_verification",
        "status": "frozen",
        "created_at": NOW.isoformat(),
    }
    return {
        "schema_version": "1.0",
        "message_id": str(
            uuid5(
                NAMESPACE_URL,
                f"lilies:sqlite-verification-message:{channel_id}:{marker}",
            )
        ),
        "channel_id": str(channel_id),
        "message_type": "verification_claim",
        "sender_role": "lilies",
        "sender_id": "lilies-sqlite",
        "correlation_id": str(claim_id),
        "causal_parent_id": None,
        "idempotency_key": f"sqlite-verification-{marker}-request",
        "visibility": "verifier",
        "payload_schema": "collaboration.verification_claim.v1",
        "payload": payload,
        "evidence_refs": [],
        "created_at": NOW.isoformat(),
    }


def _report_payload(report_id: UUID) -> dict[str, Any]:
    evidence = {
        "evidence_id": "evidence:sqlite-report-trace-0001",
        "kind": "trace",
        "digest": DIGEST_A,
        "media_type": "application/json",
        "label": "SQLite collaboration report trace",
        "captured_at": NOW.isoformat(),
    }
    return {
        "schema_version": "1.0",
        "report_id": str(report_id),
        "category": "platform_capability_gap",
        "phase": "preflight",
        "severity": "blocking",
        "summary": "The public catalog lacks a required generic intake block.",
        "original_goal": "Build a verified enterprise intake workflow.",
        "requirement_digest": DIGEST_A,
        "platform_contract_digest": DIGEST_B,
        "manuals_checked": [
            {
                "manual_id": "manual:block-catalog-sqlite",
                "version": "2026-07-23",
                "digest": DIGEST_A,
            }
        ],
        "attempted_routes": [
            {
                "attempt_id": str(uuid4()),
                "route": "workflow block catalog lookup",
                "input_digest": DIGEST_A,
                "outcome": "No documented block satisfied the contract.",
                "evidence_refs": [evidence],
                "attempted_at": NOW.isoformat(),
            }
        ],
        "expected": "The catalog exposes a typed intake contract.",
        "actual": "The required generic contract is absent.",
        "missing_contract": "Typed inputs, outputs, errors, and evidence.",
        "blocking_scope": "Typed intake is blocked; artifact planning can continue.",
        "independent_work": ["Plan the artifact validation branch."],
        "workaround_considered": ["Use the closest documented catalog block."],
        "workaround_loss": "The substitute loses typed validation evidence.",
        "requested_outcome": "Add a generic typed intake contract.",
        "confidence": 0.96,
        "secret_redactions": ["provider_api_key"],
        "evidence_refs": [evidence],
    }


def _report_write(
    channel_id: UUID,
    report_id: UUID,
    *,
    expected_channel_revision: int = 1,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _report_payload(report_id)
    message_id = uuid5(
        NAMESPACE_URL,
        f"lilies:sqlite-report-message:{channel_id}:{report_id}",
    )
    idempotency_key = f"sqlite-report-{report_id}-request"
    record = {
        "report_id": str(report_id),
        "channel_id": str(channel_id),
        "category": payload["category"],
        "phase": payload["phase"],
        "severity": payload["severity"],
        "route": "developer",
        "status": "approved_for_codex",
        "visibility": "approved_developer",
        "payload": payload,
        "expected_channel_revision": expected_channel_revision,
    }
    message = {
        "schema_version": "1.0",
        "message_id": str(message_id),
        "channel_id": str(channel_id),
        "message_type": "report",
        "sender_role": "lilies",
        "sender_id": "lilies-sqlite",
        "correlation_id": str(report_id),
        "causal_parent_id": None,
        "idempotency_key": idempotency_key,
        "visibility": "user_and_lilies",
        "payload_schema": "collaboration.report.v1",
        "payload": payload,
        "evidence_refs": payload["evidence_refs"],
        "created_at": NOW.isoformat(),
    }
    return record, message


def _developer_response_payload(
    *,
    response_id: UUID,
    channel_id: UUID,
    report_id: UUID,
    report_revision: int,
    created_at: datetime,
) -> dict[str, Any]:
    evidence = {
        "evidence_id": f"evidence:sqlite-response-{response_id}",
        "kind": "test_run",
        "digest": DIGEST_A,
        "media_type": "application/json",
        "label": "SQLite lease response test evidence",
        "captured_at": created_at.isoformat(),
    }
    return {
        "schema_version": "1.0",
        "response_id": str(response_id),
        "channel_id": str(channel_id),
        "report_id": str(report_id),
        "report_revision": report_revision,
        "outcome": "implemented",
        "commit_sha": "c" * 40,
        "generic_capability_changes": [
            "Added a generic typed intake contract with durable evidence."
        ],
        "new_contract_digest": DIGEST_B,
        "tests_run": [
            {
                "test_id": "test:sqlite-lease-response-0001",
                "command": "pytest -q tests/test_generic_intake.py",
                "exit_code": 0,
                "summary": "The generic intake contract passed.",
                "evidence_ref": evidence,
            }
        ],
        "browser_or_live_evidence": [],
        "known_limits": [],
        "reprobe_steps": [
            {
                "order": 1,
                "action": "Refresh the public platform contract.",
                "expected": "The contract exposes the generic intake operation.",
            }
        ],
        "created_at": created_at.isoformat(),
    }


def _developer_response_write(
    *,
    channel_id: UUID,
    report_id: UUID,
    source_message_id: UUID,
    lease_id: UUID,
    owner_id: str,
    report_revision: int,
    marker: str,
    created_at: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    response_id = uuid5(
        NAMESPACE_URL,
        f"lilies:sqlite-developer-response:{report_id}:{marker}",
    )
    payload = _developer_response_payload(
        response_id=response_id,
        channel_id=channel_id,
        report_id=report_id,
        report_revision=report_revision,
        created_at=created_at,
    )
    idempotency_key = f"sqlite-developer-response-{marker}-request"
    record = {
        **payload,
        "lease_id": str(lease_id),
        "lease_owner_id": owner_id,
        "expected_report_revision": report_revision,
        "idempotency_key": idempotency_key,
        "payload": payload,
    }
    message = {
        "schema_version": "1.0",
        "message_id": str(uuid4()),
        "channel_id": str(channel_id),
        "message_type": "developer_response",
        "sender_role": "codex",
        "sender_id": owner_id,
        "correlation_id": str(report_id),
        "causal_parent_id": str(source_message_id),
        "idempotency_key": idempotency_key,
        "visibility": "user_and_lilies",
        "payload_schema": "collaboration.developer_response.v1",
        "payload": payload,
        "evidence_refs": [],
        "created_at": created_at.isoformat(),
    }
    return record, message


def _user_principal() -> CollaborationPrincipal:
    return CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="studio-user",
        scopes=frozenset(),
    )


@pytest.mark.asyncio
async def test_one_hundred_identical_replays_are_one_durable_result_and_conflict_on_drift(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    request = _control_message(channel_id, "idempotency-0001")

    # Independent store instances model concurrent processes; an in-memory
    # lock cannot be the mechanism that makes this replay safe.
    writers = [CollaborationStore(database) for _ in range(8)]
    results = await asyncio.gather(
        *(writers[index % len(writers)].append_message(request) for index in range(100))
    )

    assert all(result == results[0] for result in results)
    assert results[0]["seq"] == 2
    for iteration, result in enumerate(results, start=1):
        record_fault_iteration(
            lane="idempotency",
            iteration=iteration,
            command_id="q11-q12-idempotency-100",
            command=command_specs_by_id()["q11-q12-idempotency-100"].argv,
            counters={
                "attempted_iterations": 1,
                "stable_replays": 1,
                "duplicate_side_effects": 0,
                "payload_drift_mutations": 0,
            },
            output={
                "message_id": result["message_id"],
                "seq": result["seq"],
                "call_index": iteration,
                "stable_result": result == results[0],
            },
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT next_seq FROM collaboration_channels WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (3,)

    drifted = deepcopy(request)
    drifted["message_id"] = str(uuid4())
    drifted["payload"]["reason"] = "The same key now carries a different payload."
    with pytest.raises(CollaborationConflict) as rejected:
        await store.append_message(drifted)
    assert rejected.value.status_code == 409
    assert await store.get_message(results[0]["message_id"]) == results[0]


@pytest.mark.asyncio
async def test_service_replay_precedes_advanced_channel_cas_across_process_stores(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    principal = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=channel["lilies_session_id"],
        scopes=frozenset(scope.value for scope in CollaborationScope),
        channel_id=channel_id,
        assignment_id=UUID(channel["assignment_id"]),
    )
    payload = CollaborationReportPayload.model_validate(_report_payload(uuid4()))
    request = ReportSubmitRequest(
        idempotency_key="sqlite-service-report-replay-0001",
        expected_channel_revision=channel["revision"],
        report=payload,
    )
    services = [
        CollaborationService(
            store=CollaborationStore(database),
            enabled=True,
        )
        for _ in range(8)
    ]
    results = await asyncio.gather(
        *(
            services[index % len(services)].submit_report(
                principal=principal,
                channel_id=channel_id,
                request=request,
            )
            for index in range(100)
        )
    )
    assert all(result == results[0] for result in results)
    assert results[0]["status"] == "awaiting_user_review"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_reports"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages"
        ).fetchone() == (2,)  # activation + report

    drifted_cas = request.model_copy(
        update={"expected_channel_revision": channel["revision"] + 1}
    )
    with pytest.raises(CollaborationConflict):
        await services[0].submit_report(
            principal=principal,
            channel_id=channel_id,
            request=drifted_cas,
        )

    # The first durable result survives a new service instance after reconnect.
    replayed = await CollaborationService(
        store=store,
        enabled=True,
    ).submit_report(
        principal=principal,
        channel_id=channel_id,
        request=request,
    )
    assert replayed == results[0]


@pytest.mark.asyncio
async def test_formal_archive_intent_replays_exactly_after_channel_close_and_rejects_drift(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    principal = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=channel["lilies_session_id"],
        scopes=frozenset(scope.value for scope in CollaborationScope),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    request = FormalRunArchivePreparationRequest(
        expected_channel_revision=channel["revision"],
        claim_id=uuid4(),
        test_run_ids=["test-run:sqlite-formal-archive-0001"],
        business_run_ids=["business-run:sqlite-formal-archive-0001"],
        summary="Freeze the complete platform-owned formal evidence denominator.",
        idempotency_key="sqlite-formal-archive-intent-0001",
    )
    provider_calls: list[str] = []

    async def freeze_intent(
        bound_channel: Any,
        bound_request: Any,
        actor_id: str,
    ) -> dict[str, Any]:
        provider_calls.append(actor_id)
        assert bound_channel.channel_id == channel_id
        assert bound_request == request
        return {
            "schema_version": "1.0",
            "task_id": bound_channel.task_id,
            "revision": bound_channel.task_revision,
            "run_id": "run:sqlite-formal-archive-0001",
            "assignment_id": str(bound_channel.assignment_id),
            "channel_id": str(bound_channel.channel_id),
            "claim_id": str(bound_request.claim_id),
            "intent_digest": DIGEST_A,
            "state": "awaiting_daemon_completion",
            "accepted_at": NOW.isoformat(),
            "replayed": False,
        }

    service = CollaborationService(
        store=store,
        enabled=True,
        formal_archive_provider=freeze_intent,
    )
    first = await service.prepare_formal_run_archive(
        principal=principal,
        channel_id=channel_id,
        request=request,
    )
    assert provider_calls == [principal.sender_id]

    await store.close_channel(
        channel_id,
        expected_revision=channel["revision"],
        idempotency_key="sqlite-close-after-formal-archive-0001",
        actor_id="studio-user",
        reason="Prove the intent receipt replays before closed-channel validation.",
    )

    async def reject_provider(*_: Any) -> dict[str, Any]:
        raise AssertionError("durable replay must not call the provider")

    restarted = CollaborationService(
        store=CollaborationStore(database),
        enabled=True,
        formal_archive_provider=reject_provider,
    )
    replayed = await restarted.prepare_formal_run_archive(
        principal=principal,
        channel_id=channel_id,
        request=request,
    )
    assert replayed == first

    with pytest.raises(CollaborationConflict):
        await restarted.prepare_formal_run_archive(
            principal=principal,
            channel_id=channel_id,
            request=request.model_copy(
                update={"summary": "The same key now selects another evidence intent."}
            ),
        )


@pytest.mark.asyncio
async def test_independent_sqlite_writers_apply_channel_revision_cas_once(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    second_store = CollaborationStore(database)
    first_report_id = uuid4()
    second_report_id = uuid4()
    first_write = _report_write(channel_id, first_report_id)
    second_write = _report_write(channel_id, second_report_id)

    outcomes = await asyncio.gather(
        store.create_report(*first_write),
        second_store.create_report(*second_write),
        return_exceptions=True,
    )

    successes = [result for result in outcomes if isinstance(result, dict)]
    failures = [result for result in outcomes if isinstance(result, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], CollaborationConflict)
    assert failures[0].status_code == 409
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_reports"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT revision,next_seq FROM collaboration_channels WHERE channel_id=?",
            (str(channel_id),),
        ).fetchone() == (2, 3)


@pytest.mark.asyncio
async def test_durable_reader_cursor_replays_from_ack_not_last_rendered_event(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    for index in range(1, 101):
        await store.append_message(
            _control_message(channel_id, f"reconnect-{index:03d}")
        )

    service = CollaborationService(store=store, enabled=True, now=lambda: NOW)
    user = _user_principal()
    rendered = await service.list_events(
        principal=user,
        channel_id=channel_id,
        after=0,
        limit=65,
    )
    assert [event["seq"] for event in rendered] == list(range(1, 66))
    first_ack = await service.ack_events(
        principal=user,
        channel_id=channel_id,
        request=ReaderAckRequest(
            idempotency_key="sqlite-reader-first-ack-0001",
            expected_cursor_revision=0,
            reader_role=SenderRole.user,
            reader_id=user.sender_id,
            ack_seq=37,
        ),
    )
    assert (first_ack["ack_seq"], first_ack["revision"]) == (37, 1)

    restarted_store = CollaborationStore(database)
    await restarted_store.initialize()
    restarted = CollaborationService(
        store=restarted_store,
        enabled=True,
        now=lambda: NOW,
    )
    resume_after = await restarted.resolve_event_cursor(
        principal=user,
        channel_id=channel_id,
        requested_after=65,
        durable=True,
    )
    assert resume_after == 37

    recovered: list[dict[str, Any]] = []
    while True:
        batch = await restarted.list_events(
            principal=user,
            channel_id=channel_id,
            after=resume_after,
            limit=11,
        )
        if not batch:
            break
        recovered.extend(batch)
        resume_after = int(batch[-1]["seq"])
    assert [event["seq"] for event in recovered] == list(range(38, 102))
    assert len({event["message_id"] for event in recovered}) == 64

    final_ack = await restarted.ack_events(
        principal=user,
        channel_id=channel_id,
        request=ReaderAckRequest(
            idempotency_key="sqlite-reader-final-ack-0001",
            expected_cursor_revision=1,
            reader_role=SenderRole.user,
            reader_id=user.sender_id,
            ack_seq=101,
        ),
    )
    assert (final_ack["ack_seq"], final_ack["revision"]) == (101, 2)

    second_restart_store = CollaborationStore(database)
    await second_restart_store.initialize()
    second_restart = CollaborationService(
        store=second_restart_store,
        enabled=True,
        now=lambda: NOW,
    )
    assert (
        await second_restart.resolve_event_cursor(
            principal=user,
            channel_id=channel_id,
            requested_after=0,
            durable=True,
        )
        == 101
    )
    assert (
        await second_restart.list_events(
            principal=user,
            channel_id=channel_id,
            after=101,
            limit=10,
        )
        == []
    )


@pytest.mark.asyncio
async def test_visibility_is_filtered_in_sql_before_page_limit(tmp_path: Path) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    for marker, visibility in (
        ("only-user", "user_only"),
        ("only-verifier", "verifier"),
        ("only-developer", "approved_developer"),
        ("only-lilies", "user_and_lilies"),
    ):
        message = (
            _verification_claim_message(channel_id, assignment_id, marker)
            if visibility == "verifier"
            else _control_message(channel_id, marker, visibility=visibility)
        )
        await store.append_message(message)

    service = CollaborationService(store=store, enabled=True, now=lambda: NOW)
    principals = {
        "user": _user_principal(),
        "lilies": CollaborationPrincipal(
            role=SenderRole.lilies,
            sender_id="lilies-reader",
            scopes=frozenset(),
            channel_id=channel_id,
            assignment_id=assignment_id,
        ),
        "developer": CollaborationPrincipal(
            role=SenderRole.codex,
            sender_id="codex-reader",
            scopes=frozenset({"collaboration.developer"}),
            channel_id=channel_id,
            assignment_id=assignment_id,
        ),
        "verifier": CollaborationPrincipal(
            role=SenderRole.verifier,
            sender_id="verifier-reader",
            scopes=frozenset({"collaboration.verify"}),
            channel_id=channel_id,
            assignment_id=assignment_id,
        ),
    }

    user_events = await service.list_events(
        principal=principals["user"],
        channel_id=channel_id,
        after=1,
        limit=10,
    )
    assert [event["seq"] for event in user_events] == [2, 3, 4, 5]
    expected = {
        "lilies": (5, "user_and_lilies"),
        "developer": (4, "approved_developer"),
        "verifier": (3, "verifier"),
    }
    for role, (expected_seq, visibility) in expected.items():
        events = await service.list_events(
            principal=principals[role],
            channel_id=channel_id,
            after=1,
            limit=1,
        )
        assert [(event["seq"], event["visibility"]) for event in events] == [
            (expected_seq, visibility)
        ]


@pytest.mark.asyncio
async def test_lilies_history_replay_recovers_own_schema_max_claim_only(
    tmp_path: Path,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    test_run_ids = [f"test-run:{index:03d}:" + ("t" * 140) for index in range(500)]
    business_run_ids = [
        f"business-run:{index:03d}:" + ("b" * 136) for index in range(500)
    ]
    claim_message = _verification_claim_message(
        channel_id,
        assignment_id,
        "schema-max-history-replay",
    )
    claim_message["payload"]["test_run_ids"] = test_run_ids
    claim_message["payload"]["business_run_ids"] = business_run_ids
    other_lilies_claim = _verification_claim_message(
        channel_id,
        assignment_id,
        "other-lilies-private-claim",
    )
    other_lilies_claim["sender_id"] = "other-lilies-reader"
    await store.append_message(other_lilies_claim)
    await store.append_message(claim_message)
    service = CollaborationService(store=store, enabled=True, now=lambda: NOW)
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id="lilies-sqlite",
        scopes=frozenset(),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )

    normal = await service.list_events(
        principal=lilies,
        channel_id=channel_id,
        after=1,
        limit=10,
    )
    replay = await service.list_events(
        principal=lilies,
        channel_id=channel_id,
        after=1,
        limit=1,
        history_replay=True,
    )
    other_lilies_replay = await service.list_events(
        principal=CollaborationPrincipal(
            role=SenderRole.lilies,
            sender_id="unrelated-lilies-reader",
            scopes=frozenset(),
            channel_id=channel_id,
            assignment_id=assignment_id,
        ),
        channel_id=channel_id,
        after=1,
        limit=10,
        history_replay=True,
    )

    assert normal == []
    assert len(replay) == 1
    assert replay[0]["message_type"] == "verification_claim"
    assert replay[0]["sender_role"] == "lilies"
    assert replay[0]["payload"]["test_run_ids"] == test_run_ids
    assert replay[0]["payload"]["business_run_ids"] == business_run_ids
    assert "other-lilies-private-claim" not in repr(replay)
    assert other_lilies_replay == []


@pytest.mark.asyncio
async def test_expired_lease_is_reacquired_and_old_owner_response_is_rejected(
    tmp_path: Path,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    report_id = uuid4()
    report_record, report_message = _report_write(channel_id, report_id)
    created_report = await store.create_report(report_record, report_message)
    source_message_id = UUID(created_report["source_message_id"])
    base = datetime.now(timezone.utc)

    first_lease_id = uuid4()
    first_lease = await store.acquire_developer_lease(
        {
            "lease_id": str(first_lease_id),
            "report_id": str(report_id),
            "report_revision": 1,
            "owner_id": "codex-worker-a",
            "idempotency_key": "sqlite-first-lease-acquire-0001",
        },
        ttl_seconds=60,
        now=base,
        next_report_status="implementing",
    )
    assert first_lease["report_revision"] == 2
    assert (await store.get_report(report_id))["status"] == "implementing"

    # Fault-inject an expired durable deadline without moving the report clock
    # into the future relative to record_developer_response's transaction time.
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE collaboration_developer_leases SET expires_at=? WHERE lease_id=?",
            ((base - timedelta(seconds=1)).isoformat(), str(first_lease_id)),
        )
    expiry_time = datetime.now(timezone.utc)
    assert await store.expire_developer_leases(now=expiry_time) == 1
    available = await store.get_report(report_id)
    assert (available["status"], available["revision"]) == (
        "approved_for_codex",
        3,
    )

    second_lease_id = uuid4()
    second_lease = await store.acquire_developer_lease(
        {
            "lease_id": str(second_lease_id),
            "report_id": str(report_id),
            "report_revision": 3,
            "owner_id": "codex-worker-b",
            "idempotency_key": "sqlite-second-lease-acquire-0001",
        },
        ttl_seconds=60,
        now=datetime.now(timezone.utc),
        next_report_status="implementing",
    )
    assert second_lease["report_revision"] == 4

    late_record, late_message = _developer_response_write(
        channel_id=channel_id,
        report_id=report_id,
        source_message_id=source_message_id,
        lease_id=first_lease_id,
        owner_id="codex-worker-a",
        report_revision=4,
        marker="late-owner-a",
        created_at=datetime.now(timezone.utc),
    )
    with pytest.raises(CollaborationUnauthorized) as rejected:
        await store.record_developer_response(
            late_record,
            next_report_status="ready_for_lilies_verification",
            message=late_message,
        )
    assert rejected.value.status_code == 403
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_developer_responses"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages"
        ).fetchone() == (2,)

    accepted_record, accepted_message = _developer_response_write(
        channel_id=channel_id,
        report_id=report_id,
        source_message_id=source_message_id,
        lease_id=second_lease_id,
        owner_id="codex-worker-b",
        report_revision=4,
        marker="active-owner-b",
        created_at=datetime.now(timezone.utc),
    )
    accepted = await store.record_developer_response(
        accepted_record,
        next_report_status="ready_for_lilies_verification",
        message=accepted_message,
    )
    assert accepted["response_id"] == accepted_record["response_id"]
    final_report = await store.get_report(report_id)
    assert (final_report["status"], final_report["revision"]) == (
        "ready_for_lilies_verification",
        5,
    )
    with sqlite3.connect(database) as connection:
        leases = connection.execute(
            "SELECT owner_id,status FROM collaboration_developer_leases "
            "ORDER BY acquired_at,owner_id"
        ).fetchall()
        assert leases == [
            ("codex-worker-a", "expired"),
            ("codex-worker-b", "released"),
        ]
        assert connection.execute(
            "SELECT seq FROM collaboration_messages ORDER BY seq"
        ).fetchall() == [(1,), (2,), (3,)]


@pytest.mark.asyncio
async def test_causal_export_reads_only_dedicated_collaboration_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, channel = await _store_with_channel(tmp_path)
    channel_id = UUID(channel["channel_id"])
    activation = (await store.list_messages(channel_id, after_seq=0, limit=1))[0]
    root_request = _control_message(channel_id, "causal-root")
    root = await store.append_message(root_request)
    child = await store.append_message(
        _control_message(
            channel_id,
            "causal-child",
            causal_parent_id=UUID(root["message_id"]),
        )
    )

    sentinel = "ordinary-agent-log-must-not-enter-causal-export"
    (tmp_path / "agent-events.jsonl").write_text(sentinel, encoding="utf-8")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE ordinary_agent_events(event_id TEXT PRIMARY KEY,payload TEXT)"
        )
        connection.execute(
            "INSERT INTO ordinary_agent_events(event_id,payload) VALUES (?,?)",
            ("event-0001", sentinel),
        )

    read_tables: set[str] = set()
    original_connect = store._connect

    def dedicated_connection() -> sqlite3.Connection:
        connection = original_connect()

        def authorize(
            action: int,
            table: str | None,
            _column: str | None,
            _database_name: str | None,
            _trigger_name: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and table is not None:
                read_tables.add(table)
                if not (
                    table.startswith("collaboration_")
                    or table.startswith("sqlite_")
                ):
                    return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    original_read_text = Path.read_text

    def reject_log_scan(path: Path, *args: Any, **kwargs: Any) -> str:
        if path.suffix in {".jsonl", ".log"}:
            raise AssertionError(f"causal export attempted to scan ordinary log {path}")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(store, "_connect", dedicated_connection)
    monkeypatch.setattr(Path, "read_text", reject_log_scan)
    service = CollaborationService(store=store, enabled=True, now=lambda: NOW)
    first = await service.export_causal_chain(
        principal=_user_principal(),
        channel_id=channel_id,
    )
    second = await service.export_causal_chain(
        principal=_user_principal(),
        channel_id=channel_id,
    )

    assert first == second
    assert first["counters"]["messages"] == 3
    assert [message["message_id"] for message in first["export"]["messages"]] == [
        activation["message_id"],
        root["message_id"],
        child["message_id"],
    ]
    assert first["export"]["messages"][2]["causal_parent_id"] == root["message_id"]
    assert sentinel not in json.dumps(first, ensure_ascii=False, sort_keys=True)
    assert "collaboration_channels" in read_tables
    assert "collaboration_messages" in read_tables
    assert "ordinary_agent_events" not in read_tables
