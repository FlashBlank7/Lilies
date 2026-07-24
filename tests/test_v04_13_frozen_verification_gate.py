from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agent_platform.collaboration_models import (
    SenderRole,
    VerificationClaim,
    VerificationClaimPayload,
    VerificationClaimRequest,
    VerificationResultPayload,
    VerificationResultRequest,
    frozen_claim_context_digest,
)
from agent_platform.collaboration_service import (
    CollaborationConflict,
    CollaborationPrincipal,
    CollaborationService,
)
from tests.test_v04_13_collaboration_service import (
    DIGEST_B,
    NOW,
    FakeCollaborationStore,
    _activated_service,
    _claim,
    _claim_input,
    _evidence,
    _verification_request,
)


TASK_PACKAGE_DIGEST = "sha256:" + "1" * 64
ENVIRONMENT_READY_DIGEST = "sha256:" + "2" * 64
ARCHIVE_MANIFEST_DIGEST = "sha256:" + "3" * 64
VERIFICATION_PROCESS_DIGEST = "sha256:" + "4" * 64


def _frozen_claim_input(claim: VerificationClaim) -> dict[str, Any]:
    payload = {
        **_claim_input(claim),
        "schema_version": "1.1",
        "task_package_digest": TASK_PACKAGE_DIGEST,
        "environment_ready_digest": ENVIRONMENT_READY_DIGEST,
        "archive_manifest_digest": ARCHIVE_MANIFEST_DIGEST,
        "verification_process_digest": VERIFICATION_PROCESS_DIGEST,
        "validation_mode": "real_host",
    }
    payload["frozen_context_digest"] = frozen_claim_context_digest(payload)
    return payload


def _frozen_verification_request(
    *,
    claim: VerificationClaim | dict[str, Any],
    idempotency_key: str = "verify-frozen-claim-0001",
) -> VerificationResultRequest:
    frozen_claim = (
        claim
        if isinstance(claim, dict)
        else claim.model_dump(mode="json", exclude_none=True)
    )
    return VerificationResultRequest.model_validate(
        {
            "idempotency_key": idempotency_key,
            "expected_claim_revision": int(frozen_claim["claim_revision"]),
            "result": {
                "schema_version": "1.1",
                "verification_id": str(uuid4()),
                "verdict": "independently_verified",
                "oracle_digest": DIGEST_B,
                "differences": [],
                "evidence_refs": [_evidence("evidence:frozen-oracle-result")],
                "task_package_digest": frozen_claim["task_package_digest"],
                "environment_ready_digest": frozen_claim[
                    "environment_ready_digest"
                ],
                "archive_manifest_digest": frozen_claim[
                    "archive_manifest_digest"
                ],
                "frozen_context_digest": frozen_claim["frozen_context_digest"],
                "verification_process_digest": VERIFICATION_PROCESS_DIGEST,
                "validation_mode": frozen_claim["validation_mode"],
            },
        }
    )


async def _strict_service(
    *,
    claim_resolver: Callable[..., bool] | None = None,
    result_resolver: Callable[..., bool] | None = None,
) -> tuple[
    CollaborationService,
    FakeCollaborationStore,
    CollaborationPrincipal,
]:
    _, store, lilies, _, _ = await _activated_service()
    service = CollaborationService(
        store=store,
        enabled=True,
        now=lambda: NOW,
        require_frozen_verification_evidence=True,
        verification_claim_resolver=claim_resolver or (lambda _channel, _claim: True),
        verification_result_resolver=result_resolver
        or (lambda _claim, _result: True),
    )
    return service, store, lilies


def _claim_for_channel(
    store: FakeCollaborationStore,
    lilies: CollaborationPrincipal,
) -> VerificationClaim:
    channel = store.channels[lilies.channel_id]
    return _claim(
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
        application_id=UUID(str(channel["application_ids"][0])),
    )


@pytest.mark.asyncio
async def test_strict_claim_requires_v11_and_trusted_frozen_resolver() -> None:
    resolved: list[tuple[Any, Any]] = []

    async def resolve_claim(channel: Any, claim: Any) -> bool:
        resolved.append((channel, claim))
        return True

    service, store, lilies = await _strict_service(claim_resolver=resolve_claim)
    claim = _claim_for_channel(store, lilies)
    channel_revision = int(store.channels[lilies.channel_id]["revision"])
    messages_before = len(store.messages)

    with pytest.raises(CollaborationConflict):
        await service.submit_verification_claim(
            principal=lilies,
            channel_id=lilies.channel_id,
            request=VerificationClaimRequest(
                idempotency_key="strict-legacy-claim-rejected-0001",
                expected_channel_revision=channel_revision,
                claim=_claim_input(claim),
            ),
        )

    assert store.claims == {}
    assert len(store.messages) == messages_before
    assert resolved == []

    stored = await service.submit_verification_claim(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=VerificationClaimRequest.model_validate(
            {
                "idempotency_key": "strict-frozen-claim-accepted-0001",
                "expected_channel_revision": channel_revision,
                "claim": _frozen_claim_input(claim),
            }
        ),
    )

    assert stored["schema_version"] == "1.1"
    assert stored["task_package_digest"] == TASK_PACKAGE_DIGEST
    assert stored["environment_ready_digest"] == ENVIRONMENT_READY_DIGEST
    assert stored["archive_manifest_digest"] == ARCHIVE_MANIFEST_DIGEST
    assert stored["validation_mode"] == "real_host"
    assert len(resolved) == 1
    resolved_channel, resolved_claim = resolved[0]
    assert resolved_channel.channel_id == lilies.channel_id
    assert resolved_claim.claim_id == claim.claim_id


@pytest.mark.asyncio
async def test_claim_resolver_rejection_is_fail_closed_before_persistence() -> None:
    resolver_calls = 0

    def reject_claim(_channel: Any, _claim: Any) -> bool:
        nonlocal resolver_calls
        resolver_calls += 1
        return False

    service, store, lilies = await _strict_service(claim_resolver=reject_claim)
    claim = _claim_for_channel(store, lilies)
    messages_before = len(store.messages)

    with pytest.raises(CollaborationConflict):
        await service.submit_verification_claim(
            principal=lilies,
            channel_id=lilies.channel_id,
            request=VerificationClaimRequest.model_validate(
                {
                    "idempotency_key": "strict-untrusted-claim-rejected-0001",
                    "expected_channel_revision": store.channels[lilies.channel_id][
                        "revision"
                    ],
                    "claim": _frozen_claim_input(claim),
                }
            ),
        )

    assert resolver_calls == 1
    assert store.claims == {}
    assert len(store.messages) == messages_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("task_package_digest", "sha256:" + "a" * 64),
        ("environment_ready_digest", "sha256:" + "b" * 64),
        ("archive_manifest_digest", "sha256:" + "c" * 64),
        ("verification_process_digest", "sha256:" + "e" * 64),
        ("frozen_context_digest", "sha256:" + "d" * 64),
    ],
)
async def test_strict_result_must_exactly_match_every_frozen_digest(
    field: str,
    replacement: str,
) -> None:
    service, store, lilies = await _strict_service()
    claim = _claim_for_channel(store, lilies)
    stored_claim = await service.submit_verification_claim(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=VerificationClaimRequest.model_validate(
            {
                "idempotency_key": f"freeze-before-{field}-mismatch-0001",
                "expected_channel_revision": store.channels[lilies.channel_id][
                    "revision"
                ],
                "claim": _frozen_claim_input(claim),
            }
        ),
    )
    verifier = CollaborationPrincipal(
        role=SenderRole.verifier,
        sender_id="independent-frozen-verifier",
        scopes=frozenset({"collaboration.verify"}),
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
    )
    request = _frozen_verification_request(
        claim=stored_claim,
        idempotency_key=f"reject-{field}-mismatch-0001",
    )
    mismatched_result = request.result.model_copy(update={field: replacement})
    mismatched_request = request.model_copy(update={"result": mismatched_result})
    messages_before = len(store.messages)

    with pytest.raises(CollaborationConflict):
        await service.submit_verification_result(
            principal=verifier,
            claim_id=claim.claim_id,
            request=mismatched_request,
        )

    assert store.verifications == []
    assert store.claims[claim.claim_id]["status"] == "frozen"
    assert len(store.messages) == messages_before


@pytest.mark.asyncio
async def test_strict_result_requires_matching_mode_and_trusted_resolver() -> None:
    trust_result = False
    resolved: list[tuple[Any, Any]] = []

    async def resolve_result(claim: Any, result: Any) -> bool:
        resolved.append((claim, result))
        return trust_result

    service, store, lilies = await _strict_service(result_resolver=resolve_result)
    claim = _claim_for_channel(store, lilies)
    stored_claim = await service.submit_verification_claim(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=VerificationClaimRequest.model_validate(
            {
                "idempotency_key": "freeze-before-strict-result-0001",
                "expected_channel_revision": store.channels[lilies.channel_id][
                    "revision"
                ],
                "claim": _frozen_claim_input(claim),
            }
        ),
    )
    verifier = CollaborationPrincipal(
        role=SenderRole.verifier,
        sender_id="independent-frozen-verifier",
        scopes=frozenset({"collaboration.verify"}),
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
    )
    correct = _frozen_verification_request(claim=stored_claim)
    messages_before = len(store.messages)

    with pytest.raises(CollaborationConflict):
        await service.submit_verification_result(
            principal=verifier,
            claim_id=claim.claim_id,
            request=correct,
        )

    assert len(resolved) == 1
    assert store.verifications == []
    assert store.claims[claim.claim_id]["status"] == "frozen"
    assert len(store.messages) == messages_before

    trust_result = True
    accepted = await service.submit_verification_result(
        principal=verifier,
        claim_id=claim.claim_id,
        request=correct.model_copy(
            update={"idempotency_key": "verify-frozen-claim-after-trust-0002"}
        ),
    )

    assert accepted["next_claim_status"] == "independently_verified"
    assert len(resolved) == 2
    resolved_claim, resolved_result = resolved[-1]
    assert resolved_claim.claim_id == claim.claim_id
    assert resolved_result.validation_mode == "real_host"
    assert resolved_result.frozen_context_digest == stored_claim[
        "frozen_context_digest"
    ]

    with pytest.raises(ValidationError, match="validation_mode"):
        VerificationResultPayload.model_validate(
            {
                **correct.result.model_dump(mode="json"),
                "validation_mode": "substitute",
            }
        )


@pytest.mark.asyncio
async def test_legacy_persisted_claim_remains_readable_but_cannot_bypass_gate() -> None:
    service, store, lilies = await _strict_service()
    legacy_claim = _claim_for_channel(store, lilies)
    store.claims[legacy_claim.claim_id] = legacy_claim.model_dump(
        mode="json",
        exclude_none=True,
    )

    state = await service.get_lilies_channel_state(
        principal=lilies,
        channel_id=lilies.channel_id,
    )
    assert state["latest_claim_resume"]["claim_id"] == str(legacy_claim.claim_id)
    assert state["latest_claim_resume"]["schema_version"] == "1.0"

    verifier = CollaborationPrincipal(
        role=SenderRole.verifier,
        sender_id="independent-frozen-verifier",
        scopes=frozenset({"collaboration.verify"}),
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
    )
    messages_before = len(store.messages)
    with pytest.raises(CollaborationConflict):
        await service.submit_verification_result(
            principal=verifier,
            claim_id=legacy_claim.claim_id,
            request=_verification_request(
                claim=legacy_claim,
                idempotency_key="legacy-result-cannot-bypass-strict-gate-0001",
            ),
        )

    assert store.verifications == []
    assert store.claims[legacy_claim.claim_id]["status"] == "frozen"
    assert len(store.messages) == messages_before

    with pytest.raises(ValidationError, match="complete frozen context"):
        VerificationClaimPayload.model_validate(
            {
                **_claim_input(legacy_claim),
                "schema_version": "1.1",
            }
        )
