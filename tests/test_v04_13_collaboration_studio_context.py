from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_platform.collaboration_models import (
    ChannelCloseRequest,
    CollaborationChannel,
    LeaseAcquireRequest,
    ReportSubmitRequest,
    SenderRole,
)
from agent_platform.collaboration_service import (
    CollaborationPrincipal,
    CollaborationService,
)
from agent_platform.collaboration_studio_context import (
    build_collaboration_studio_context,
)
from agent_platform.local_lilies_bridge import LocalLiliesRelayEvent
from agent_platform.lilies_models import (
    SessionKind,
    SessionResult,
    SessionStatus,
)
from tests.test_v04_13_collaboration_service import (
    DIGEST_A,
    NOW,
    _activated_service,
    _claim,
    _report_payload,
)
from tests.test_v04_13_collaboration_sqlite_integration import (
    _control_message,
    _store_with_channel,
)


class _BridgeStore:
    def __init__(
        self,
        *,
        assignment: dict[str, Any],
        connection: dict[str, Any],
    ) -> None:
        self.assignment = assignment
        self.connection = connection

    async def get_assignment(self, assignment_id: UUID) -> dict[str, Any]:
        assert assignment_id == UUID(str(self.assignment["assignment_id"]))
        return dict(self.assignment)

    async def get_connection(self, connection_id: UUID) -> dict[str, Any]:
        assert connection_id == UUID(str(self.connection["connection_id"]))
        return dict(self.connection)


class _Bridge:
    def __init__(
        self,
        *,
        assignment: dict[str, Any],
        connection: dict[str, Any],
        session: SessionResult,
        events: list[LocalLiliesRelayEvent],
    ) -> None:
        self.store = _BridgeStore(assignment=assignment, connection=connection)
        self.session = session
        self.events = events

    async def get_assignment_session(self, assignment_id: UUID) -> SessionResult:
        assert assignment_id == self.session.assignment_id
        return self.session

    async def list_events(
        self,
        assignment_id: UUID,
        *,
        after: int,
    ) -> list[LocalLiliesRelayEvent]:
        assert assignment_id == self.session.assignment_id
        assert after == 0
        return list(self.events)


class _MissingWorkflowStorage:
    async def get_application(self, _: str) -> dict[str, Any]:
        raise KeyError("not needed by this context projection test")


def _relay_event(
    *,
    assignment_id: UUID,
    session_id: UUID,
    seq: int,
    event_type: str,
    data: dict[str, Any],
    offset_ms: int = 0,
) -> LocalLiliesRelayEvent:
    return LocalLiliesRelayEvent(
        assignment_id=assignment_id,
        session_id=session_id,
        daemon_seq=seq,
        event_type=event_type,
        data=data,
        received_at=NOW + timedelta(milliseconds=offset_ms),
    )


@pytest.mark.asyncio
async def test_studio_context_projects_formal_task_compaction_tools_and_permissions() -> None:
    _, store, lilies, _, _ = await _activated_service()
    channel = await store.get_channel(lilies.channel_id)
    assignment_id = lilies.assignment_id
    session_id = UUID(str(channel["lilies_session_id"]))
    application_id = UUID(str(channel["application_ids"][0]))
    connection_id = uuid4()
    build_id = uuid4()
    permission_id = uuid4()
    assignment = {
        "assignment_id": assignment_id,
        "application_id": application_id,
        "build_id": build_id,
        "session_id": session_id,
        "connection_id": connection_id,
        "phase": "running",
        "status": "running",
        "desired_state": "running",
        "daemon_status": "waiting_permission",
        "request_json": "{}",
        "submission_json": json.dumps(
            {
                "requirement": "Build and verify the enterprise reconciliation workflow.",
                "business_context": {
                    "business_goal": "Reconcile customer invoices.",
                    "thinking": "private model thought",
                    "private_reason": "private diagnostic",
                },
                "task_package": {
                    "task_id": channel["task_id"],
                    "revision": channel["task_revision"],
                    "public_summary_digest": DIGEST_A,
                },
                "deliverables": [
                    {
                        "name": "reconciliation workbook",
                        "description": "A customer-visible reconciliation workbook.",
                        "media_type": (
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet"
                        ),
                        "required": True,
                    }
                ],
                "platform": {"contract_digest": DIGEST_A},
                "constraints": {
                    "allowed_actions": ["workflow.read", "workflow.write"],
                    "max_turns": 40,
                    "max_tool_calls": 100,
                    "max_budget_usd": 12.5,
                },
            }
        ),
        "created_at": NOW,
        "updated_at": NOW,
    }
    session = SessionResult(
        session_id=session_id,
        assignment_id=assignment_id,
        status=SessionStatus.waiting_permission,
        kind=SessionKind.platform,
        context_summary="已完成输入检查，下一步执行客户数据对账。",
        summary_through_event_seq=81,
        created_at=NOW,
        updated_at=NOW,
    )
    events = [
        _relay_event(
            assignment_id=assignment_id,
            session_id=session_id,
            seq=1,
            event_type="tool.started",
            data={
                "tool": "workflow.test",
                "tool_call_id": "tool-call-001",
                "input": {
                    "application_id": str(application_id),
                    "thinking": "private tool thought",
                    "private_reason": "private tool diagnosis",
                },
            },
        ),
        _relay_event(
            assignment_id=assignment_id,
            session_id=session_id,
            seq=2,
            event_type="tool.completed",
            data={
                "tool": "workflow.test",
                "tool_call_id": "tool-call-001",
                "content": json.dumps(
                    {
                        "ok": True,
                        "operation": "workflow.test",
                        "evidence_refs": ["evidence:test-run-001"],
                        "thinking": "private result thought",
                    }
                ),
            },
            offset_ms=1_250,
        ),
        _relay_event(
            assignment_id=assignment_id,
            session_id=session_id,
            seq=3,
            event_type="model.thinking.delta",
            data={"thinking": "private chain of thought"},
            offset_ms=1_300,
        ),
        _relay_event(
            assignment_id=assignment_id,
            session_id=session_id,
            seq=4,
            event_type="runtime.private_reason.recorded",
            data={"private_reason": "private diagnostic"},
            offset_ms=1_350,
        ),
        _relay_event(
            assignment_id=assignment_id,
            session_id=session_id,
            seq=5,
            event_type="permission.requested",
            data={
                "permission_request": {
                    "request_id": str(permission_id),
                    "tool_name": "connector.execute",
                    "input_digest": DIGEST_A,
                    "redacted_input": {
                        "customer_id": "customer-1042",
                        "thinking": "private permission thought",
                    },
                }
            },
            offset_ms=1_400,
        ),
        _relay_event(
            assignment_id=assignment_id,
            session_id=session_id,
            seq=6,
            event_type="permission.resolved",
            data={
                "request_id": str(permission_id),
                "tool_name": "connector.execute",
                "behavior": "allow",
            },
            offset_ms=1_500,
        ),
    ]
    bridge = _Bridge(
        assignment=assignment,
        connection={"connection_id": connection_id, "status": "connected"},
        session=session,
        events=events,
    )

    projected = await build_collaboration_studio_context(
        channel=CollaborationChannel.model_validate(channel),
        bridge=bridge,
        workflow_storage=_MissingWorkflowStorage(),
    )
    payload = projected.model_dump(mode="json", exclude_none=True)

    assert payload["assignment"]["task_id"] == channel["task_id"]
    assert payload["assignment"]["task_revision"] == channel["task_revision"]
    assert payload["assignment"]["task_package"] == {
        "task_id": channel["task_id"],
        "revision": channel["task_revision"],
        "public_summary_digest": DIGEST_A,
    }
    assert payload["assignment"]["deliverables"] == [
        {
            "name": "reconciliation workbook",
            "description": "A customer-visible reconciliation workbook.",
            "media_type": (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            "required": True,
        }
    ]
    assert payload["assignment"]["compaction"] == {
        "summary": "已完成输入检查，下一步执行客户数据对账。",
        "summary_through_event_seq": 81,
    }

    observable = payload["observable_events"]
    assert [item["seq"] for item in observable] == [1, 2, 5, 6]
    assert observable[1]["duration_ms"] == 1_250
    assert observable[1]["evidence_refs"] == ["evidence:test-run-001"]
    assert observable[2]["kind"] == "permission"
    assert observable[2]["permission_request"]["request_id"] == str(permission_id)
    assert observable[2]["status"] == "pending"
    assert observable[3]["kind"] == "permission"
    assert observable[3]["status"] == "allow"
    assert observable[3]["permission_request_id"] == str(permission_id)
    assert observable[2]["seq"] != observable[3]["seq"]

    encoded = json.dumps(payload, ensure_ascii=False).casefold()
    for private_material in (
        "private model thought",
        "private diagnostic",
        "private tool thought",
        "private tool diagnosis",
        "private result thought",
        "private chain of thought",
        "private permission thought",
    ):
        assert private_material not in encoded


@pytest.mark.asyncio
async def test_channel_detail_includes_active_lease_claim_and_derived_next_owner() -> None:
    service, store, lilies, user, developer = await _activated_service(
        approval_mode="auto_forward"
    )
    channel = await store.get_channel(lilies.channel_id)
    report_payload = _report_payload()
    report = await service.submit_report(
        principal=lilies,
        channel_id=lilies.channel_id,
        request=ReportSubmitRequest(
            idempotency_key="studio-detail-report-0001",
            expected_channel_revision=channel["revision"],
            report=report_payload,
        ),
    )
    lease = await service.acquire_developer_lease(
        principal=developer,
        report_id=report_payload.report_id,
        request=LeaseAcquireRequest(
            idempotency_key="studio-detail-lease-0001",
            expected_report_revision=report["revision"],
            owner_id=developer.sender_id,
        ),
    )
    # As above, real lease read models do not expose write idempotency metadata.
    lease.pop("idempotency_key", None)
    store.active_leases[report_payload.report_id].pop("idempotency_key", None)
    claim = _claim(
        channel_id=lilies.channel_id,
        assignment_id=lilies.assignment_id,
        application_id=UUID(str(channel["application_ids"][0])),
    )
    store.claims[claim.claim_id] = claim.model_dump(mode="json", exclude_none=True)
    # The semantic fake retains the digest used to validate idempotent writes;
    # the real store removes that internal column from message read models.
    for message in store.messages:
        message.pop("client_request_digest", None)
    service._studio_context_provider = lambda _: {
        "assignment": {
            "status": "running",
            "daemon_status": "running",
            "connection_status": "connected",
        }
    }

    detail = await service.get_channel_detail(
        principal=user,
        channel_id=lilies.channel_id,
    )

    assert detail["active_leases"] == [lease]
    assert detail["claims"] == [
        claim.model_dump(mode="json", exclude_none=True)
    ]
    assert detail["derived"]["current_block"]["code"] == "developer_implementation"
    assert detail["derived"]["owner"] == {
        "role": "codex",
        "id": "codex-developer",
        "label": "codex-developer",
    }
    assert "有效租约" in detail["derived"]["why_waiting"]
    assert detail["derived"]["next_action"]["code"] == "developer_response"


@pytest.mark.asyncio
async def test_channel_detail_without_assignment_context_degrades_to_safe_status() -> None:
    service, _, lilies, user, _ = await _activated_service()

    detail = await service.get_channel_detail(
        principal=user,
        channel_id=lilies.channel_id,
    )

    assert "context" not in detail
    assert detail["derived"]["current_block"]["code"] == "lilies_execution"
    assert detail["derived"]["owner"]["role"] == "lilies"

    service._studio_context_provider = lambda _: {"assignment": None}
    detail_with_null_assignment = await service.get_channel_detail(
        principal=user,
        channel_id=lilies.channel_id,
    )

    assert detail_with_null_assignment["context"]["assignment"] is None
    assert (
        detail_with_null_assignment["derived"]["current_block"]["code"]
        == "lilies_execution"
    )


@pytest.mark.asyncio
async def test_concurrent_detail_reads_keep_reader_cursor_monotonic(
    tmp_path: Any,
) -> None:
    store, _, channel = await _store_with_channel(tmp_path)
    service = CollaborationService(
        store=store,
        enabled=True,
        studio_context_provider=lambda _: {"assignment": {}},
    )
    user = CollaborationPrincipal(
        role=SenderRole.user,
        sender_id="local-user",
        scopes=frozenset(),
    )
    channel_id = UUID(str(channel["channel_id"]))
    original_ack = store.ack_reader
    first_ack_started = asyncio.Event()
    second_ack_started = asyncio.Event()
    release_acks = asyncio.Event()
    ack_count = 0

    async def delayed_ack(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal ack_count
        ack_count += 1
        if ack_count == 1:
            first_ack_started.set()
        else:
            second_ack_started.set()
        await release_acks.wait()
        return await original_ack(*args, **kwargs)

    store.ack_reader = delayed_ack  # type: ignore[method-assign]
    first = asyncio.create_task(
        service.get_channel_detail(principal=user, channel_id=channel_id)
    )
    await first_ack_started.wait()
    await store.append_message(_control_message(channel_id, "studio-reader-race"))
    second = asyncio.create_task(
        service.get_channel_detail(principal=user, channel_id=channel_id)
    )
    await second_ack_started.wait()
    release_acks.set()

    first_detail, second_detail = await asyncio.gather(first, second)
    cursor = await store.get_reader_cursor(
        channel_id,
        user.sender_id,
        reader_role=SenderRole.user,
    )

    assert first_detail["channel"]["channel_id"] == str(channel_id)
    assert second_detail["channel"]["channel_id"] == str(channel_id)
    assert cursor["ack_seq"] == 2
    assert cursor["revision"] >= 1


@pytest.mark.asyncio
async def test_close_cancels_assignment_before_persist_and_replay_is_idempotent() -> None:
    service, store, lilies, user, _ = await _activated_service()
    order: list[str] = []
    original_close = store.close_channel

    async def cancel_assignment(
        assignment_id: UUID,
        idempotency_key: str,
        reason: str,
    ) -> None:
        assert assignment_id == lilies.assignment_id
        assert idempotency_key == "collaboration.close.studio-close-0001"
        assert reason == "user ended the formal collaboration task"
        order.append("assignment.cancel")

    async def close_and_record(
        channel_id: UUID,
        **kwargs: Any,
    ) -> dict[str, Any]:
        order.append("channel.persist")
        await store.append_message(kwargs["message"])
        return await original_close(channel_id, **kwargs)

    service._assignment_cancel_handler = cancel_assignment
    store.close_channel = close_and_record  # type: ignore[method-assign]
    channel = await store.get_channel(lilies.channel_id)
    request = ChannelCloseRequest(
        idempotency_key="studio-close-0001",
        expected_channel_revision=channel["revision"],
        reason="user ended the formal collaboration task",
    )

    first = await service.close_channel(
        principal=user,
        channel_id=lilies.channel_id,
        request=request,
    )
    replay = await service.close_channel(
        principal=user,
        channel_id=lilies.channel_id,
        request=request,
    )

    assert order == ["assignment.cancel", "channel.persist"]
    assert replay == first
    assert first["status"] == "closed"


@pytest.mark.asyncio
async def test_close_callback_failure_does_not_persist_a_closed_channel() -> None:
    service, store, lilies, user, _ = await _activated_service()
    persist_calls = 0
    original_close = store.close_channel

    async def failed_cancel(_: UUID, __: str, ___: str) -> None:
        raise RuntimeError("daemon cancellation failed")

    async def counted_close(
        channel_id: UUID,
        **kwargs: Any,
    ) -> dict[str, Any]:
        nonlocal persist_calls
        persist_calls += 1
        return await original_close(channel_id, **kwargs)

    service._assignment_cancel_handler = failed_cancel
    store.close_channel = counted_close  # type: ignore[method-assign]
    channel = await store.get_channel(lilies.channel_id)

    with pytest.raises(RuntimeError, match="daemon cancellation failed"):
        await service.close_channel(
            principal=user,
            channel_id=lilies.channel_id,
            request=ChannelCloseRequest(
                idempotency_key="studio-close-failure-0001",
                expected_channel_revision=channel["revision"],
                reason="must cancel the assignment before closing",
            ),
        )

    unchanged = await store.get_channel(lilies.channel_id)
    assert persist_calls == 0
    assert unchanged["status"] == "active"
    assert unchanged["revision"] == channel["revision"]
