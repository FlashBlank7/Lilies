from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID

from .collaboration_models import (
    ClaimStatus,
    CollaborationChannel,
    VerificationClaim,
    VerificationResultPayload,
    VerificationResultRequest,
)
from .collaboration_service import CollaborationService
from .independent_verifier_broker import run_independent_verifier_subprocess
from .stable_verification_coordinator import StableVerificationCoordinator


class FormalIndependentVerificationError(RuntimeError):
    """A frozen formal claim could not be independently verified."""


class FormalIndependentVerificationService:
    """Run and persist the platform's frozen verifier for one assignment.

    The service owns no oracle logic. It resolves the latest claim from the
    collaboration trust root, invokes the content-addressed verifier process,
    and submits that exact result through the verifier role.
    """

    def __init__(
        self,
        *,
        collaboration: CollaborationService,
        state_root: Path,
        broker_root: Path,
        verifier_token: str,
        hidden_seed_key: str = "",
    ) -> None:
        self._collaboration = collaboration
        self._state_root = Path(state_root)
        self._broker_root = Path(broker_root)
        self._verifier_token = verifier_token
        encoded_seed_key = hidden_seed_key.encode("utf-8")
        self._stable_coordinator = (
            StableVerificationCoordinator(
                state_root=self._state_root,
                broker_root=self._broker_root,
                platform_seed_key_resolver=lambda _context: encoded_seed_key,
            )
            if len(encoded_seed_key) >= 32
            else None
        )

    async def verify_assignment(self, assignment_id: UUID) -> dict[str, Any]:
        claims = await self._collaboration.store.list_claims(
            assignment_id=assignment_id,
            limit=5_000,
        )
        if not claims:
            raise FormalIndependentVerificationError(
                "formal assignment has no frozen verification claim"
            )
        claim = VerificationClaim.model_validate(claims[-1])
        if claim.assignment_id != assignment_id:
            raise FormalIndependentVerificationError(
                "formal verification claim changed its assignment binding"
            )
        if claim.status is ClaimStatus.invalidated:
            raise FormalIndependentVerificationError(
                "formal verification claim was invalidated"
            )
        channel = CollaborationChannel.model_validate(
            await self._collaboration.store.get_channel(claim.channel_id)
        )
        if channel.assignment_id != assignment_id:
            raise FormalIndependentVerificationError(
                "formal verification channel changed its assignment binding"
            )
        try:
            payload = await asyncio.to_thread(
                run_independent_verifier_subprocess,
                state_root=self._state_root,
                task_id=channel.task_id,
                revision=channel.task_revision,
                claim=claim,
                broker_root=self._broker_root,
            )
        except Exception as error:
            raise FormalIndependentVerificationError(
                "frozen independent verifier did not produce a result"
            ) from error
        result = VerificationResultPayload.model_validate(payload)
        try:
            principal = await self._collaboration.authenticate_verifier(
                self._verifier_token,
                claim_id=claim.claim_id,
                required_scope="collaboration.verify",
            )
            stored = await self._collaboration.submit_verification_result(
                principal=principal,
                claim_id=claim.claim_id,
                request=VerificationResultRequest(
                    idempotency_key=f"formal.verification.{claim.claim_id.hex}",
                    expected_claim_revision=claim.claim_revision,
                    result=result,
                ),
            )
            persisted_claim = await self._collaboration.store.get_claim(
                claim.claim_id
            )
        except Exception as error:
            raise FormalIndependentVerificationError(
                "independent verification result could not be persisted"
            ) from error
        stable_progress = None
        if self._stable_coordinator is not None:
            try:
                stable_progress = await asyncio.to_thread(
                    self._stable_coordinator.verify_and_record,
                    task_id=channel.task_id,
                    revision=channel.task_revision,
                    claim=claim,
                )
            except Exception as error:
                raise FormalIndependentVerificationError(
                    "stable verification result could not be recorded"
                ) from error
        return {
            "schema_version": "1.0",
            "assignment_id": str(assignment_id),
            "claim_id": str(claim.claim_id),
            "claim_status": persisted_claim["status"],
            "verification": stored,
            "stable_progress": (
                None
                if stable_progress is None
                else stable_progress.model_dump(mode="json", exclude_none=True)
            ),
        }
