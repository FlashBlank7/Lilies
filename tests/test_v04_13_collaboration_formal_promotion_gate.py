from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from agent_platform.collaboration_models import (
    ChannelStatus,
    CollaborationChannel,
    DeveloperResponse,
)
from agent_platform.collaboration_service import (
    CollaborationConflict,
    CollaborationService,
)
from tests.test_v04_13_collaboration_sqlite_integration import (
    _developer_response_payload,
)


def _implemented_response(
    channel: CollaborationChannel,
) -> DeveloperResponse:
    return DeveloperResponse.model_validate(
        _developer_response_payload(
            response_id=uuid4(),
            channel_id=channel.channel_id,
            report_id=uuid4(),
            report_revision=1,
            created_at=channel.created_at,
        )
    )


def _channel() -> CollaborationChannel:
    now = datetime.now(timezone.utc)
    return CollaborationChannel(
        channel_id=uuid4(),
        task_id="EXP-LILIES-PROMOTION-GATE-001",
        task_revision=1,
        assignment_id=uuid4(),
        lilies_session_id=uuid4(),
        application_ids=[uuid4()],
        status=ChannelStatus.active,
        created_at=now,
        retention_until=now + timedelta(days=30),
    )


@pytest.mark.asyncio
async def test_frozen_formal_channel_never_falls_back_to_generic_commit_lookup() -> None:
    channel = _channel()
    policy_inputs: list[tuple[str, int, str]] = []
    commit_lookups: list[str] = []

    def formal_policy(persisted: CollaborationChannel) -> bool:
        policy_inputs.append(
            (
                persisted.task_id,
                persisted.task_revision,
                str(persisted.assignment_id),
            )
        )
        return True

    service = CollaborationService(
        store=object(),
        enabled=True,
        developer_commit_resolver=lambda commit_sha: (
            commit_lookups.append(commit_sha) or True
        ),
        developer_evidence_resolver=lambda _commit_sha, _evidence: True,
        developer_promotion_resolver=None,
        require_frozen_verification_evidence=formal_policy,
    )

    with pytest.raises(CollaborationConflict) as rejected:
        await service._require_developer_response_evidence(  # noqa: SLF001
            channel,
            _implemented_response(channel),
        )

    assert rejected.value.code == "developer_promotion_resolver_unavailable"
    assert policy_inputs == [
        (channel.task_id, channel.task_revision, str(channel.assignment_id))
    ]
    assert commit_lookups == []


@pytest.mark.asyncio
async def test_legacy_nonformal_channel_keeps_generic_commit_and_evidence_resolution() -> None:
    channel = _channel()
    commit_lookups: list[str] = []
    evidence_lookups: list[tuple[str, str]] = []

    def resolve_commit(commit_sha: str) -> bool:
        commit_lookups.append(commit_sha)
        return True

    def resolve_evidence(commit_sha: str, evidence: object) -> bool:
        evidence_lookups.append(
            (commit_sha, str(getattr(evidence, "evidence_id")))
        )
        return True

    service = CollaborationService(
        store=object(),
        enabled=True,
        developer_commit_resolver=resolve_commit,
        developer_evidence_resolver=resolve_evidence,
        developer_promotion_resolver=None,
        require_frozen_verification_evidence=False,
    )
    response = _implemented_response(channel)

    await service._require_developer_response_evidence(  # noqa: SLF001
        channel,
        response,
    )

    assert commit_lookups == [response.commit_sha]
    assert evidence_lookups == [
        (response.commit_sha, test.evidence_ref.evidence_id)
        for test in response.tests_run
    ]
