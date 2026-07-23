from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MethodType
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from agent_platform.lilies_collaboration_client import LiliesCollaborationClient
from agent_platform.lilies_collaboration_tools import register_lilies_collaboration_tools
from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_models import BuildAssignment
from agent_platform.lilies_service import (
    CumulativeMetrics,
    LiliesCollaborationDurabilityError,
    LocalLiliesService,
    TurnMetrics,
)
from agent_platform.lilies_tools import LiliesToolRegistry
from agent_platform.models import ContentBlock, Usage
from tests.test_v04_13_collaboration_daemon_tools import assignment_payload
from tests.test_v04_13_lilies_service import ScriptedLocalProvider


async def _running_turn(
    service: LocalLiliesService,
    *,
    assignment: dict[str, Any] | None = None,
    deadline: datetime | None = None,
) -> tuple[str, str]:
    session_id = str(uuid4())
    await service.storage.create_session(
        session_id=session_id,
        config={"deadline_at": deadline.isoformat()} if deadline else {},
        assignment_id=(str(assignment["assignment_id"]) if assignment else None),
        assignment=assignment,
        platform_contract_digest=(
            str(assignment["platform"]["contract_digest"]) if assignment else None
        ),
    )
    message = await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "Wait for the formal collaboration response."}],
    )
    turn = await service.storage.create_turn(
        session_id,
        request_id=message["id"],
        idempotency_key=f"wait-turn:{uuid4().hex}",
        input_message_id=message["id"],
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )
    return session_id, str(turn["id"])


def _bind_registry(
    service: LocalLiliesService,
    *,
    channel_id: str,
    handler: Any,
) -> LiliesToolRegistry:
    client = LiliesCollaborationClient(
        base_url="http://127.0.0.1:18081",
        access_token="waiting-test-collaboration-token-value",
        channel_id=UUID(channel_id),
        transport=httpx.MockTransport(handler),
    )
    registry = register_lilies_collaboration_tools(LiliesToolRegistry(), client)

    async def tool_registry_for_session(
        self: LocalLiliesService,
        session_id: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> LiliesToolRegistry:
        return registry

    service.tool_registry_for_session = MethodType(  # type: ignore[method-assign]
        tool_registry_for_session, service
    )
    return registry


@pytest.mark.asyncio
async def test_wait_buffers_unrelated_event_then_resumes_and_acks_related_reply(
    tmp_path: Path,
) -> None:
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    report_id = str(uuid4())
    other_report_id = str(uuid4())
    channel_id = str(uuid4())
    session_id, turn_id = await _running_turn(service)
    await service.storage.begin_collaboration_wait(
        turn_id,
        wait_id=f"report:{report_id}",
        pipeline_cursor=0,
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )

    allow_target = False
    ack_seq = 0
    ack_revision = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ack_seq, ack_revision
        if request.url.path.endswith("/acks"):
            payload = json.loads(request.content)
            ack_seq = int(payload["ack_seq"])
            ack_revision += 1
            return httpx.Response(
                200,
                request=request,
                json={"ack_seq": ack_seq, "revision": ack_revision},
            )
        if request.url.path.endswith("/events"):
            events = [
                {
                    "seq": 1,
                    "message_type": "developer_response",
                    "correlation_id": other_report_id,
                    "payload": {"report_id": other_report_id, "status": "ready"},
                }
            ]
            if allow_target:
                events.append(
                    {
                        "seq": 2,
                        "message_type": "developer_response",
                        "correlation_id": report_id,
                        "payload": {
                            "report_id": report_id,
                            "status": "ready_for_lilies_verification",
                        },
                    }
                )
            return httpx.Response(200, request=request, json={"events": events})
        return httpx.Response(
            200,
            request=request,
            json={
                "reader_cursor": {
                    "reader_id": session_id,
                    "ack_seq": ack_seq,
                    "revision": ack_revision,
                }
            },
        )

    _bind_registry(service, channel_id=channel_id, handler=handler)
    watcher = asyncio.create_task(
        service._await_collaboration_updates(session_id, turn_id)
    )
    async with asyncio.timeout(2):
        while int((await service.storage.get_session(session_id))["last_pipeline_cursor"]) < 1:
            await asyncio.sleep(0.01)
    buffered = await service.storage.get_session(session_id)
    assert buffered["status"] == "waiting_collaboration"
    assert buffered["waiting_collaboration_id"] == f"report:{report_id}"

    allow_target = True
    await asyncio.wait_for(watcher, timeout=2)
    resumed = await service.storage.get_session(session_id)
    assert resumed["status"] == "running"
    assert resumed["waiting_collaboration_id"] is None
    assert resumed["last_pipeline_cursor"] == 2
    assert ack_seq == 2
    events = await service.storage.list_events(session_id, after=0, limit=100)
    assert [
        event["event_type"]
        for event in events
        if event["event_type"].startswith("collaboration.")
    ] == [
        "collaboration.waiting",
        "collaboration.buffered",
        "collaboration.resumed",
    ]


@pytest.mark.asyncio
async def test_restart_resumes_same_waiting_turn_report_and_cursor(tmp_path: Path) -> None:
    settings = LiliesSettings(
        data_dir=tmp_path / "lilies",
        workspace_root=tmp_path / "workspaces",
    )
    first = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    await first.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    projection = assignment.model_dump(mode="json", exclude_none=True)
    session_id, turn_id = await _running_turn(first, assignment=projection)
    report_id = str(uuid4())
    await first.storage.begin_collaboration_wait(
        turn_id,
        wait_id=f"report:{report_id}",
        pipeline_cursor=7,
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )

    channel_id = str(assignment.collaboration.channel_id)  # type: ignore[union-attr]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "events": [
                        {
                            "seq": 8,
                            "message_type": "developer_response",
                            "correlation_id": report_id,
                            "payload": {
                                "report_id": report_id,
                                "status": "ready_for_lilies_verification",
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/acks"):
            return httpx.Response(200, request=request, json={"ack_seq": 8, "revision": 2})
        return httpx.Response(
            200,
            request=request,
            json={
                "reader_cursor": {
                    "reader_id": session_id,
                    "ack_seq": 7,
                    "revision": 1,
                }
            },
        )

    resumed = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    _bind_registry(resumed, channel_id=channel_id, handler=handler)
    await resumed.initialize()
    async with asyncio.timeout(3):
        while (await resumed.storage.get_turn(turn_id))["status"] != "completed":
            await asyncio.sleep(0.01)
    session = await resumed.storage.get_session(session_id)
    assert session["status"] == "ready"
    assert session["last_pipeline_cursor"] == 8
    assert session["waiting_collaboration_id"] is None
    messages = await resumed.storage.list_messages_for_compaction(session_id)
    collaboration_messages = [
        message
        for message in messages
        if message["role"] == "user"
        and report_id in json.dumps(message["content"], sort_keys=True)
    ]
    assert collaboration_messages
    assert all(
        message["provenance"] == "collaboration_update"
        for message in collaboration_messages
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [404, 410])
async def test_closed_waiting_channel_records_terminal_update_and_resumes(
    tmp_path: Path,
    status_code: int,
) -> None:
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / str(status_code)),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    session_id, turn_id = await _running_turn(service)
    report_id = str(uuid4())
    await service.storage.begin_collaboration_wait(
        turn_id,
        wait_id=f"report:{report_id}",
        pipeline_cursor=3,
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, request=request, json={"detail": {"code": "gone"}})

    _bind_registry(service, channel_id=str(uuid4()), handler=handler)
    await service._await_collaboration_updates(session_id, turn_id)
    session = await service.storage.get_session(session_id)
    assert session["status"] == "running"
    assert session["waiting_collaboration_id"] is None
    serialized = json.dumps(
        await service.storage.list_messages_for_compaction(session_id),
        sort_keys=True,
    )
    assert "channel_closed_or_credential_revoked" in serialized


@pytest.mark.asyncio
async def test_unavailable_channel_stops_at_deadline_with_environment_evidence(
    tmp_path: Path,
) -> None:
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=80)
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "deadline"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    session_id, turn_id = await _running_turn(service, deadline=deadline)
    report_id = str(uuid4())
    await service.storage.begin_collaboration_wait(
        turn_id,
        wait_id=f"report:{report_id}",
        pipeline_cursor=0,
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request, json={"detail": {"code": "offline"}})

    _bind_registry(service, channel_id=str(uuid4()), handler=handler)
    await asyncio.wait_for(
        service._await_collaboration_updates(session_id, turn_id), timeout=1
    )
    session = await service.storage.get_session(session_id)
    assert session["status"] == "running"
    assert session["waiting_collaboration_id"] is None
    serialized = json.dumps(
        await service.storage.list_messages_for_compaction(session_id),
        sort_keys=True,
    )
    assert "channel_unavailable_before_assignment_deadline" in serialized
    assert "failure_owner" in serialized
    assert "environment" in serialized


@pytest.mark.asyncio
async def test_reachable_empty_channel_does_not_create_unavailable_evidence(
    tmp_path: Path,
) -> None:
    deadline = datetime.now(timezone.utc) + timedelta(milliseconds=50)
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "reachable-deadline"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    session_id, turn_id = await _running_turn(service, deadline=deadline)
    report_id = str(uuid4())
    await service.storage.begin_collaboration_wait(
        turn_id,
        wait_id=f"report:{report_id}",
        pipeline_cursor=0,
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(200, request=request, json={"events": []})
        return httpx.Response(
            200,
            request=request,
            json={
                "reader_cursor": {
                    "reader_id": session_id,
                    "ack_seq": 0,
                    "revision": 0,
                }
            },
        )

    _bind_registry(service, channel_id=str(uuid4()), handler=handler)
    await asyncio.wait_for(
        service._await_collaboration_updates(session_id, turn_id), timeout=1
    )
    serialized = json.dumps(
        await service.storage.list_messages_for_compaction(session_id),
        sort_keys=True,
    )
    assert "collaboration_response_deadline_elapsed" in serialized
    assert "channel_unavailable_before_assignment_deadline" not in serialized
    assert "collaboration_counterparty" in serialized


@pytest.mark.asyncio
async def test_tool_result_and_wait_transition_roll_back_together_on_fault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "atomic"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    session_id, turn_id = await _running_turn(service)
    result_message_id = str(uuid4())

    def injected_fault(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("fault after atomic result insert")

    monkeypatch.setattr(service.storage, "_transition_session_conn", injected_fault)
    with pytest.raises(RuntimeError, match="fault after atomic result insert"):
        await service.storage.begin_collaboration_wait(
            turn_id,
            wait_id=f"report:{uuid4()}",
            pipeline_cursor=0,
            checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 1}},
            tool_result_content=[
                {
                    "type": "tool_result",
                    "tool_use_id": "blocking-report-tool",
                    "content": "persisted report result",
                }
            ],
            tool_result_message_id=result_message_id,
        )
    assert (await service.storage.get_session(session_id))["status"] == "running"
    assert (await service.storage.get_turn(turn_id))["status"] == "running"
    assert all(
        message["id"] != result_message_id
        for message in await service.storage.list_messages_for_compaction(session_id)
    )


@pytest.mark.asyncio
async def test_remote_result_checkpoint_recovers_wait_and_closes_later_tool_calls(
    tmp_path: Path,
) -> None:
    settings = LiliesSettings(
        data_dir=tmp_path / "remote-crash",
        workspace_root=tmp_path / "workspaces",
    )
    first = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    await first.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    projection = assignment.model_dump(mode="json", exclude_none=True)
    session_id, turn_id = await _running_turn(first, assignment=projection)
    report_id = str(uuid4())
    await first.storage.add_message(
        session_id,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "blocking-collaboration-report",
                "name": "collaboration_report_submit",
                "input": {
                    "operation": "submit",
                    "idempotency_key": "remote-crash-report-0001",
                    "report": {"report_id": report_id},
                },
            },
            {
                "type": "tool_use",
                "id": "later-tool-not-executed",
                "name": "collaboration_updates_read",
                "input": {},
            },
        ],
        turn_id=turn_id,
    )
    remote_result = ContentBlock(
        type="tool_result",
        tool_use_id="blocking-collaboration-report",
        content=json.dumps(
            {
                "ok": True,
                "status_code": 200,
                "data": {
                    "report_id": report_id,
                    "status": "awaiting_user_review",
                },
            }
        ),
    )
    await first._checkpoint_completed_tool_result(
        session_id=session_id,
        turn_id=turn_id,
        tool_name="collaboration_report_submit",
        tool_input={"operation": "submit"},
        result=remote_result,
        metrics=TurnMetrics(Usage()),
    )
    checkpointed = await first.storage.get_turn(turn_id)
    assert checkpointed["phase"] == "collaboration_remote_result"
    assert checkpointed["status"] == "running"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "events": [
                        {
                            "seq": 1,
                            "message_type": "developer_response",
                            "correlation_id": report_id,
                            "payload": {"report_id": report_id, "status": "ready"},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/acks"):
            return httpx.Response(200, request=request, json={"ack_seq": 1, "revision": 1})
        return httpx.Response(
            200,
            request=request,
            json={
                "reader_cursor": {
                    "reader_id": session_id,
                    "ack_seq": 0,
                    "revision": 0,
                }
            },
        )

    restarted = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    collaboration = assignment.collaboration
    assert collaboration is not None
    _bind_registry(
        restarted,
        channel_id=str(collaboration.channel_id),
        handler=handler,
    )
    recovery = await restarted.initialize()
    assert recovery["interrupted_turns"] == 0
    async with asyncio.timeout(3):
        while (await restarted.storage.get_turn(turn_id))["status"] != "completed":
            await asyncio.sleep(0.01)
    messages = await restarted.storage.list_messages_for_compaction(session_id)
    results = [
        block
        for message in messages
        if message["role"] == "tool"
        for block in message["content"]
        if block.get("type") == "tool_result"
    ]
    assert {block["tool_use_id"] for block in results} == {
        "blocking-collaboration-report",
        "later-tool-not-executed",
    }
    assert next(
        block for block in results if block["tool_use_id"] == "later-tool-not-executed"
    )["is_error"] is True
    events = await restarted.storage.list_events(session_id, after=0, limit=100)
    assert any(
        event["event_type"] == "collaboration.waiting"
        and event["data"].get("recovered_after_remote_commit") is True
        for event in events
    )


@pytest.mark.asyncio
async def test_remote_post_before_result_checkpoint_replays_from_durable_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = LiliesSettings(
        data_dir=tmp_path / "pre-checkpoint-crash",
        workspace_root=tmp_path / "workspaces",
    )
    first = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    await first.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    projection = assignment.model_dump(mode="json", exclude_none=True)
    session_id, turn_id = await _running_turn(first, assignment=projection)
    dynamic_secret = "opaque-dynamic-registered-value-97351"
    first._registered_secret_values[session_id] = frozenset({dynamic_secret})
    report = {
        "schema_version": "1.0",
        "report_id": str(uuid4()),
        "category": "platform_capability_gap",
        "phase": "preflight",
        "severity": "blocking",
        "summary": (
            "The public catalog lacks the required generic contract; copied marker "
            + dynamic_secret
        ),
        "original_goal": "Build the assigned enterprise reconciliation workflow.",
        "requirement_digest": "sha256:" + "a" * 64,
        "manuals_checked": [],
        "attempted_routes": [],
        "blocking_scope": "The generic input branch is blocked.",
        "independent_work": ["Continue artifact planning."],
        "workaround_considered": ["Use the nearest documented block."],
        "workaround_loss": "The substitute loses required typed evidence.",
        "requested_outcome": "Publish the generic typed contract.",
        "confidence": 0.95,
        "secret_redactions": [],
        "evidence_refs": [],
        "missing_contract": "Typed request and response semantics.",
    }
    tool_input = {
        "operation": "submit",
        "idempotency_key": "pre-checkpoint-crash-report-0001",
        "report": report,
    }
    block = ContentBlock(
        type="tool_use",
        id="pre-checkpoint-report-tool",
        name="collaboration_report_submit",
        input=tool_input,
    )
    await first.storage.add_message(
        session_id,
        "assistant",
        [block.model_dump(mode="json", exclude_none=True)],
        turn_id=turn_id,
    )
    post_count = 0
    post_bodies: list[dict[str, Any]] = []
    allow_reply = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        if request.method == "POST" and request.url.path.endswith("/reports"):
            post_count += 1
            post_bodies.append(json.loads(request.content))
            return httpx.Response(
                200,
                request=request,
                json={
                    "report_id": report["report_id"],
                    "status": "awaiting_user_review",
                    "revision": 1,
                },
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(
                200,
                request=request,
                json={
                    "events": (
                        [
                            {
                                "seq": 1,
                                "message_type": "developer_response",
                                "correlation_id": report["report_id"],
                                "payload": {
                                    "report_id": report["report_id"],
                                    "status": "ready_for_lilies_verification",
                                },
                            }
                        ]
                        if allow_reply
                        else []
                    )
                },
            )
        if request.url.path.endswith("/acks"):
            return httpx.Response(200, request=request, json={"ack_seq": 1, "revision": 1})
        return httpx.Response(
            200,
            request=request,
            json={
                "revision": 1,
                "reader_cursor": {
                    "reader_id": session_id,
                    "ack_seq": 0,
                    "revision": 0,
                },
            },
        )

    collaboration = assignment.collaboration
    assert collaboration is not None
    registry = _bind_registry(
        first,
        channel_id=str(collaboration.channel_id),
        handler=handler,
    )

    async def fail_after_remote_commit(**kwargs: Any) -> None:
        raise RuntimeError("injected local checkpoint failure")

    monkeypatch.setattr(
        first, "_checkpoint_completed_tool_result", fail_after_remote_commit
    )
    with pytest.raises(LiliesCollaborationDurabilityError):
        await first._execute_tool(
            session_id,
            turn_id,
            block,
            TurnMetrics(Usage()),
            (await first.storage.get_session(session_id))["config"],
            assignment.constraints.deadline_at,
            CumulativeMetrics(),
            registry,
        )
    assert post_count == 1
    pending = (await first.storage.get_turn(turn_id))["checkpoint"]["pending"]
    assert pending["kind"] == "collaboration_side_effect_pending"
    assert pending["tool_input"]["idempotency_key"] == tool_input["idempotency_key"]
    assert dynamic_secret not in json.dumps(post_bodies)
    assert pending["tool_input"]["report"]["summary"].endswith("[REDACTED]")

    allow_reply = True
    restarted = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    _bind_registry(
        restarted,
        channel_id=str(collaboration.channel_id),
        handler=handler,
    )
    recovery = await restarted.initialize()
    assert recovery["interrupted_turns"] == 0
    async with asyncio.timeout(3):
        while (await restarted.storage.get_turn(turn_id))["status"] != "completed":
            await asyncio.sleep(0.01)
    assert post_count == 2
    assert post_bodies[0] == post_bodies[1]
    session = await restarted.storage.get_session(session_id)
    assert session["last_pipeline_cursor"] == 1
    assert session["waiting_collaboration_id"] is None
    results = [
        block
        for message in await restarted.storage.list_messages_for_compaction(session_id)
        if message["role"] == "tool"
        for block in message["content"]
        if block.get("tool_use_id") == "pre-checkpoint-report-tool"
    ]
    assert len(results) == 1
    assert results[0].get("is_error", False) is False


@pytest.mark.asyncio
async def test_non_waiting_collaboration_result_is_recovered_before_model_resume(
    tmp_path: Path,
) -> None:
    settings = LiliesSettings(
        data_dir=tmp_path / "non-wait-result-crash",
        workspace_root=tmp_path / "workspaces",
    )
    first = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    await first.initialize()
    assignment = BuildAssignment.model_validate(assignment_payload(formal=True))
    projection = assignment.model_dump(mode="json", exclude_none=True)
    session_id, turn_id = await _running_turn(first, assignment=projection)
    tool_call_id = "withdraw-result-before-local-message"
    await first.storage.add_message(
        session_id,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": tool_call_id,
                "name": "collaboration_report_submit",
                "input": {"operation": "withdraw"},
            }
        ],
        turn_id=turn_id,
    )
    result = ContentBlock(
        type="tool_result",
        tool_use_id=tool_call_id,
        content=json.dumps(
            {
                "ok": True,
                "status_code": 200,
                "data": {"status": "withdrawn", "report_id": str(uuid4())},
            }
        ),
    )
    await first._checkpoint_completed_tool_result(
        session_id=session_id,
        turn_id=turn_id,
        tool_name="collaboration_report_submit",
        tool_input={"operation": "withdraw"},
        result=result,
        metrics=TurnMetrics(Usage()),
    )
    pending = (await first.storage.get_turn(turn_id))["checkpoint"]["pending"]
    assert pending["kind"] == "collaboration_remote_result"
    assert pending["wait_id"] is None

    restarted = LocalLiliesService(settings, provider=ScriptedLocalProvider())

    async def core_registry(
        self: LocalLiliesService,
        session_id: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> LiliesToolRegistry:
        return LiliesToolRegistry()

    restarted.tool_registry_for_session = MethodType(  # type: ignore[method-assign]
        core_registry, restarted
    )
    recovery = await restarted.initialize()
    assert recovery["interrupted_turns"] == 0
    async with asyncio.timeout(3):
        while (await restarted.storage.get_turn(turn_id))["status"] != "completed":
            await asyncio.sleep(0.01)
    results = [
        block
        for message in await restarted.storage.list_messages_for_compaction(session_id)
        if message["role"] == "tool"
        for block in message["content"]
        if block.get("tool_use_id") == tool_call_id
    ]
    assert len(results) == 1
    assert results[0]["content"] == result.content
