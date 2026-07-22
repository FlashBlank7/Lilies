from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_platform.collaboration_models import (
    ApprovalDecisionRequest,
    ChannelCloseRequest,
    CollaborationReportPayload,
    DeveloperResponseRequest,
    EnvironmentResponse,
    EnvironmentResponseRequest,
    LeaseAcquireRequest,
    LeaseReleaseRequest,
    LeaseRenewRequest,
    LiliesReprobeResultPayload,
    LiliesReprobeResultRequest,
    ReportSubmitRequest,
    ReportWithdrawalRequest,
    SenderRole,
    VerificationClaim,
    VerificationClaimRequest,
    VerificationResultRequest,
)
from agent_platform.collaboration_service import (
    CollaborationConflict as ServiceConflict,
)
from agent_platform.collaboration_service import (
    CollaborationPrincipal,
    CollaborationService,
    CollaborationNotFound,
)
from agent_platform.collaboration_storage import (
    CollaborationConflict as StorageConflict,
)
from agent_platform.collaboration_storage import CollaborationStore
from agent_platform.lilies_models import CollaborationScope
from tests.test_v04_13_collaboration_sqlite_integration import (
    DIGEST_A,
    DIGEST_B,
    _developer_response_payload,
    _report_payload,
    _store_with_channel,
)


CONFLICTS = (ServiceConflict, StorageConflict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _evidence(
    evidence_id: str,
    *,
    kind: str = "trace",
    digest: str = DIGEST_A,
    label: str = "Real black-box collaboration evidence",
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "kind": kind,
        "digest": digest,
        "media_type": "application/json",
        "label": label,
        "captured_at": _now().isoformat(),
    }


async def _harness(
    tmp_path: Path,
) -> tuple[
    CollaborationService,
    CollaborationStore,
    dict[str, Any],
    CollaborationPrincipal,
    CollaborationPrincipal,
    CollaborationPrincipal,
]:
    store, _, channel = await _store_with_channel(tmp_path)
    service = CollaborationService(
        store=store,
        enabled=True,
        now=_now,
        developer_commit_resolver=lambda commit_sha: commit_sha == "c" * 40,
        developer_evidence_resolver=lambda commit_sha, evidence: (
            commit_sha == "c" * 40 and evidence.evidence_id.startswith("evidence:")
        ),
    )
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=str(channel["lilies_session_id"]),
        scopes=frozenset(
            {
                CollaborationScope.report_write.value,
                CollaborationScope.response_read.value,
            }
        ),
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
    return service, store, channel, lilies, user, developer


def _reprobe(
    *,
    marker: str,
    contract_digest: str,
    evidence_refs: list[dict[str, Any]] | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> LiliesReprobeResultPayload:
    return LiliesReprobeResultPayload.model_validate(
        {
            "schema_version": "1.0",
            "reprobe_id": str(uuid4()),
            "outcome": "lilies_verified",
            "contract_digest": contract_digest,
            "steps": steps
            or [
                {
                    "order": 1,
                    "action": "Refresh the public platform contract.",
                    "expected": "The contract exposes the generic intake operation.",
                }
            ],
            "expected": "The repaired generic route succeeds through the public API.",
            "actual": "The repaired generic route succeeded through the public API.",
            "evidence_refs": evidence_refs
            or [
                _evidence(
                    f"evidence:reprobe-{marker}-0001",
                    kind="test_run",
                    digest=contract_digest,
                )
            ],
        }
    )


def _sender_payload(model: Any, *server_fields: str) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude=set(server_fields))


def _assert_parents_precede_children(messages: list[dict[str, Any]]) -> None:
    prior_ids: set[str] = set()
    assert [int(message["seq"]) for message in messages] == list(
        range(1, len(messages) + 1)
    )
    for message in messages:
        parent = message.get("causal_parent_id")
        if parent is not None:
            assert str(parent) in prior_ids
        prior_ids.add(str(message["message_id"]))


@pytest.mark.asyncio
async def test_lilies_withdrawal_is_terminal_causal_and_idempotent(
    tmp_path: Path,
) -> None:
    service, store, channel, lilies, user, developer = await _harness(tmp_path)
    channel_id = UUID(channel["channel_id"])
    report_id = uuid4()
    await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="state-machine-withdraw-report-0001",
            expected_channel_revision=1,
            report=CollaborationReportPayload.model_validate(_report_payload(report_id)),
        ),
    )
    request = ReportWithdrawalRequest(
        idempotency_key="state-machine-withdraw-report-terminal-0001",
        expected_report_revision=3,
        reason="Fresh public evidence showed that this capability request is obsolete.",
    )
    withdrawn = await service.withdraw_report(
        principal=lilies,
        channel_id=channel_id,
        report_id=report_id,
        request=request,
    )
    assert (withdrawn["status"], withdrawn["revision"]) == ("withdrawn", 4)
    assert (
        await service.withdraw_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=request,
        )
        == withdrawn
    )

    with pytest.raises(CONFLICTS):
        await service.withdraw_report(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=request.model_copy(update={"reason": "A different withdrawal reason."}),
        )
    with pytest.raises(ServiceConflict) as approval_rejected:
        await service.decide_report(
            principal=user,
            report_id=report_id,
            request=ApprovalDecisionRequest(
                idempotency_key="state-machine-withdraw-approval-0001",
                expected_report_revision=4,
                decision="approve",
            ),
        )
    assert approval_rejected.value.code == "report_not_awaiting_review"
    with pytest.raises((CollaborationNotFound, ServiceConflict)):
        await service.acquire_developer_lease(
            principal=developer,
            report_id=report_id,
            request=LeaseAcquireRequest(
                idempotency_key="state-machine-withdraw-lease-0001",
                expected_report_revision=4,
                owner_id=developer.sender_id,
            ),
        )

    exported = await service.export_causal_chain(principal=user, channel_id=channel_id)
    chain = [
        message
        for message in exported["export"]["messages"]
        if message["correlation_id"] == str(report_id)
    ]
    assert [message["message_type"] for message in chain] == ["report", "control"]
    assert chain[1]["causal_parent_id"] == chain[0]["message_id"]
    assert any(
        record["event_type"] == "collaboration.report_withdrawn"
        for record in exported["export"]["audit"]
    )


@pytest.mark.asyncio
async def test_manual_capability_report_reaches_post_close_independent_verification(
    tmp_path: Path,
) -> None:
    service, store, channel, lilies, user, developer = await _harness(tmp_path)
    channel_id = UUID(channel["channel_id"])
    assignment_id = UUID(channel["assignment_id"])
    report_id = uuid4()
    report_payload = CollaborationReportPayload.model_validate(
        _report_payload(report_id)
    )

    submitted = await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="state-machine-capability-report-0001",
            expected_channel_revision=1,
            report=report_payload,
        ),
    )
    assert (submitted["status"], submitted["route"], submitted["revision"]) == (
        "awaiting_user_review",
        "capability_approval",
        3,
    )

    private_inbox = await service.developer_inbox(
        principal=developer,
        after=0,
        limit=50,
        route=None,
    )
    assert private_inbox == {
        "reports": [],
        "claims": [],
        "pending_user_action": True,
        "next_cursor": 0,
    }
    private_projection = json.dumps(private_inbox, ensure_ascii=False, sort_keys=True)
    assert report_payload.summary not in private_projection
    assert str(report_id) not in private_projection

    approval_request = ApprovalDecisionRequest(
        idempotency_key="state-machine-user-approval-0001",
        expected_report_revision=3,
        decision="approve",
    )
    first_approval = await service.decide_report(
        principal=user,
        report_id=report_id,
        request=approval_request,
    )
    replayed_approvals = await asyncio.gather(
        *(
            service.decide_report(
                principal=user,
                report_id=report_id,
                request=approval_request,
            )
            for _ in range(100)
        )
    )
    assert all(replay == first_approval for replay in replayed_approvals)

    with pytest.raises(CONFLICTS) as changed_cas:
        await service.decide_report(
            principal=user,
            report_id=report_id,
            request=ApprovalDecisionRequest(
                idempotency_key=approval_request.idempotency_key,
                expected_report_revision=4,
                decision="approve",
            ),
        )
    assert changed_cas.value.status_code == 409
    with pytest.raises(ServiceConflict) as stale_cas:
        await service.decide_report(
            principal=user,
            report_id=report_id,
            request=ApprovalDecisionRequest(
                idempotency_key="state-machine-stale-user-approval-0001",
                expected_report_revision=3,
                decision="approve",
            ),
        )
    assert stale_cas.value.code == "report_revision_conflict"

    database = store.db_path
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_approvals WHERE report_id=?",
            (str(report_id),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_outbox "
            "WHERE destination='developer_inbox'",
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_messages "
            "WHERE idempotency_key=?",
            (approval_request.idempotency_key,),
        ).fetchone() == (1,)

    approved_inbox = await service.developer_inbox(
        principal=developer,
        after=0,
        limit=50,
        route=None,
    )
    assert approved_inbox["pending_user_action"] is False
    assert [item["report_id"] for item in approved_inbox["reports"]] == [
        str(report_id)
    ]
    assert approved_inbox["reports"][0]["summary"] == report_payload.summary

    acquire_request = LeaseAcquireRequest(
        idempotency_key="state-machine-lease-acquire-0001",
        expected_report_revision=4,
        owner_id=developer.sender_id,
        ttl_seconds=900,
    )
    acquired = await service.acquire_developer_lease(
        principal=developer,
        report_id=report_id,
        request=acquire_request,
    )
    assert (acquired["status"], acquired["revision"], acquired["report_revision"]) == (
        "active",
        1,
        5,
    )
    assert (
        await service.acquire_developer_lease(
            principal=developer,
            report_id=report_id,
            request=acquire_request,
        )
        == acquired
    )

    renew_request = LeaseRenewRequest(
        idempotency_key="state-machine-lease-renew-0001",
        expected_lease_revision=1,
        owner_id=developer.sender_id,
        ttl_seconds=900,
    )
    renewed = await service.renew_developer_lease(
        principal=developer,
        report_id=report_id,
        request=renew_request,
    )
    assert renewed["revision"] == 2
    assert (
        await service.renew_developer_lease(
            principal=developer,
            report_id=report_id,
            request=renew_request,
        )
        == renewed
    )

    response_payload = _developer_response_payload(
        response_id=uuid4(),
        channel_id=channel_id,
        report_id=report_id,
        report_revision=5,
        created_at=_now(),
    )
    response_request = DeveloperResponseRequest(
        idempotency_key="state-machine-developer-response-0001",
        lease_id=UUID(acquired["lease_id"]),
        lease_owner_id=developer.sender_id,
        expected_report_revision=5,
        response={
            key: value
            for key, value in response_payload.items()
            if key not in {"channel_id", "report_id", "report_revision", "created_at"}
        },
    )
    missing_commit_service = CollaborationService(
        store=store,
        enabled=True,
        now=_now,
        developer_commit_resolver=lambda commit_sha: False,
        developer_evidence_resolver=lambda commit_sha, evidence: True,
    )
    with pytest.raises(ServiceConflict) as missing_commit:
        await missing_commit_service.submit_developer_response(
            principal=developer,
            report_id=report_id,
            request=response_request,
        )
    assert missing_commit.value.code == "developer_commit_not_found"

    missing_evidence_service = CollaborationService(
        store=store,
        enabled=True,
        now=_now,
        developer_commit_resolver=lambda commit_sha: True,
        developer_evidence_resolver=lambda commit_sha, evidence: False,
    )
    with pytest.raises(ServiceConflict) as missing_evidence:
        await missing_evidence_service.submit_developer_response(
            principal=developer,
            report_id=report_id,
            request=response_request,
        )
    assert missing_evidence.value.code == "developer_evidence_not_found"
    assert (await store.get_report(report_id))["status"] == "implementing"
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_developer_responses WHERE report_id=?",
            (str(report_id),),
        ).fetchone() == (0,)

    response = await service.submit_developer_response(
        principal=developer,
        report_id=report_id,
        request=response_request,
    )
    assert response["outcome"] == "implemented"
    ready_report = await store.get_report(report_id)
    assert (ready_report["status"], ready_report["revision"]) == (
        "ready_for_lilies_verification",
        6,
    )

    reprobe = _reprobe(
        marker="capability",
        contract_digest=DIGEST_B,
    )
    reprobe_result = await service.submit_lilies_reprobe(
        principal=lilies,
        channel_id=channel_id,
        report_id=report_id,
        request=LiliesReprobeResultRequest(
            idempotency_key="state-machine-lilies-reprobe-0001",
            expected_report_revision=6,
            result=reprobe,
        ),
    )
    assert reprobe_result["outcome"] == "lilies_verified"
    lilies_verified = await store.get_report(report_id)
    assert (lilies_verified["status"], lilies_verified["revision"]) == (
        "lilies_verified",
        7,
    )

    claim_id = uuid4()
    application_id = UUID(channel["application_ids"][0])
    claim = VerificationClaim.model_validate(
        {
            "schema_version": "1.0",
            "claim_id": str(claim_id),
            "channel_id": str(channel_id),
            "assignment_id": str(assignment_id),
            "application_id": str(application_id),
            "claim_revision": 1,
            "draft_revision": 7,
            "content_hash": DIGEST_A,
            "published_version": 1,
            "test_run_ids": ["test-run:state-machine-0001"],
            "business_run_ids": ["business-run:state-machine-0001"],
            "artifact_refs": [
                _evidence(
                    "evidence:state-machine-artifact-0001",
                    kind="artifact",
                )
            ],
            "host_receipt_refs": [
                _evidence(
                    "evidence:state-machine-host-receipt-0001",
                    kind="host_receipt",
                    digest=DIGEST_B,
                )
            ],
            "resolved_report_ids": [str(report_id)],
            "remaining_limits": [],
            "claim": "ready_for_independent_verification",
            "status": "frozen",
            "created_at": _now().isoformat(),
        }
    )
    frozen = await service.submit_verification_claim(
        principal=lilies,
        channel_id=channel_id,
        request=VerificationClaimRequest(
            idempotency_key="state-machine-verification-claim-0001",
            expected_channel_revision=2,
            claim=_sender_payload(
                claim,
                "channel_id",
                "assignment_id",
                "claim_revision",
                "status",
                "created_at",
                "invalidated_at",
                "invalidation_reason",
            ),
        ),
    )
    assert (frozen["status"], frozen["claim_revision"]) == ("frozen", 1)

    closed = await service.close_channel(
        principal=user,
        channel_id=channel_id,
        request=ChannelCloseRequest(
            idempotency_key="state-machine-channel-close-0001",
            expected_channel_revision=3,
            reason="The formal run ended after its verification claim was frozen.",
        ),
    )
    assert closed["status"] == "closed"

    verifier = CollaborationPrincipal(
        role=SenderRole.verifier,
        sender_id="independent-verifier",
        scopes=frozenset({"collaboration.verify"}),
        channel_id=channel_id,
        assignment_id=assignment_id,
    )
    verification = {
        "schema_version": "1.0",
        "verification_id": str(uuid4()),
        "verdict": "independently_verified",
        "oracle_digest": DIGEST_B,
        "differences": [],
        "evidence_refs": [
            _evidence(
                "evidence:independent-oracle-0001",
                kind="contract",
                digest=DIGEST_B,
            )
        ],
    }
    post_close = await service.submit_verification_result(
        principal=verifier,
        claim_id=claim_id,
        request=VerificationResultRequest(
            idempotency_key="state-machine-verification-result-0001",
            expected_claim_revision=1,
            result=verification,
        ),
    )
    assert post_close["verdict"] == "independently_verified"
    resolved_report = await store.get_report(report_id)
    resolved_claim = await store.get_claim(claim_id)
    assert (resolved_report["status"], resolved_report["route"]) == (
        "independently_verified",
        "verifier",
    )
    assert (resolved_report["revision"], resolved_claim["claim_revision"]) == (8, 2)
    assert resolved_claim["status"] == "independently_verified"

    exported = await service.export_causal_chain(
        principal=user,
        channel_id=channel_id,
    )
    messages = exported["export"]["messages"]
    _assert_parents_precede_children(messages)
    report_messages = [
        message
        for message in messages
        if message["correlation_id"] == str(report_id)
    ]
    assert [message["message_type"] for message in report_messages] == [
        "report",
        "approval",
        "developer_response",
        "control",
    ]
    for parent, child in zip(
        report_messages[:-1], report_messages[1:], strict=True
    ):
        assert child["causal_parent_id"] == parent["message_id"]
    claim_messages = [
        message
        for message in messages
        if message["correlation_id"] == str(claim_id)
    ]
    assert [message["message_type"] for message in claim_messages] == [
        "verification_claim",
        "verification_result",
    ]
    assert claim_messages[1]["causal_parent_id"] == claim_messages[0]["message_id"]
    report_revisions = [
        revision
        for revision in exported["export"]["report_revisions"]
        if revision["report_id"] == str(report_id)
    ]
    assert [revision["status"] for revision in report_revisions] == [
        "observed",
        "evidence_collecting",
        "awaiting_user_review",
        "approved_for_codex",
        "implementing",
        "ready_for_lilies_verification",
        "lilies_verified",
        "independently_verified",
    ]


@pytest.mark.asyncio
async def test_environment_failure_routes_through_real_health_restore_and_lilies_check(
    tmp_path: Path,
) -> None:
    service, store, channel, lilies, user, developer = await _harness(tmp_path)
    channel_id = UUID(channel["channel_id"])
    report_id = uuid4()
    environment_payload = deepcopy(_report_payload(report_id))
    environment_payload.update(
        {
            "category": "environment_gap",
            "summary": "The specified real inventory host failed its health contract.",
            "actual": "The real host returned an unavailable health response.",
            "requested_outcome": "Restore the specified host and return health evidence.",
        }
    )
    submitted = await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="state-machine-environment-report-0001",
            expected_channel_revision=1,
            report=CollaborationReportPayload.model_validate(environment_payload),
        ),
    )
    assert (submitted["status"], submitted["route"], submitted["revision"]) == (
        "environment_failed",
        "environment",
        1,
    )

    direct_inbox = await service.developer_inbox(
        principal=developer,
        after=0,
        limit=50,
        route="environment",
    )
    assert [item["report_id"] for item in direct_inbox["reports"]] == [
        str(report_id)
    ]
    assert direct_inbox["pending_user_action"] is False

    acquired = await service.acquire_developer_lease(
        principal=developer,
        report_id=report_id,
        request=LeaseAcquireRequest(
            idempotency_key="state-machine-environment-lease-0001",
            expected_report_revision=1,
            owner_id=developer.sender_id,
            ttl_seconds=900,
        ),
    )
    routed = await store.get_report(report_id)
    assert (acquired["report_revision"], routed["status"], routed["revision"]) == (
        2,
        "routed_to_task_author",
        2,
    )

    health_evidence = _evidence(
        "evidence:real-host-health-check-0001",
        kind="health_check",
        digest=DIGEST_B,
        label="Real pinned inventory host health response",
    )
    environment_response = EnvironmentResponse.model_validate(
        {
            "schema_version": "1.0",
            "response_id": str(uuid4()),
            "channel_id": str(channel_id),
            "report_id": str(report_id),
            "report_revision": 2,
            "outcome": "restored",
            "environment_digest": DIGEST_B,
            "summary": "The pinned real host is healthy and accepted a bounded probe.",
            "health_checks": [
                {
                    "test_id": "health:real-inventory-host-0001",
                    "command": "curl --fail http://127.0.0.1:8123/api/health/",
                    "exit_code": 0,
                    "summary": "The pinned real host returned its healthy response.",
                    "evidence_ref": health_evidence,
                }
            ],
            "known_limits": [],
            "created_at": _now().isoformat(),
        }
    )
    restored = await service.submit_environment_response(
        principal=developer,
        report_id=report_id,
        request=EnvironmentResponseRequest(
            idempotency_key="state-machine-environment-response-0001",
            expected_report_revision=2,
            response=_sender_payload(
                environment_response,
                "channel_id",
                "report_id",
                "report_revision",
                "created_at",
            ),
        ),
    )
    assert restored["outcome"] == "restored"
    restored_report = await store.get_report(report_id)
    assert (restored_report["status"], restored_report["revision"]) == (
        "environment_restored",
        3,
    )

    health_steps = [
        {
            "order": index,
            "action": check.command,
            "expected": f"exit_code={check.exit_code}: {check.summary}",
        }
        for index, check in enumerate(environment_response.health_checks, start=1)
    ]
    non_independent_reprobe = _reprobe(
        marker="environment-health-old",
        contract_digest=DIGEST_B,
        evidence_refs=[
            check.evidence_ref.model_dump(mode="json")
            for check in environment_response.health_checks
        ],
        steps=health_steps,
    )
    with pytest.raises(ServiceConflict) as rejected:
        await service.submit_lilies_reprobe(
            principal=lilies,
            channel_id=channel_id,
            report_id=report_id,
            request=LiliesReprobeResultRequest(
                idempotency_key="state-machine-environment-reprobe-stale-0001",
                expected_report_revision=3,
                result=non_independent_reprobe,
            ),
        )
    assert rejected.value.code == "environment_health_evidence_not_independent"

    health_reprobe = _reprobe(
        marker="environment-health",
        contract_digest=DIGEST_B,
        evidence_refs=[
            _evidence(
                "evidence:lilies-real-host-health-recheck-0001",
                kind="test_run",
                digest=DIGEST_B,
                label="Lilies independent pinned-host health rerun",
            )
        ],
        steps=health_steps,
    )
    checked = await service.submit_lilies_reprobe(
        principal=lilies,
        channel_id=channel_id,
        report_id=report_id,
        request=LiliesReprobeResultRequest(
            idempotency_key="state-machine-environment-reprobe-0001",
            expected_report_revision=3,
            result=health_reprobe,
        ),
    )
    assert checked["outcome"] == "lilies_verified"
    final_report = await store.get_report(report_id)
    assert (final_report["status"], final_report["revision"]) == (
        "lilies_health_checks",
        4,
    )

    export = await service.export_causal_chain(
        principal=user,
        channel_id=channel_id,
    )
    messages = export["export"]["messages"]
    _assert_parents_precede_children(messages)
    report_messages = [
        message
        for message in messages
        if message["correlation_id"] == str(report_id)
    ]
    assert [message["message_type"] for message in report_messages] == [
        "report",
        "environment_response",
        "control",
    ]
    for parent, child in zip(
        report_messages[:-1], report_messages[1:], strict=True
    ):
        assert child["causal_parent_id"] == parent["message_id"]
    assert export["export"]["environment_responses"][0]["health_checks"][0][
        "evidence_ref"
    ]["kind"] == "health_check"
    revisions = [
        revision
        for revision in export["export"]["report_revisions"]
        if revision["report_id"] == str(report_id)
    ]
    assert [revision["status"] for revision in revisions] == [
        "environment_failed",
        "routed_to_task_author",
        "environment_restored",
        "lilies_health_checks",
    ]
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM collaboration_approvals WHERE report_id=?",
            (str(report_id),),
        ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_developer_inbox_cursor_and_report_share_one_sqlite_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, store, channel, lilies, user, developer = await _harness(tmp_path)
    channel_id = UUID(channel["channel_id"])
    report_id = uuid4()
    report_payload = CollaborationReportPayload.model_validate(
        _report_payload(report_id)
    )
    await service.submit_report(
        principal=lilies,
        channel_id=channel_id,
        request=ReportSubmitRequest(
            idempotency_key="snapshot-submit-report-0001",
            expected_channel_revision=1,
            report=report_payload,
        ),
    )
    await service.decide_report(
        principal=user,
        report_id=report_id,
        request=ApprovalDecisionRequest(
            idempotency_key="snapshot-approve-report-0001",
            expected_report_revision=3,
            decision="approve",
        ),
    )
    approved = await store.get_report(report_id)
    assert (approved["status"], approved["revision"]) == (
        "approved_for_codex",
        4,
    )

    async def do_not_reap_while_the_read_barrier_is_active(*args: Any, **kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(
        store,
        "expire_developer_leases",
        do_not_reap_while_the_read_barrier_is_active,
    )
    original_connect = store._connect
    reader_at_report_select = threading.Event()
    release_reader = threading.Event()
    arm_reader = True
    arm_lock = threading.Lock()

    def instrumented_connect() -> sqlite3.Connection:
        nonlocal arm_reader
        connection = original_connect()
        with arm_lock:
            instrument = arm_reader
            arm_reader = False
        if instrument:
            def pause_after_outbox_snapshot(statement: str) -> None:
                normalized = " ".join(statement.split())
                if normalized.startswith(
                    "SELECT * FROM collaboration_reports WHERE report_id="
                ):
                    reader_at_report_select.set()
                    if not release_reader.wait(timeout=5):
                        raise TimeoutError("developer inbox read barrier timed out")

            connection.set_trace_callback(pause_after_outbox_snapshot)
        return connection

    monkeypatch.setattr(store, "_connect", instrumented_connect)
    first_page_task = asyncio.create_task(
        service.developer_inbox(
            principal=developer,
            after=0,
            limit=1,
            route=None,
        )
    )
    assert await asyncio.to_thread(reader_at_report_select.wait, 5)
    try:
        lease = await service.acquire_developer_lease(
            principal=developer,
            report_id=report_id,
            request=LeaseAcquireRequest(
                idempotency_key="snapshot-acquire-lease-0001",
                expected_report_revision=4,
                owner_id=developer.sender_id,
                ttl_seconds=900,
            ),
        )
        released = await service.release_developer_lease(
            principal=developer,
            report_id=report_id,
            request=LeaseReleaseRequest(
                idempotency_key="snapshot-release-lease-0001",
                expected_lease_revision=1,
                owner_id=developer.sender_id,
                reason="Return this report to the durable developer inbox.",
            ),
        )
        assert lease["status"] == "active"
        assert released["status"] == "released"
    finally:
        release_reader.set()

    first_page = await asyncio.wait_for(first_page_task, timeout=5)
    assert first_page["next_cursor"] == 1
    assert [(item["status"], item["revision"]) for item in first_page["reports"]] == [
        ("approved_for_codex", 4)
    ]

    second_page = await service.developer_inbox(
        principal=developer,
        after=first_page["next_cursor"],
        limit=1,
        route=None,
    )
    assert second_page["next_cursor"] == 2
    assert [(item["status"], item["revision"]) for item in second_page["reports"]] == [
        ("approved_for_codex", 6)
    ]
    final_page = await service.developer_inbox(
        principal=developer,
        after=second_page["next_cursor"],
        limit=1,
        route=None,
    )
    assert final_page["reports"] == []
    assert final_page["next_cursor"] == 2
