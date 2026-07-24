from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from agent_platform.collaboration_models import (
    CollaborationReportPayload,
    ReportRevisionRequest,
    ReportSubmitRequest,
    SenderRole,
)
from agent_platform.collaboration_service import (
    CollaborationBudgetExhausted,
    CollaborationPrincipal,
    CollaborationService,
)
from agent_platform.collaboration_storage import CollaborationStore
from agent_platform.lilies_models import AssignmentMode, CollaborationScope


NOW = datetime(2026, 7, 24, 1, 2, 3, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _evidence(marker: str) -> dict[str, Any]:
    return {
        "evidence_id": f"evidence:formal-budget-{marker}",
        "kind": "trace",
        "digest": "sha256:" + marker[0].lower() * 64,
        "media_type": "application/json",
        "label": f"Formal evidence {marker}",
        "captured_at": NOW.isoformat(),
    }


def _payload(report_id: UUID, marker: str) -> CollaborationReportPayload:
    evidence = _evidence(marker)
    return CollaborationReportPayload.model_validate(
        {
            "report_id": str(report_id),
            "category": "platform_capability_gap",
            "phase": "preflight",
            "severity": "blocking",
            "summary": f"Generic platform evidence gap {marker}.",
            "original_goal": "Build the frozen enterprise workflow.",
            "requirement_digest": DIGEST_A,
            "platform_contract_digest": DIGEST_B,
            "manuals_checked": [
                {
                    "manual_id": "manual:formal-budget",
                    "version": "2026-07-24",
                    "digest": DIGEST_A,
                }
            ],
            "attempted_routes": [
                {
                    "attempt_id": str(
                        uuid5(
                            NAMESPACE_URL,
                            f"lilies:formal-report-budget-attempt:{marker}",
                        )
                    ),
                    "route": "public platform contract",
                    "input_digest": DIGEST_A,
                    "outcome": f"Evidence marker {marker} remains incomplete.",
                    "evidence_refs": [evidence],
                    "attempted_at": NOW.isoformat(),
                }
            ],
            "expected": "The platform exposes the required generic contract.",
            # Keeping actual absent deliberately leaves the capability report in
            # needs_more_evidence so multiple Lilies supplements are legal.
            "actual": None,
            "missing_contract": "A generic typed capability remains absent.",
            "blocking_scope": "The formal workflow cannot finish.",
            "independent_work": ["Continue artifact validation."],
            "workaround_considered": ["Use only documented public contracts."],
            "workaround_loss": "The closest substitute loses required evidence.",
            "requested_outcome": "Expose the missing generic contract.",
            "confidence": 0.95,
            "secret_redactions": [],
            "evidence_refs": [evidence],
        }
    )


async def _setup(
    tmp_path: Path,
    *,
    max_rounds: int,
    cancellations: list[tuple[UUID, str, str]] | None = None,
) -> tuple[
    CollaborationService,
    CollaborationStore,
    CollaborationPrincipal,
    UUID,
    UUID,
]:
    store = CollaborationStore(tmp_path / "collaboration.db")
    await store.initialize()
    activation_time = datetime.now(timezone.utc)

    async def cancel(assignment_id: UUID, key: str, reason: str) -> None:
        if cancellations is not None:
            cancellations.append((assignment_id, key, reason))

    service = CollaborationService(
        store=store,
        enabled=True,
        now=lambda: datetime.now(timezone.utc),
        assignment_cancel_handler=cancel,
    )
    assignment_id = uuid4()
    session_id = uuid4()
    issued = await service.create_formal_channel(
        assignment_mode=AssignmentMode.formal_experiment,
        task_id=f"EXP-REPORT-BUDGET-{uuid4()}",
        task_revision=1,
        assignment_id=assignment_id,
        lilies_session_id=session_id,
        application_ids=[uuid4()],
        collaboration_enabled=True,
        user_notified=True,
        expires_at=activation_time + timedelta(hours=2),
        retention_until=activation_time + timedelta(days=30),
        idempotency_key="activate-formal-report-budget",
        max_report_evidence_rounds=max_rounds,
    )
    channel_id = issued.channel.channel_id
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=str(session_id),
        scopes=frozenset(scope.value for scope in CollaborationScope),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    report_id = uuid4()
    await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="submit-formal-report-budget",
            expected_channel_revision=1,
            report=_payload(report_id, "a"),
        ),
    )
    return service, store, lilies, channel_id, report_id


async def _revise(
    service: CollaborationService,
    store: CollaborationStore,
    lilies: CollaborationPrincipal,
    channel_id: UUID,
    report_id: UUID,
    marker: str,
    *,
    key_marker: str | None = None,
) -> dict[str, Any]:
    current = await store.get_report(report_id)
    return await service.revise_report(
        principal=lilies,
        channel_id=channel_id,
        report_id=report_id,
        request=ReportRevisionRequest(
            idempotency_key=(
                f"revise-formal-report-budget-{key_marker or marker}"
            ),
            expected_report_revision=int(current["revision"]),
            report=_payload(report_id, marker),
        ),
    )


@pytest.mark.asyncio
async def test_formal_report_budget_accepts_n_then_persists_n_plus_one_stop_and_replay(
    tmp_path: Path,
) -> None:
    cancellations: list[tuple[UUID, str, str]] = []
    service, store, lilies, channel_id, report_id = await _setup(
        tmp_path,
        max_rounds=2,
        cancellations=cancellations,
    )

    await _revise(service, store, lilies, channel_id, report_id, "b")
    await _revise(service, store, lilies, channel_id, report_id, "c")
    current = await store.get_report(report_id)
    assert current["revision"] == 7
    active_budget = await store.get_report_evidence_budget(report_id)
    assert active_budget is not None
    assert active_budget["rounds_used"] == 2
    rejected = ReportRevisionRequest(
        idempotency_key="revise-formal-report-budget-d",
        expected_report_revision=int(current["revision"]),
        report=_payload(report_id, "d"),
    )
    with pytest.raises(CollaborationBudgetExhausted) as exhausted:
        await service.revise_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=rejected,
        )
    assert exhausted.value.identifiers["reason"] == "max_report_evidence_rounds"
    budget = await store.get_report_evidence_budget(report_id)
    assert budget is not None
    assert (budget["status"], budget["rounds_used"], budget["max_rounds"]) == (
        "budget_exhausted",
        2,
        2,
    )
    assert (await store.get_channel(channel_id))["status"] == "closed"
    assert cancellations and cancellations[0][0] == lilies.assignment_id

    # The rejected attempt has a durable receipt. Replaying it after restart
    # re-enters the same stop result without consuming another round.
    restarted = CollaborationService(
        store=CollaborationStore(store.db_path),
        enabled=True,
        now=lambda: datetime.now(timezone.utc),
        assignment_cancel_handler=lambda *_: None,
    )
    await restarted.initialize()
    with pytest.raises(CollaborationBudgetExhausted):
        await restarted.revise_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=rejected,
        )
    replayed_budget = await restarted.store.get_report_evidence_budget(report_id)
    assert replayed_budget is not None
    assert replayed_budget["rounds_used"] == 2
    first_export = await restarted.store.export_channel(channel_id)
    second_export = await restarted.store.export_channel(channel_id)
    assert first_export == second_export
    assert first_export["counts"]["report_evidence_budgets"] == 1
    assert first_export["report_evidence_budgets"] == [
        {
            **replayed_budget,
        }
    ]
    assert first_export["watermark"] == {
        "min_message_seq": 1,
        "max_message_seq": 4,
        "next_seq": 5,
        "max_report_evidence_rounds": 2,
        "report_evidence_rounds_used_total": 2,
        "max_report_evidence_rounds_used": 2,
        "budget_exhausted_reports": 1,
    }
    exhausted_receipts = [
        receipt
        for receipt in first_export["operation_receipts"]
        if receipt["response"].get("budget_exhausted") is True
    ]
    assert len(exhausted_receipts) == 1
    assert exhausted_receipts[0]["response"]["reason"] == "max_report_evidence_rounds"


@pytest.mark.asyncio
async def test_report_budget_is_atomic_across_independent_concurrent_writers(
    tmp_path: Path,
) -> None:
    first, first_store, lilies, channel_id, report_id = await _setup(
        tmp_path,
        max_rounds=2,
    )
    await _revise(first, first_store, lilies, channel_id, report_id, "b")
    current = await first_store.get_report(report_id)
    expected_revision = int(current["revision"])

    second_store = CollaborationStore(first_store.db_path)
    await second_store.initialize()
    second = CollaborationService(
        store=second_store,
        enabled=True,
        now=lambda: datetime.now(timezone.utc),
    )
    requests = [
        ReportRevisionRequest(
            idempotency_key=f"concurrent-formal-report-budget-{marker}",
            expected_report_revision=expected_revision,
            report=_payload(report_id, marker),
        )
        for marker in ("c", "d")
    ]
    results = await asyncio.gather(
        first.revise_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=requests[0],
        ),
        second.revise_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=requests[1],
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, dict) for item in results) == 1
    assert sum(isinstance(item, Exception) for item in results) == 1
    budget = await first_store.get_report_evidence_budget(report_id)
    assert budget is not None
    assert (budget["status"], budget["rounds_used"]) == ("active", 2)


@pytest.mark.asyncio
async def test_three_unchanged_evidence_digests_enter_the_same_durable_stop_gate(
    tmp_path: Path,
) -> None:
    cancellations: list[tuple[UUID, str, str]] = []
    service, store, lilies, channel_id, report_id = await _setup(
        tmp_path,
        max_rounds=5,
        cancellations=cancellations,
    )

    # The initial report already binds evidence digest "a". The first two
    # unchanged supplements commit; the third is durably stopped.
    await _revise(
        service, store, lilies, channel_id, report_id, "a", key_marker="a-one"
    )
    await _revise(
        service, store, lilies, channel_id, report_id, "a", key_marker="a-two"
    )
    with pytest.raises(CollaborationBudgetExhausted) as exhausted:
        await _revise(
            service, store, lilies, channel_id, report_id, "a", key_marker="a-three"
        )
    assert (
        exhausted.value.identifiers["reason"]
        == "unchanged_evidence_digest_three_times"
    )
    budget = await store.get_report_evidence_budget(report_id)
    assert budget is not None
    assert (
        budget["status"],
        budget["rounds_used"],
        budget["unchanged_evidence_streak"],
    ) == ("budget_exhausted", 3, 3)
    assert cancellations


@pytest.mark.asyncio
async def test_formal_channel_freezes_report_budget_while_legacy_projection_is_compatible(
    tmp_path: Path,
) -> None:
    _, store, _, channel_id, _ = await _setup(tmp_path, max_rounds=3)
    channel = await store.get_channel(channel_id)
    assert channel["max_report_evidence_rounds"] == 3
    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(
            sqlite3.IntegrityError,
            match="report evidence budget is immutable",
        ):
            connection.execute(
                """
                UPDATE collaboration_channels
                SET max_report_evidence_rounds=4
                WHERE channel_id=?
                """,
                (str(channel_id),),
            )

    # A pre-budget row remains readable after migration but receives no
    # synthetic mutable authority.
    legacy_db = tmp_path / "legacy.db"
    legacy_store = CollaborationStore(legacy_db)
    await legacy_store.initialize()
    with sqlite3.connect(legacy_db) as connection:
        connection.execute(
            """
            INSERT INTO collaboration_channels(
              channel_id,task_id,task_revision,assignment_id,lilies_session_id,
              application_ids_json,approval_mode,max_report_evidence_rounds,
              status,revision,next_seq,metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'manual',NULL,'active',1,1,'{}',?,?)
            """,
            (
                str(uuid4()),
                "LEGACY-REPORT-BUDGET",
                1,
                str(uuid4()),
                str(uuid4()),
                f'["{uuid4()}"]',
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    rows = await legacy_store.list_channels(limit=10)
    assert rows[0]["max_report_evidence_rounds"] is None


@pytest.mark.asyncio
async def test_cancel_handler_failure_cannot_rollback_exhaustion_or_credential_revocation(
    tmp_path: Path,
) -> None:
    service, store, lilies, channel_id, report_id = await _setup(
        tmp_path,
        max_rounds=1,
    )
    await _revise(service, store, lilies, channel_id, report_id, "b")
    current = await store.get_report(report_id)
    rejected = ReportRevisionRequest(
        idempotency_key="revise-formal-report-budget-cancel-failure",
        expected_report_revision=int(current["revision"]),
        report=_payload(report_id, "c"),
    )
    observed_keys: list[str] = []

    async def fail_cancel(_: UUID, key: str, __: str) -> None:
        observed_keys.append(key)
        raise RuntimeError("injected assignment cancellation transport failure")

    service._assignment_cancel_handler = fail_cancel
    with pytest.raises(RuntimeError, match="injected assignment cancellation"):
        await service.revise_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=rejected,
        )

    budget = await store.get_report_evidence_budget(report_id)
    assert budget is not None
    assert (budget["status"], budget["rounds_used"]) == ("budget_exhausted", 1)
    with sqlite3.connect(store.db_path) as connection:
        revoked = connection.execute(
            """
            SELECT revoked_at,revocation_reason
            FROM collaboration_credentials
            WHERE channel_id=?
            """,
            (str(channel_id),),
        ).fetchone()
    assert revoked is not None
    assert revoked[0] is not None
    assert revoked[1] == "formal report evidence budget exhausted"

    replay_keys: list[str] = []

    async def replay_cancel(_: UUID, key: str, __: str) -> None:
        replay_keys.append(key)

    restarted_store = CollaborationStore(store.db_path)
    restarted = CollaborationService(
        store=restarted_store,
        enabled=True,
        now=lambda: datetime.now(timezone.utc),
        assignment_cancel_handler=replay_cancel,
    )
    await restarted.initialize()
    with pytest.raises(CollaborationBudgetExhausted):
        await restarted.revise_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=rejected,
        )
    assert observed_keys == replay_keys
