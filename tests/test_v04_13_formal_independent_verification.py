from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_platform.collaboration_models import (
    ClaimStatus,
    CollaborationChannel,
    VerificationClaim,
    VerificationResultPayload,
    VerificationVerdict,
)
from agent_platform.formal_independent_verification import (
    FormalIndependentVerificationError,
    FormalIndependentVerificationService,
)
from agent_platform import formal_independent_verification as verification_module


class FakeStore:
    def __init__(
        self,
        *,
        claim: VerificationClaim | None,
        channel: CollaborationChannel | None,
    ) -> None:
        self.claim = claim
        self.channel = channel

    async def list_claims(self, **_kwargs: Any) -> list[VerificationClaim]:
        return [] if self.claim is None else [self.claim]

    async def get_channel(self, _channel_id: UUID) -> CollaborationChannel:
        assert self.channel is not None
        return self.channel

    async def get_claim(self, _claim_id: UUID) -> dict[str, Any]:
        assert self.claim is not None
        return {
            "status": ClaimStatus.independently_verified.value,
        }


class FakeCollaboration:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.submitted: Any = None

    async def authenticate_verifier(self, token: str, **kwargs: Any) -> str:
        assert token == "verifier-token"
        assert kwargs["required_scope"] == "collaboration.verify"
        return "principal"

    async def submit_verification_result(self, **kwargs: Any) -> dict[str, Any]:
        self.submitted = kwargs
        return {
            "verification_id": str(
                kwargs["request"].result.verification_id
            ),
            "verdict": kwargs["request"].result.verdict.value,
        }


def _claim_and_channel(
    assignment_id: UUID,
) -> tuple[VerificationClaim, CollaborationChannel]:
    channel_id = uuid4()
    claim = VerificationClaim.model_validate(
        {
            "schema_version": "1.0",
            "claim_id": str(uuid4()),
            "application_id": str(uuid4()),
            "draft_revision": 1,
            "content_hash": "sha256:" + "a" * 64,
            "test_run_ids": ["test-run"],
            "business_run_ids": ["business-run"],
            "artifact_refs": [],
            "host_receipt_refs": [],
            "resolved_report_ids": [],
            "remaining_limits": [],
            "claim": "ready_for_independent_verification",
            "channel_id": str(channel_id),
            "assignment_id": str(assignment_id),
            "claim_revision": 1,
            "status": "frozen",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    channel = CollaborationChannel.model_validate(
        {
            "channel_id": str(channel_id),
            "assignment_id": str(assignment_id),
            "task_id": "EXP-LILIES-001",
            "task_revision": 1,
            "lilies_session_id": str(uuid4()),
            "application_ids": [str(claim.application_id)],
            "approval_mode": "manual",
            "max_report_evidence_rounds": 3,
            "status": "active",
            "revision": 1,
            "next_seq": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    return claim, channel


@pytest.mark.asyncio
async def test_service_runs_frozen_broker_and_persists_exact_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assignment_id = uuid4()
    claim, channel = _claim_and_channel(assignment_id)
    collaboration = FakeCollaboration(FakeStore(claim=claim, channel=channel))
    verification_id = uuid4()
    payload = VerificationResultPayload.model_validate(
        {
            "schema_version": "1.0",
            "verification_id": str(verification_id),
            "verdict": VerificationVerdict.independently_verified.value,
            "oracle_digest": "sha256:" + "1" * 64,
            "differences": [],
            "evidence_refs": [
                {
                    "evidence_id": "verification-result",
                    "kind": "archive",
                    "digest": "sha256:" + "2" * 64,
                    "media_type": "application/json",
                    "label": "verification-result.json",
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                }
            ],
        }
    )
    observed: dict[str, Any] = {}

    def fake_broker(**kwargs: Any) -> VerificationResultPayload:
        observed.update(kwargs)
        return payload

    monkeypatch.setattr(
        verification_module,
        "run_independent_verifier_subprocess",
        fake_broker,
    )
    stable_observed: dict[str, Any] = {}

    class FakeProgress:
        def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "stable_hidden_runs": 3,
                "consecutive_passes": 1,
                "progress_digest": "sha256:" + "3" * 64,
            }

    class FakeStableCoordinator:
        def __init__(self, **kwargs: Any) -> None:
            stable_observed["configuration"] = kwargs

        def verify_and_record(self, **kwargs: Any) -> FakeProgress:
            stable_observed["record"] = kwargs
            return FakeProgress()

    monkeypatch.setattr(
        verification_module,
        "StableVerificationCoordinator",
        FakeStableCoordinator,
    )
    service = FormalIndependentVerificationService(
        collaboration=collaboration,  # type: ignore[arg-type]
        state_root=tmp_path / "packages",
        broker_root=tmp_path / "broker",
        verifier_token="verifier-token",
        hidden_seed_key="s" * 48,
    )

    result = await service.verify_assignment(assignment_id)

    assert observed["task_id"] == "EXP-LILIES-001"
    assert observed["revision"] == 1
    assert observed["claim"] == claim
    assert result["claim_status"] == "independently_verified"
    assert collaboration.submitted["request"].result == payload
    assert stable_observed["record"]["claim"] == claim
    assert result["stable_progress"]["consecutive_passes"] == 1


@pytest.mark.asyncio
async def test_service_rejects_assignment_without_a_claim(tmp_path: Path) -> None:
    collaboration = FakeCollaboration(FakeStore(claim=None, channel=None))
    service = FormalIndependentVerificationService(
        collaboration=collaboration,  # type: ignore[arg-type]
        state_root=tmp_path / "packages",
        broker_root=tmp_path / "broker",
        verifier_token="verifier-token",
    )

    with pytest.raises(
        FormalIndependentVerificationError,
        match="no frozen verification claim",
    ):
        await service.verify_assignment(uuid4())
