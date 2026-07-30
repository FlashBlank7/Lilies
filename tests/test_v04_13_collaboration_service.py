from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_platform.collaboration_models import (
    ApprovalDecisionRequest,
    ChannelCloseRequest,
    ChannelSettingsRequest,
    ClaimStatus,
    CollaborationChannel,
    CollaborationReport,
    CollaborationReportPayload,
    LeaseAcquireRequest,
    ReportStatus,
    ReportSubmitRequest,
    VerificationClaim,
    VerificationClaimRequest,
    VerificationResultRequest,
)
from agent_platform.collaboration_service import (
    CollaborationClosed,
    CollaborationConflict,
    CollaborationNotFound,
    CollaborationPrincipal,
    CollaborationService,
    CollaborationSubscriberOverflow,
)
from agent_platform.lilies_models import AssignmentMode, CollaborationScope
from agent_platform.collaboration_models import SenderRole


NOW = datetime(2026, 7, 23, 1, 2, 3, tzinfo=timezone.utc)
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
COMMIT_SHA = "c" * 40


def _evidence(evidence_id: str = "evidence:service-trace") -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "kind": "trace",
        "digest": DIGEST_A,
        "media_type": "application/json",
        "label": "Black-box collaboration trace",
        "captured_at": NOW.isoformat(),
    }


def _report_payload(*, complete: bool = True) -> CollaborationReportPayload:
    payload: dict[str, Any] = {
        "report_id": str(uuid4()),
        "category": "platform_capability_gap",
        "phase": "preflight",
        "severity": "blocking",
        "summary": "The public catalog lacks a required generic intake block.",
        "original_goal": "Build a verified enterprise intake workflow.",
        "requirement_digest": DIGEST_A,
        "manuals_checked": [],
        "attempted_routes": [],
        "blocking_scope": "Typed intake is blocked; artifact planning can continue.",
        "independent_work": ["Plan the artifact validation branch."],
        "workaround_considered": ["Use the closest documented catalog block."],
        "workaround_loss": "The substitute loses typed validation evidence.",
        "requested_outcome": "Add a generic typed intake contract.",
        "confidence": 0.96,
        "secret_redactions": ["provider_api_key"],
        "evidence_refs": [],
        "missing_contract": "Typed inputs, outputs, errors, and immutable evidence.",
    }
    if complete:
        payload.update(
            {
                "platform_contract_digest": DIGEST_B,
                "manuals_checked": [
                    {
                        "manual_id": "manual:block-catalog",
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
                        "evidence_refs": [_evidence()],
                        "attempted_at": NOW.isoformat(),
                    }
                ],
                "expected": "The catalog exposes a typed intake contract.",
                "actual": "The required generic contract is absent.",
                "evidence_refs": [_evidence()],
            }
        )
    return CollaborationReportPayload.model_validate(payload)


def _claim(
    *,
    channel_id: UUID,
    assignment_id: UUID,
    application_id: UUID | None = None,
    content_hash: str = DIGEST_A,
) -> VerificationClaim:
    return VerificationClaim.model_validate(
        {
            "claim_id": str(uuid4()),
            "channel_id": str(channel_id),
            "assignment_id": str(assignment_id),
            "application_id": str(application_id or uuid4()),
            "claim_revision": 1,
            "draft_revision": 8,
            "content_hash": content_hash,
            "test_run_ids": ["test-run:acceptance-1"],
            "business_run_ids": ["business-run:host-1"],
            "artifact_refs": [_evidence("evidence:claim-artifact")],
            "host_receipt_refs": [_evidence("evidence:claim-host-receipt")],
            "resolved_report_ids": [],
            "remaining_limits": [],
            "claim": "ready_for_independent_verification",
            "status": "frozen",
            "created_at": NOW.isoformat(),
        }
    )


def _verification_request(
    *,
    claim: VerificationClaim,
    idempotency_key: str = "verify-claim-0001",
) -> VerificationResultRequest:
    return VerificationResultRequest.model_validate(
        {
            "idempotency_key": idempotency_key,
            "expected_claim_revision": claim.claim_revision,
            "result": {
                "verification_id": str(uuid4()),
                "verdict": "independently_verified",
                "oracle_digest": DIGEST_B,
                "differences": [],
                "evidence_refs": [_evidence("evidence:oracle-result")],
            },
        }
    )


def _claim_input(claim: VerificationClaim) -> dict[str, Any]:
    return claim.model_dump(
        mode="json",
        exclude={
            "channel_id",
            "assignment_id",
            "claim_revision",
            "status",
            "created_at",
            "invalidated_at",
            "invalidation_reason",
        },
    )


class FakeCollaborationStore:
    """Small semantic fake: persistence details belong to storage tests."""

    def __init__(self) -> None:
        self.channels: dict[UUID, dict[str, Any]] = {}
        self.credentials: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.reports: dict[UUID, dict[str, Any]] = {}
        self.claims: dict[UUID, dict[str, Any]] = {}
        self.active_leases: dict[UUID, dict[str, Any]] = {}
        self.verifications: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        return None

    async def create_channel(self, record: dict[str, Any]) -> dict[str, Any]:
        clean = {key: value for key, value in record.items() if key != "idempotency_key"}
        channel = CollaborationChannel.model_validate(clean)
        stored = channel.model_dump(mode="json", exclude_none=True)
        self.channels.setdefault(channel.channel_id, stored)
        return deepcopy(self.channels[channel.channel_id])

    async def activate_channel(
        self,
        channel_record: dict[str, Any],
        credential_record: dict[str, Any],
        bearer: str,
        message: dict[str, Any],
    ) -> dict[str, Any]:
        channel = await self.create_channel(channel_record)
        credential = await self.provision_credential(credential_record, bearer)
        stored_message = await self.append_message(message)
        return {
            "channel": channel,
            "credential": credential,
            "message": stored_message,
        }

    async def get_channel(self, channel_id: UUID) -> dict[str, Any]:
        return deepcopy(self.channels[channel_id])

    async def provision_credential(
        self, record: dict[str, Any], bearer: str
    ) -> dict[str, Any]:
        stored = deepcopy(record)
        stored["bearer"] = bearer
        self.credentials.append(stored)
        return deepcopy(stored)

    async def authenticate_credential(
        self, bearer: str, *, required_scope: str, channel_id: UUID
    ) -> dict[str, Any]:
        for credential in self.credentials:
            if credential["bearer"] != bearer:
                continue
            if UUID(str(credential["channel_id"])) != channel_id:
                continue
            if required_scope not in credential["scopes"]:
                continue
            return {
                **credential,
                "actor_id": credential.get("actor_id")
                or credential.get("lilies_session_id"),
            }
        raise KeyError("credential not found")

    async def append_message(self, record: dict[str, Any]) -> dict[str, Any]:
        channel_id = UUID(str(record["channel_id"]))
        channel = self.channels[channel_id]
        stored = deepcopy(record)
        stored["seq"] = channel["next_seq"]
        stored["created_at"] = NOW.isoformat()
        channel["next_seq"] += 1
        self.messages.append(stored)
        return deepcopy(stored)

    async def list_messages(
        self,
        channel_id: UUID,
        *,
        after_seq: int,
        limit: int,
        visibilities: list[str] | None,
        lilies_claim_sender_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            deepcopy(message)
            for message in self.messages
            if UUID(str(message["channel_id"])) == channel_id
            and message["seq"] > after_seq
            and (
                visibilities is None
                or message["visibility"] in visibilities
                or (
                    lilies_claim_sender_id is not None
                    and message["visibility"] == "verifier"
                    and message["sender_role"] == "lilies"
                    and message["sender_id"] == lilies_claim_sender_id
                    and message["message_type"] == "verification_claim"
                )
            )
        ][:limit]

    async def get_message_by_idempotency(
        self,
        channel_id: UUID,
        sender_role: str,
        sender_id: str,
        key: str,
        *,
        client_request_digest: str,
    ) -> dict[str, Any] | None:
        for message in self.messages:
            if (
                UUID(str(message["channel_id"])) == channel_id
                and message["sender_role"] == sender_role
                and message["sender_id"] == sender_id
                and message["idempotency_key"] == key
            ):
                if message.get("client_request_digest") != client_request_digest:
                    raise CollaborationConflict(
                        "idempotency_payload_conflict",
                        "idempotency key was reused with another full request",
                    )
                projected = deepcopy(message)
                projected.pop("client_request_digest", None)
                return projected
        return None

    async def create_report(
        self, record: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        await self.append_message(message)
        payload = deepcopy(record["payload"])
        intake_transitions = record.get("intake_transitions", [])
        auto_forward = record.get("auto_forward")
        validated_state = intake_transitions[-1] if intake_transitions else record
        clean = {
            **payload,
            "channel_id": record["channel_id"],
            "source_message_id": message["message_id"],
            "route": (
                auto_forward["next_report_route"]
                if auto_forward is not None
                else validated_state["route"]
            ),
            "status": (
                auto_forward["next_report_status"]
                if auto_forward is not None
                else validated_state["status"]
            ),
            "revision": 1 + len(intake_transitions) + (1 if auto_forward else 0),
            "created_at": NOW.isoformat(),
            "updated_at": NOW.isoformat(),
        }
        report = CollaborationReport.model_validate(clean)
        stored = report.model_dump(mode="json", exclude_none=True)
        self.reports[report.report_id] = stored
        channel = self.channels[report.channel_id]
        channel["revision"] += 1
        return deepcopy(stored)

    async def get_report(self, report_id: UUID) -> dict[str, Any]:
        return deepcopy(self.reports[report_id])

    async def list_reports(self, **_: Any) -> list[dict[str, Any]]:
        # Deliberately return every report. The service boundary, not this fake,
        # must suppress reports that have not been approved for developers.
        return [deepcopy(report) for report in self.reports.values()]

    async def has_pending_user_action(self) -> bool:
        return any(
            report["status"] == ReportStatus.awaiting_user_review.value
            for report in self.reports.values()
        )

    async def expire_developer_leases(self, **_: Any) -> None:
        return None

    async def set_channel_approval_mode(
        self,
        channel_id: UUID,
        *,
        approval_mode: str,
        expected_revision: int,
        resulting_revision: int,
        **_: Any,
    ) -> dict[str, Any]:
        channel = self.channels[channel_id]
        assert channel["revision"] == expected_revision
        channel["approval_mode"] = approval_mode
        channel["revision"] = resulting_revision
        return deepcopy(channel)

    async def record_approval(
        self,
        record: dict[str, Any],
        *,
        next_report_status: str,
        message: dict[str, Any],
        **_: Any,
    ) -> dict[str, Any]:
        report_id = UUID(str(record["report_id"]))
        report = self.reports[report_id]
        assert report["revision"] == record["expected_report_revision"]
        await self.append_message(message)
        report["status"] = next_report_status
        report["route"] = record["next_report_route"]
        report["revision"] += 1
        report["updated_at"] = NOW.isoformat()
        return deepcopy(record)

    async def acquire_developer_lease(
        self, record: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        report_id = UUID(str(record["report_id"]))
        self.active_leases[report_id] = deepcopy(record)
        return deepcopy(record)

    async def get_active_lease(self, report_id: UUID, **_: Any) -> dict[str, Any]:
        return deepcopy(self.active_leases[report_id])

    async def close_channel(
        self, channel_id: UUID, *, expected_revision: int, **_: Any
    ) -> dict[str, Any]:
        channel = self.channels[channel_id]
        assert channel["revision"] == expected_revision
        channel["status"] = "closed"
        channel["closed_at"] = NOW.isoformat()
        channel["revision"] += 1
        return deepcopy(channel)

    async def create_verification_claim(
        self, record: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        await self.append_message(message)
        clean = {
            key: value
            for key, value in record.items()
            if key not in {"expected_channel_revision", "idempotency_key", "outbox"}
        }
        claim = VerificationClaim.model_validate(clean)
        stored = claim.model_dump(mode="json", exclude_none=True)
        self.claims[claim.claim_id] = stored
        return deepcopy(stored)

    async def get_claim(self, claim_id: UUID) -> dict[str, Any]:
        return deepcopy(self.claims[claim_id])

    async def get_latest_claim(self, **_: Any) -> dict[str, Any] | None:
        if not self.claims:
            return None
        return deepcopy(list(self.claims.values())[-1])

    async def list_claims(self, **_: Any) -> list[dict[str, Any]]:
        return [deepcopy(claim) for claim in self.claims.values()]

    async def invalidate_verification_claims(
        self,
        *,
        application_id: UUID,
        assignment_id: UUID | None,
        current_draft_revision: int,
        current_content_hash: str,
        reason: str,
        now: datetime,
    ) -> list[dict[str, Any]]:
        changed: list[dict[str, Any]] = []
        for claim in self.claims.values():
            if UUID(claim["application_id"]) != application_id:
                continue
            if assignment_id is not None and UUID(claim["assignment_id"]) != assignment_id:
                continue
            if claim["status"] != ClaimStatus.frozen.value:
                continue
            if (
                claim["draft_revision"] == current_draft_revision
                and claim["content_hash"] == current_content_hash
            ):
                continue
            claim["status"] = ClaimStatus.invalidated.value
            claim["invalidated_at"] = now.isoformat()
            claim["invalidation_reason"] = reason
            changed.append(deepcopy(claim))
        return changed

    async def record_verification(
        self, record: dict[str, Any], message: dict[str, Any]
    ) -> dict[str, Any]:
        await self.append_message(message)
        claim_id = UUID(str(record["claim_id"]))
        self.claims[claim_id]["status"] = record["next_claim_status"]
        self.verifications.append(deepcopy(record))
        return deepcopy(record)


async def _activated_service(
    *, approval_mode: str = "manual"
) -> tuple[
    CollaborationService,
    FakeCollaborationStore,
    CollaborationPrincipal,
    CollaborationPrincipal,
    CollaborationPrincipal,
]:
    store = FakeCollaborationStore()
    service = CollaborationService(store=store, enabled=True, now=lambda: NOW)
    assignment_id = uuid4()
    lilies_session_id = uuid4()
    application_id = uuid4()
    issued = await service.create_formal_channel(
        assignment_mode=AssignmentMode.formal_experiment,
        task_id="EXP-LILIES-SERVICE-001",
        task_revision=3,
        assignment_id=assignment_id,
        lilies_session_id=lilies_session_id,
        application_ids=[application_id],
        collaboration_enabled=True,
        user_notified=True,
        expires_at=NOW + timedelta(hours=2),
        retention_until=NOW + timedelta(days=30),
        idempotency_key="activate-formal-channel-0001",
        max_report_evidence_rounds=3,
    )
    lilies = CollaborationPrincipal(
        role=SenderRole.lilies,
        sender_id=str(lilies_session_id),
        scopes=frozenset(scope.value for scope in CollaborationScope),
        channel_id=issued.channel.channel_id,
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
    if approval_mode == "auto_forward":
        await service.set_channel_approval_mode(
            principal=user,
            channel_id=issued.channel.channel_id,
            request=ChannelSettingsRequest(
                idempotency_key="enable-auto-forward-0001",
                expected_channel_revision=issued.channel.revision,
                approval_mode="auto_forward",
                confirmed=True,
            ),
        )
    return service, store, lilies, user, developer


@pytest.mark.asyncio
async def test_formal_only_activation_and_channel_bound_lilies_credential() -> None:
    store = FakeCollaborationStore()
    disabled = CollaborationService(store=store, enabled=False, now=lambda: NOW)
    common = {
        "task_id": "EXP-LILIES-SERVICE-ACTIVATION",
        "task_revision": 1,
        "assignment_id": uuid4(),
        "lilies_session_id": uuid4(),
        "application_ids": [uuid4()],
        "collaboration_enabled": True,
        "user_notified": True,
        "expires_at": NOW + timedelta(hours=1),
        "retention_until": NOW + timedelta(days=7),
        "idempotency_key": "formal-activation-0001",
        "max_report_evidence_rounds": 3,
    }
    with pytest.raises(CollaborationNotFound):
        await disabled.create_formal_channel(
            assignment_mode=AssignmentMode.formal_experiment, **common
        )

    service = CollaborationService(store=store, enabled=True, now=lambda: NOW)
    with pytest.raises(CollaborationConflict, match="formal experiments"):
        await service.create_formal_channel(
            assignment_mode=AssignmentMode.customer, **common
        )
    with pytest.raises(CollaborationConflict, match="prior user notice"):
        await service.create_formal_channel(
            assignment_mode=AssignmentMode.formal_experiment,
            **{**common, "user_notified": False},
        )

    issued = await service.create_formal_channel(
        assignment_mode=AssignmentMode.formal_experiment, **common
    )
    token = issued.access_token.get_secret_value()
    principal = await service.authenticate_lilies(
        token,
        channel_id=issued.channel.channel_id,
        required_scope=CollaborationScope.report_write.value,
    )
    assert principal.channel_id == issued.channel.channel_id
    assert principal.assignment_id == common["assignment_id"]
    with pytest.raises(CollaborationNotFound):
        await service.authenticate_lilies(
            token,
            channel_id=uuid4(),
            required_scope=CollaborationScope.report_write.value,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("approval_mode", "complete", "expected_route", "expected_status"),
    [
        ("manual", True, "capability_approval", "awaiting_user_review"),
        ("manual", False, "capability_approval", "needs_more_evidence"),
        ("auto_forward", True, "developer", "approved_for_codex"),
        ("auto_forward", False, "capability_approval", "needs_more_evidence"),
    ],
)
async def test_platform_report_routing_is_schema_and_task_setting_driven(
    approval_mode: str,
    complete: bool,
    expected_route: str,
    expected_status: str,
) -> None:
    service, store, lilies, _, _ = await _activated_service(
        approval_mode=approval_mode
    )
    channel = await store.get_channel(lilies.channel_id)
    result = await service.submit_report(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=ReportSubmitRequest(
            idempotency_key=f"submit-{approval_mode}-{complete}",
            expected_channel_revision=channel["revision"],
            report=_report_payload(complete=complete),
        ),
    )
    assert result["route"] == expected_route
    assert result["status"] == expected_status
    assert result["completeness_issues"] == (
        []
        if complete
        else [
            "attempted_routes",
            "expected",
            "actual",
            "evidence_refs",
            "platform_contract_digest",
            "manuals_checked",
        ]
    )


@pytest.mark.asyncio
async def test_report_replay_precedes_cas_and_compares_the_full_request() -> None:
    service, store, lilies, _, _ = await _activated_service()
    channel = await store.get_channel(lilies.channel_id)
    payload = _report_payload()
    request = ReportSubmitRequest(
        idempotency_key="service-report-replay-0001",
        expected_channel_revision=channel["revision"],
        report=payload,
    )
    first = await service.submit_report(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=request,
    )
    replays = [
        await service.submit_report(
            principal=lilies,
            channel_id=lilies.channel_id,
            request=request,
        )
        for _ in range(100)
    ]
    assert all(item == first for item in replays)
    assert len(store.messages) == 2  # activation + one report

    changed_cas = request.model_copy(
        update={"expected_channel_revision": channel["revision"] + 1}
    )
    with pytest.raises(CollaborationConflict) as rejected:
        await service.submit_report(
            principal=lilies,
            channel_id=lilies.channel_id,
            request=changed_cas,
        )
    assert rejected.value.code == "idempotency_payload_conflict"


@pytest.mark.asyncio
async def test_developer_inbox_hides_all_preapproval_report_content() -> None:
    service, store, lilies, user, developer = await _activated_service()
    channel = await store.get_channel(lilies.channel_id)
    payload = _report_payload(complete=True)
    await service.submit_report(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=ReportSubmitRequest(
            idempotency_key="submit-private-report-0001",
            expected_channel_revision=channel["revision"],
            report=payload,
        ),
    )

    private_inbox = await service.developer_inbox(
        principal=developer, after=0, limit=50, route=None
    )
    assert private_inbox == {
        "reports": [],
        "claims": [],
        "pending_user_action": True,
        "next_cursor": 0,
    }
    assert payload.summary not in repr(private_inbox)
    assert str(payload.report_id) not in repr(private_inbox)

    await service.decide_report(
        principal=user,
        report_id=payload.report_id,
            request=ApprovalDecisionRequest(
                idempotency_key="approve-private-report-0001",
                expected_report_revision=3,
                decision="approve",
            ),
    )
    approved_inbox = await service.developer_inbox(
        principal=developer, after=0, limit=50, route=None
    )
    assert approved_inbox["pending_user_action"] is False
    assert [item["report_id"] for item in approved_inbox["reports"]] == [
        str(payload.report_id)
    ]
    assert approved_inbox["reports"][0]["summary"] == payload.summary


@pytest.mark.asyncio
async def test_verification_failed_report_returns_to_developer_inbox_and_is_leaseable() -> None:
    service, store, lilies, _, developer = await _activated_service(
        approval_mode="auto_forward"
    )
    channel = await store.get_channel(lilies.channel_id)
    payload = _report_payload()
    await service.submit_report(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=ReportSubmitRequest(
            idempotency_key="submit-for-verification-repair-0001",
            expected_channel_revision=channel["revision"],
            report=payload,
        ),
    )
    store.reports[payload.report_id].update(
        {
            "status": ReportStatus.verification_failed.value,
            "revision": 9,
            "updated_at": NOW.isoformat(),
        }
    )

    inbox = await service.developer_inbox(
        principal=developer,
        after=0,
        limit=50,
        route=None,
    )
    assert [item["report_id"] for item in inbox["reports"]] == [
        str(payload.report_id)
    ]
    assert inbox["reports"][0]["status"] == "verification_failed"

    lease = await service.acquire_developer_lease(
        principal=developer,
        report_id=payload.report_id,
        request=LeaseAcquireRequest(
            idempotency_key="reacquire-after-verification-failed-0001",
            expected_report_revision=9,
            owner_id="codex-developer",
        ),
    )
    assert lease["status"] == "active"
    assert lease["report_revision"] == 9


@pytest.mark.asyncio
async def test_lease_owner_body_must_match_authenticated_developer() -> None:
    service, store, lilies, _, developer = await _activated_service(
        approval_mode="auto_forward"
    )
    channel = await store.get_channel(lilies.channel_id)
    payload = _report_payload()
    await service.submit_report(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=ReportSubmitRequest(
            idempotency_key="submit-for-lease-0001",
            expected_channel_revision=channel["revision"],
            report=payload,
        ),
    )
    with pytest.raises(CollaborationNotFound):
        await service.acquire_developer_lease(
            principal=developer,
            report_id=payload.report_id,
            request=LeaseAcquireRequest(
                idempotency_key="spoofed-lease-owner-0001",
                expected_report_revision=1,
                owner_id="another-developer",
            ),
        )
    assert store.active_leases == {}


@pytest.mark.asyncio
async def test_lilies_channel_state_exposes_exact_latest_claim_resume_core() -> None:
    service, store, lilies, _, _ = await _activated_service()
    channel = await store.get_channel(lilies.channel_id)
    test_run_ids = [f"test-run:{index:03d}:" + ("t" * 140) for index in range(500)]
    business_run_ids = [
        f"business-run:{index:03d}:" + ("b" * 136) for index in range(500)
    ]
    claim = _claim(
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
        application_id=UUID(str(channel["application_ids"][0])),
    )
    claim_input = _claim_input(claim)
    claim_input["test_run_ids"] = test_run_ids
    claim_input["business_run_ids"] = business_run_ids
    submitted = await service.submit_verification_claim(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=VerificationClaimRequest.model_validate(
            {
                "idempotency_key": "freeze-max-resume-claim-0001",
                "expected_channel_revision": channel["revision"],
                "claim": claim_input,
            }
        ),
    )

    state = await service.get_lilies_channel_state(
        principal=lilies,
        channel_id=lilies.channel_id,
    )
    resume = state["latest_claim_resume"]

    assert resume["claim_id"] == submitted["claim_id"]
    assert resume["application_id"] == submitted["application_id"]
    assert resume["draft_revision"] == submitted["draft_revision"]
    assert resume["content_hash"] == submitted["content_hash"]
    assert resume["test_run_ids"] == test_run_ids
    assert resume["business_run_ids"] == business_run_ids
    assert resume["artifact_refs"]["count"] == 1
    assert resume["host_receipt_refs"]["count"] == 1
    assert resume["claim_digest"].startswith("sha256:")
    assert "oracle_digest" not in resume
    assert "differences" not in resume


@pytest.mark.asyncio
async def test_draft_mutation_invalidates_frozen_claim_before_verification() -> None:
    service, store, lilies, _, _ = await _activated_service()
    channel = await store.get_channel(lilies.channel_id)
    claim = _claim(
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
        application_id=UUID(str(channel["application_ids"][0])),
    )
    await service.submit_verification_claim(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=VerificationClaimRequest(
            idempotency_key="freeze-claim-0001",
            expected_channel_revision=channel["revision"],
            claim=_claim_input(claim),
        ),
    )
    invalidated = await service.invalidate_claims_for_draft(
        application_id=claim.application_id,
        assignment_id=claim.assignment_id,
        current_draft_revision=claim.draft_revision + 1,
        current_content_hash=DIGEST_B,
        reason="draft content changed after claim freeze",
    )
    assert [item["claim_id"] for item in invalidated] == [str(claim.claim_id)]
    assert invalidated[0]["status"] == "invalidated"

    verifier = CollaborationPrincipal(
        role=SenderRole.verifier,
        sender_id="independent-verifier",
        scopes=frozenset({"collaboration.verify"}),
        channel_id=claim.channel_id,
        assignment_id=claim.assignment_id,
    )
    with pytest.raises(CollaborationConflict) as rejected:
        await service.submit_verification_result(
            principal=verifier,
            claim_id=claim.claim_id,
            request=_verification_request(claim=claim),
        )
    assert rejected.value.code == "claim_not_verifiable"


@pytest.mark.asyncio
async def test_subscriber_overflow_disconnects_notification_only() -> None:
    service, store, lilies, _, _ = await _activated_service()
    subscription = service.subscribe_events(lilies.channel_id, max_queue=2)
    service._notify_events(lilies.channel_id)
    service._notify_events(lilies.channel_id)
    service._notify_events(lilies.channel_id)

    with pytest.raises(CollaborationSubscriberOverflow):
        await service.wait_for_event(subscription, timeout=0.01)

    # The overflow queue carries only wakeups. Durable messages remain intact
    # and reconnect reads them from storage using the persisted cursor.
    assert store.messages
    reconnected = await service.list_events(
        principal=lilies,
        channel_id=lilies.channel_id,
        after=0,
        limit=100,
    )
    assert [event["seq"] for event in reconnected] == [1]
    service.unsubscribe_events(subscription)


@pytest.mark.asyncio
async def test_only_verifier_may_finish_frozen_claim_after_channel_close() -> None:
    service, store, lilies, user, developer = await _activated_service()
    channel = await store.get_channel(lilies.channel_id)
    claim = _claim(
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
        application_id=UUID(str(channel["application_ids"][0])),
    )
    await service.submit_verification_claim(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=VerificationClaimRequest(
            idempotency_key="freeze-before-close-0001",
            expected_channel_revision=channel["revision"],
            claim=_claim_input(claim),
        ),
    )
    await service.close_channel(
        principal=user,
        channel_id=lilies.channel_id,
        request=ChannelCloseRequest(
            idempotency_key="close-with-frozen-claim-0001",
            expected_channel_revision=channel["revision"],
            reason="formal task run ended; preserve independent verification",
        ),
    )
    with pytest.raises(CollaborationNotFound):
        await service.submit_verification_result(
            principal=developer,
            claim_id=claim.claim_id,
            request=_verification_request(claim=claim),
        )

    verifier = CollaborationPrincipal(
        role=SenderRole.verifier,
        sender_id="independent-verifier",
        scopes=frozenset({"collaboration.verify"}),
        channel_id=claim.channel_id,
        assignment_id=claim.assignment_id,
    )
    verified = await service.submit_verification_result(
        principal=verifier,
        claim_id=claim.claim_id,
        request=_verification_request(claim=claim),
    )
    assert verified["next_claim_status"] == "independently_verified"

    second = _claim(
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
        application_id=UUID(str(channel["application_ids"][0])),
    )
    store.claims[second.claim_id] = second.model_dump(mode="json", exclude_none=True)
    store.channels[lilies.channel_id]["status"] = "archived"
    with pytest.raises(CollaborationClosed):
        await service.submit_verification_result(
                principal=verifier,
                claim_id=second.claim_id,
                request=_verification_request(
                    claim=second,
                    idempotency_key="verify-archived-claim-0002",
                ),
            )
