from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_platform.lilies_config import LiliesSettings
from agent_platform.lilies_models import (
    PermissionDecisionRequest,
    SessionCancelRequest,
    SessionCreateRequest,
    SessionMessageRequest,
    SessionResumeRequest,
    SessionStatus,
)
from agent_platform.lilies_service import LiliesBudgetExceeded, LocalLiliesService
from agent_platform.lilies_storage import LiliesConflictError
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


class ScriptedLocalProvider(ModelProvider):
    name = "scripted"

    def __init__(self, *, tool: str | None = None, tool_input: dict[str, Any] | None = None) -> None:
        self.tool = tool
        self.tool_input = tool_input or {}
        self.calls = 0
        self.seen_messages: list[list[ChatMessage]] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, False, False, False, 100_000, 10_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.seen_messages.append(messages)
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 10}}},
        )
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "thinking", "thinking": ""}},
        )
        yield StreamEvent(
            type="content_block_delta",
            data={
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "private reasoning"},
            },
        )
        if self.tool and self.calls == 1:
            payload = __import__("json").dumps(self.tool_input, ensure_ascii=False)
            yield StreamEvent(
                type="content_block_start",
                data={
                    "index": 1,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-call-1",
                        "name": self.tool,
                        "input": {},
                    },
                },
            )
            yield StreamEvent(
                type="content_block_delta",
                data={
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": payload},
                },
            )
            yield StreamEvent(type="content_block_stop", data={"index": 1})
            stop_reason = "tool_use"
        else:
            yield StreamEvent(
                type="content_block_start",
                data={"index": 1, "content_block": {"type": "text", "text": ""}},
            )
            yield StreamEvent(
                type="content_block_delta",
                data={
                    "index": 1,
                    "delta": {"type": "text_delta", "text": f"reply-{self.calls}"},
                },
            )
            stop_reason = "end_turn"
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 5}},
        )


class SlowProvider(ModelProvider):
    name = "slow"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(False, True, False, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        await asyncio.sleep(30)
        yield StreamEvent(type="message_start", data={})


class MeteredProvider(ModelProvider):
    name = "metered"

    def __init__(
        self,
        *,
        input_tokens: int = 10,
        output_tokens: int = 5,
        cost_usd: float = 0.25,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(False, True, False, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(
            type="message_start",
            data={
                "message": {
                    "usage": {
                        "input_tokens": self.input_tokens,
                        "cost_usd": self.cost_usd,
                    }
                }
            },
        )
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "text", "text": ""}},
        )
        yield StreamEvent(
            type="content_block_delta",
            data={
                "index": 0,
                "delta": {"type": "text_delta", "text": f"metered-{self.calls}"},
            },
        )
        yield StreamEvent(
            type="message_delta",
            data={
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": self.output_tokens},
            },
        )


class RepeatingToolProvider(ModelProvider):
    name = "repeating-tool"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(False, True, False, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 1}}},
        )
        if self.calls % 2:
            yield StreamEvent(
                type="content_block_start",
                data={
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"tool-{self.calls}",
                        "name": "local_time",
                        "input": {},
                    },
                },
            )
            stop_reason = "tool_use"
        else:
            yield StreamEvent(
                type="content_block_start",
                data={
                    "index": 0,
                    "content_block": {"type": "text", "text": "complete"},
                },
            )
            stop_reason = "end_turn"
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 1}},
        )


async def paired_service(
    tmp_path: Path,
    provider: ModelProvider,
) -> tuple[LocalLiliesService, str]:
    settings = LiliesSettings(data_dir=tmp_path / "lilies", model="test-model")
    service = LocalLiliesService(settings, provider=provider)
    await service.initialize()
    code = await service.storage.create_pairing_code()
    exchanged = await service.storage.exchange_pairing_code(
        code["pairing_code"],
        "cli:test",
        [
            "lilies.session:read",
            "lilies.session:write",
            "lilies.permission:resolve",
            "lilies.daemon:control",
            "lilies.credential:write",
        ],
        f"nonce-{uuid4().hex}",
        settings.daemon_fingerprint(),
    )
    return service, exchanged["client_id"]


async def create_session(service: LocalLiliesService, client_id: str) -> str:
    created = await service.create_session(
        SessionCreateRequest(idempotency_key=f"session:{uuid4().hex}"),
        client_id=client_id,
    )
    return created["id"]


async def send(service: LocalLiliesService, client_id: str, session_id: str, text: str) -> dict:
    request = SessionMessageRequest(
        idempotency_key=f"message:{uuid4().hex}",
        message_id=uuid4(),
        content=text,
    )
    return await service.submit_message(session_id, request, client_id=client_id)


async def wait_for_status(
    service: LocalLiliesService,
    session_id: str,
    status: str,
    *,
    timeout: float = 3,
) -> dict[str, Any]:
    async with asyncio.timeout(timeout):
        while True:
            session = await service.storage.get_session(session_id)
            if session["status"] == status:
                return session
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_local_service_persists_three_turns_without_private_thinking(tmp_path: Path) -> None:
    provider = ScriptedLocalProvider()
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)

    for number in range(3):
        await send(service, client_id, session_id, f"message-{number}")
        await wait_for_status(service, session_id, "ready")

    messages = await service.storage.list_messages(session_id, client_id=client_id)
    events = await service.storage.list_events(session_id, client_id=client_id)
    encoded = __import__("json").dumps({"messages": messages, "events": events})
    assert provider.calls == 3
    assert [item["role"] for item in messages] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert "private reasoning" not in encoded
    assert "thinking" not in encoded

    restarted = LocalLiliesService(service.settings, provider=ScriptedLocalProvider())
    recovery = await restarted.initialize()
    assert recovery["interrupted_sessions"] == 0
    assert (await restarted.storage.get_session(session_id))["status"] == "ready"
    assert len(await restarted.storage.list_messages(session_id)) == 6


@pytest.mark.asyncio
async def test_local_service_runs_safe_tool_loop_sequentially(tmp_path: Path) -> None:
    provider = ScriptedLocalProvider(tool="local_time")
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)

    await send(service, client_id, session_id, "What time is it?")
    session = await wait_for_status(service, session_id, "ready")

    messages = await service.storage.list_messages(session_id)
    assert provider.calls == 2
    assert session["tool_count"] == 1
    assert any(item["role"] == "tool" for item in messages)
    assert any(
        block.get("text") == "reply-2"
        for item in messages
        for block in item["content"]
    )


@pytest.mark.asyncio
async def test_permission_is_durable_and_exact_input_executes_after_restart(tmp_path: Path) -> None:
    provider = ScriptedLocalProvider(
        tool="workspace_write",
        tool_input={"path": "result.txt", "content": "persisted"},
    )
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    await send(service, client_id, session_id, "Write the result")
    await wait_for_status(service, session_id, "waiting_permission")
    pending = (await service.storage.list_pending_permission_requests(session_id=session_id))[0]
    assert pending["tool_input"] == {"path": "result.txt", "content": "persisted"}

    await service.shutdown(reason="test_restart")
    assert (await service.storage.get_session(session_id))["status"] == "waiting_permission"

    restarted = LocalLiliesService(service.settings, provider=provider)
    await restarted.initialize()
    decision = PermissionDecisionRequest(
        idempotency_key=f"permission:{uuid4().hex}",
        behavior="allow",
        expected_input_digest=pending["input_digest"],
        updated_input={"path": "approved.txt", "content": "approved-after-restart"},
    )
    await restarted.resolve_permission(
        session_id,
        pending["id"],
        decision,
        client_id=client_id,
    )
    await wait_for_status(restarted, session_id, "ready")

    workspace = restarted.settings.resolved_workspace_root / session_id
    assert (workspace / "approved.txt").read_text(encoding="utf-8") == "approved-after-restart"
    assert not (workspace / "result.txt").exists()
    resolved = await restarted.storage.get_permission_request(pending["id"])
    assert resolved["tool_input"] == {"path": "result.txt", "content": "persisted"}
    assert resolved["decision_input"] == {
        "path": "approved.txt",
        "content": "approved-after-restart",
    }
    assert resolved["original_input_digest"] == pending["input_digest"]
    assert resolved["approved_input_digest"] == restarted._digest_json(
        resolved["decision_input"]
    )
    events = await restarted.storage.list_events(session_id)
    assert [item["event_type"] for item in events].count("permission.requested") == 1
    assert [item["event_type"] for item in events].count("permission.resolved") == 1
    assert [item["event_type"] for item in events].count("tool.completed") == 1


@pytest.mark.asyncio
async def test_tampered_approved_permission_input_fails_closed_after_restart(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider(
        tool="workspace_write",
        tool_input={"path": "original.txt", "content": "original"},
    )
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    await send(service, client_id, session_id, "Write the original")
    await wait_for_status(service, session_id, "waiting_permission")
    pending = (await service.storage.list_pending_permission_requests(session_id=session_id))[0]
    await service.shutdown(reason="test_restart")

    await service.storage.resolve_permission_request(
        session_id,
        pending["id"],
        "allow",
        client_id=client_id,
        expected_input_digest=pending["input_digest"],
        updated_input={"path": "approved.txt", "content": "approved"},
        idempotency_key=f"permission:{uuid4().hex}",
    )
    with sqlite3.connect(service.storage.db_path) as conn:
        conn.execute(
            "UPDATE permission_requests SET decision_input_json=? WHERE id=?",
            (json.dumps({"path": "tampered.txt", "content": "tampered"}), pending["id"]),
        )

    restarted = LocalLiliesService(service.settings, provider=provider)
    await restarted.initialize()
    persisted = await restarted.storage.get_permission_request(pending["id"])
    with pytest.raises(LiliesConflictError, match="approved input digest mismatch"):
        restarted._validated_approved_permission_input(
            restarted.tools.get("workspace_write"),
            persisted,
        )
    workspace = restarted.settings.resolved_workspace_root / session_id
    assert not (workspace / "original.txt").exists()
    assert not (workspace / "approved.txt").exists()
    assert not (workspace / "tampered.txt").exists()


@pytest.mark.asyncio
async def test_concurrent_turn_and_budget_limit_fail_closed(tmp_path: Path) -> None:
    service, client_id = await paired_service(tmp_path, SlowProvider())
    session_id = await create_session(service, client_id)
    await send(service, client_id, session_id, "first")
    await wait_for_status(service, session_id, "running")

    with pytest.raises(Exception, match="cannot accept a message|cannot start a turn|active turn"):
        await send(service, client_id, session_id, "second")
    assert all(
        block.get("text") != "second"
        for item in await service.storage.list_messages(session_id)
        for block in item["content"]
    )

    await service.cancel_session(
        session_id,
        SessionCancelRequest(
            idempotency_key=f"cancel:{uuid4().hex}",
            reason="test cancellation",
        ),
        client_id=client_id,
    )
    assert (await service.storage.get_session(session_id))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_model_turn_limit_marks_session_error(tmp_path: Path) -> None:
    provider = ScriptedLocalProvider(tool="local_time")
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    session = await service.storage.get_session(session_id)
    config = dict(session["config"])
    config["max_turns"] = 1
    config["max_model_calls"] = 1
    await service.storage.update_session_context(session_id, config=config)

    await send(service, client_id, session_id, "Use a tool")
    await wait_for_status(service, session_id, "error")

    turn = (await service.storage.list_turns(session_id))[-1]
    assert turn["status"] == "error"
    assert "maximum model turns exceeded" in turn["error"]


@pytest.mark.asyncio
async def test_explicit_daemon_stop_cancels_turn_but_leaves_session_resumable(
    tmp_path: Path,
) -> None:
    service, client_id = await paired_service(tmp_path, SlowProvider())
    session_id = await create_session(service, client_id)
    await send(service, client_id, session_id, "keep working")
    await wait_for_status(service, session_id, "running")
    task = service.active_turns[session_id]

    assert await service.request_stop(reason="test stop") == 1
    await asyncio.gather(task, return_exceptions=True)

    assert (await service.storage.get_session(session_id))["status"] == "interrupted"
    assert (await service.storage.list_turns(session_id))[-1]["status"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit_key", "limit_value", "expected_error"),
    [
        ("max_budget_usd", 0.25, "model budget exceeded"),
        ("max_tokens", 15, "maximum token budget exceeded"),
        ("max_model_calls", 1, "maximum model turns exceeded"),
    ],
)
async def test_limits_reject_second_turn_from_persisted_cumulative_usage_without_model_call(
    tmp_path: Path,
    limit_key: str,
    limit_value: float,
    expected_error: str,
) -> None:
    provider = MeteredProvider()
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    session = await service.storage.get_session(session_id)
    config = {
        **session["config"],
        "max_budget_usd": 10.0,
        "max_tokens": 10_000,
        "max_model_calls": 10,
        limit_key: limit_value,
    }
    await service.storage.update_session_context(session_id, config=config)

    await send(service, client_id, session_id, "first metered turn")
    first = await wait_for_status(service, session_id, "ready")
    assert first["token_count"] == 15
    assert first["cost_usd"] == pytest.approx(0.25)
    assert first["model_call_count"] == 1
    assert provider.calls == 1

    await send(service, client_id, session_id, "second metered turn")
    second = await wait_for_status(service, session_id, "error")
    second_turn = (await service.storage.list_turns(session_id))[-1]
    assert expected_error in second_turn["error"]
    assert provider.calls == 1
    assert second["token_count"] == 15
    assert second["cost_usd"] == pytest.approx(0.25)
    assert second["model_call_count"] == 1


@pytest.mark.asyncio
async def test_tool_limit_uses_previous_turn_and_never_executes_over_limit_tool(
    tmp_path: Path,
) -> None:
    provider = RepeatingToolProvider()
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    session = await service.storage.get_session(session_id)
    await service.storage.update_session_context(
        session_id,
        config={**session["config"], "max_tool_calls": 1, "max_model_calls": 10},
    )

    await send(service, client_id, session_id, "first tool turn")
    first = await wait_for_status(service, session_id, "ready")
    assert first["tool_count"] == 1
    first_events = await service.storage.list_events(session_id)
    assert [event["event_type"] for event in first_events].count("tool.started") == 1

    await send(service, client_id, session_id, "second tool turn")
    await wait_for_status(service, session_id, "error")
    failed = (await service.storage.list_turns(session_id))[-1]
    assert "maximum tool calls exceeded" in failed["error"]
    events = await service.storage.list_events(session_id)
    assert [event["event_type"] for event in events].count("tool.started") == 1
    assert provider.calls == 3  # The model requested it; the tool itself was never invoked.


@pytest.mark.asyncio
async def test_relative_deadline_belongs_to_current_turn_not_session_age(
    tmp_path: Path,
) -> None:
    provider = MeteredProvider()
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    session = await service.storage.get_session(session_id)
    await service.storage.update_session_context(
        session_id,
        config={**session["config"], "deadline_seconds": 1},
    )
    old = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with sqlite3.connect(service.storage.db_path) as connection:
        connection.execute("UPDATE sessions SET created_at=? WHERE id=?", (old, session_id))

    await send(service, client_id, session_id, "old session, fresh turn")
    await wait_for_status(service, session_id, "ready")
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_current_turn_deadline_interrupts_slow_model(tmp_path: Path) -> None:
    provider = SlowProvider()
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    session = await service.storage.get_session(session_id)
    await service.storage.update_session_context(
        session_id,
        config={**session["config"], "deadline_seconds": 1},
    )

    await send(service, client_id, session_id, "bounded slow turn")
    await wait_for_status(service, session_id, "error", timeout=3)
    turn = (await service.storage.list_turns(session_id))[-1]
    assert "session deadline exceeded" in turn["error"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_absolute_assignment_deadline_is_cumulative_and_skips_model(tmp_path: Path) -> None:
    provider = MeteredProvider()
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    session = await service.storage.get_session(session_id)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    await service.storage.update_session_context(
        session_id,
        config={**session["config"], "deadline_seconds": 3600, "deadline_at": expired},
    )

    await send(service, client_id, session_id, "expired assignment")
    await wait_for_status(service, session_id, "error")
    turn = (await service.storage.list_turns(session_id))[-1]
    assert "session deadline exceeded" in turn["error"]
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_explicit_stop_cancels_waiting_permission_and_persists_resumable_state(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider(
        tool="workspace_write",
        tool_input={"path": "never-written.txt", "content": "blocked"},
    )
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    await send(service, client_id, session_id, "request dangerous write")
    waiting = await wait_for_status(service, session_id, "waiting_permission")
    permission_id = waiting["waiting_permission_id"]

    assert await service.request_stop(reason="explicit stop") == 1

    session = await service.storage.get_session(session_id)
    turn = (await service.storage.list_turns(session_id))[-1]
    permission = await service.storage.get_permission_request(permission_id)
    assert session["status"] == "interrupted"
    assert session["waiting_permission_id"] is None
    assert session["waiting_collaboration_id"] is None
    assert turn["status"] == "cancelled"
    assert turn["token_count"] == 15
    assert turn["tool_count"] == 1
    assert turn["model_call_count"] == 1
    assert permission["status"] == "cancelled"
    assert session["token_count"] == 15
    assert session["tool_count"] == 1
    assert session["model_call_count"] == 1
    assert await service.storage.list_pending_permission_requests(session_id=session_id) == []
    assert not (
        service.settings.resolved_workspace_root / session_id / "never-written.txt"
    ).exists()


@pytest.mark.asyncio
async def test_explicit_stop_sweeps_durable_wait_without_in_memory_task(
    tmp_path: Path,
) -> None:
    service, client_id = await paired_service(tmp_path, MeteredProvider())
    session_id = await create_session(service, client_id)
    message = await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "wait for collaboration"}],
    )
    turn = await service.storage.create_turn(
        session_id,
        request_id=f"request:{uuid4().hex}",
        idempotency_key=f"turn:{uuid4().hex}",
        input_message_id=message["id"],
        checkpoint={"metrics": {"usage": {}, "model_calls": 0, "tool_calls": 0}},
    )
    with sqlite3.connect(service.storage.db_path) as connection:
        connection.execute(
            "UPDATE turns SET status='waiting_collaboration' WHERE id=?",
            (turn["id"],),
        )
        connection.execute(
            """
            UPDATE sessions SET status='waiting_collaboration',waiting_collaboration_id=?
            WHERE id=?
            """,
            ("report:pending", session_id),
        )

    assert await service.request_stop(reason="explicit stop") == 1
    assert (await service.storage.get_session(session_id))["status"] == "interrupted"
    assert (await service.storage.get_turn(turn["id"]))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_user_cancel_settles_durable_waiting_permission_checkpoint_usage(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider(
        tool="workspace_write",
        tool_input={"path": "cancelled.txt", "content": "must not execute"},
    )
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    await send(service, client_id, session_id, "request then cancel")
    waiting = await wait_for_status(service, session_id, "waiting_permission")
    permission_id = waiting["waiting_permission_id"]
    await service.shutdown(reason="restart before user cancel")

    restarted = LocalLiliesService(service.settings, provider=provider)
    await restarted.initialize()
    cancelled = await restarted.cancel_session(
        session_id,
        SessionCancelRequest(
            idempotency_key=f"cancel:{uuid4().hex}",
            reason="user cancelled durable wait",
        ),
        client_id=client_id,
    )

    turn = (await restarted.storage.list_turns(session_id))[-1]
    permission = await restarted.storage.get_permission_request(permission_id)
    assert cancelled["status"] == "cancelled"
    assert cancelled["waiting_permission_id"] is None
    assert turn["status"] == "cancelled"
    assert permission["status"] == "cancelled"
    assert (turn["token_count"], turn["tool_count"], turn["model_call_count"]) == (15, 1, 1)
    assert (
        cancelled["token_count"],
        cancelled["tool_count"],
        cancelled["model_call_count"],
    ) == (15, 1, 1)
    assert not (
        restarted.settings.resolved_workspace_root / session_id / "cancelled.txt"
    ).exists()


@pytest.mark.asyncio
async def test_user_cancel_finishes_durable_collaboration_turn_without_task(
    tmp_path: Path,
) -> None:
    service, client_id = await paired_service(tmp_path, MeteredProvider())
    session_id = await create_session(service, client_id)
    message = await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "durable collaboration wait"}],
    )
    turn = await service.storage.create_turn(
        session_id,
        request_id=f"request:{uuid4().hex}",
        idempotency_key=f"turn:{uuid4().hex}",
        input_message_id=message["id"],
        checkpoint={
            "metrics": {
                "usage": {"input_tokens": 7, "output_tokens": 3, "cost_usd": 0.5},
                "model_calls": 1,
                "tool_calls": 0,
            }
        },
    )
    with sqlite3.connect(service.storage.db_path) as connection:
        connection.execute(
            "UPDATE turns SET status='waiting_collaboration' WHERE id=?",
            (turn["id"],),
        )
        connection.execute(
            """
            UPDATE sessions SET status='waiting_collaboration',waiting_collaboration_id=?
            WHERE id=?
            """,
            ("report:cancel-me", session_id),
        )

    cancelled = await service.cancel_session(
        session_id,
        SessionCancelRequest(
            idempotency_key=f"cancel:{uuid4().hex}",
            reason="user cancelled collaboration wait",
        ),
        client_id=client_id,
    )
    persisted_turn = await service.storage.get_turn(turn["id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["waiting_collaboration_id"] is None
    assert persisted_turn["status"] == "cancelled"
    assert persisted_turn["token_count"] == 10
    assert persisted_turn["cost_usd"] == pytest.approx(0.5)
    assert persisted_turn["model_call_count"] == 1


@pytest.mark.asyncio
async def test_interrupted_session_requires_resume_and_seals_uncertain_tool_call(
    tmp_path: Path,
) -> None:
    service, client_id = await paired_service(tmp_path, SlowProvider())
    session_id = await create_session(service, client_id)
    await send(service, client_id, session_id, "begin interrupted work")
    await wait_for_status(service, session_id, "running")
    await service.shutdown(reason="simulated crash")
    await service.storage.add_message(
        session_id,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "uncertain-side-effect",
                "name": "workspace_write",
                "input": {"path": "uncertain.txt", "content": "must not replay"},
            }
        ],
    )

    restarted = LocalLiliesService(service.settings, provider=MeteredProvider())
    await restarted.initialize()
    with pytest.raises(LiliesConflictError, match="cannot accept a message from interrupted"):
        await send(restarted, client_id, session_id, "illegal direct continuation")

    await restarted.resume_session(
        session_id,
        SessionResumeRequest(
            idempotency_key=f"resume:{uuid4().hex}",
            expected_status=SessionStatus.interrupted,
            reason="explicit recovery",
        ),
        client_id=client_id,
    )
    await wait_for_status(restarted, session_id, "ready")
    messages = await restarted.storage.list_messages(session_id)
    uncertain_results = [
        block
        for item in messages
        for block in item["content"]
        if block.get("type") == "tool_result"
        and block.get("tool_use_id") == "uncertain-side-effect"
    ]
    assert len(uncertain_results) == 1
    assert uncertain_results[0]["is_error"] is True
    assert "not replayed automatically" in uncertain_results[0]["content"]
    assert not (
        restarted.settings.resolved_workspace_root / session_id / "uncertain.txt"
    ).exists()


@pytest.mark.asyncio
async def test_status_excludes_expired_clients_and_platform_pairing(tmp_path: Path) -> None:
    service, client_id = await paired_service(tmp_path, MeteredProvider())
    with sqlite3.connect(service.storage.db_path) as connection:
        connection.execute("UPDATE clients SET name='platform' WHERE id=?", (client_id,))
    before = await service.status()
    assert before["paired_client_count"] == 1
    assert before["platform_paired"] is True

    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(service.storage.db_path) as connection:
        connection.execute(
            "UPDATE clients SET expires_at=? WHERE id=?",
            (expired, client_id),
        )
    after = await service.status()
    assert after["paired_client_count"] == 0
    assert after["platform_paired"] is False


@pytest.mark.asyncio
async def test_status_excludes_terminal_assignment_sessions(tmp_path: Path) -> None:
    service, client_id = await paired_service(tmp_path, MeteredProvider())
    session_id = await create_session(service, client_id)
    with sqlite3.connect(service.storage.db_path) as connection:
        connection.execute(
            "UPDATE sessions SET assignment_id=? WHERE id=?",
            (str(uuid4()), session_id),
        )
    assert (await service.status())["active_assignment_count"] == 1

    await service.storage.transition_session(session_id, "completed")
    assert (await service.status())["active_assignment_count"] == 0


@pytest.mark.asyncio
async def test_restart_settles_checkpoint_usage_once_for_future_limit_baseline(
    tmp_path: Path,
) -> None:
    service, client_id = await paired_service(tmp_path, MeteredProvider())
    session_id = await create_session(service, client_id)
    message = await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "hard restart checkpoint"}],
    )
    turn = await service.storage.create_turn(
        session_id,
        request_id=f"request:{uuid4().hex}",
        idempotency_key=f"turn:{uuid4().hex}",
        input_message_id=message["id"],
        checkpoint={
            "metrics": {
                "usage": {"input_tokens": 7, "output_tokens": 3, "cost_usd": 0.5},
                "model_calls": 1,
                "tool_calls": 2,
            }
        },
    )

    restarted = LocalLiliesService(service.settings, provider=MeteredProvider())
    recovery = await restarted.initialize()
    assert recovery["interrupted_turns"] == 1
    interrupted = await restarted.storage.get_turn(turn["id"])
    session = await restarted.storage.get_session(session_id)
    assert interrupted["status"] == "interrupted"
    assert (
        interrupted["token_count"],
        interrupted["cost_usd"],
        interrupted["tool_count"],
        interrupted["model_call_count"],
    ) == (10, 0.5, 2, 1)
    assert (
        session["token_count"],
        session["cost_usd"],
        session["tool_count"],
        session["model_call_count"],
    ) == (10, 0.5, 2, 1)

    baseline = restarted._cumulative_metrics(session)
    assert (
        baseline.token_count,
        baseline.cost_usd,
        baseline.tool_calls,
        baseline.model_calls,
    ) == (10, 0.5, 2, 1)


def test_safe_error_keeps_structured_limit_but_redacts_credentials() -> None:
    assert "maximum token budget exceeded" in LocalLiliesService._safe_error(
        LiliesBudgetExceeded("maximum token budget exceeded (15)")
    )
    assert LocalLiliesService._safe_error(
        RuntimeError("Authorization: Bearer api-token-value")
    ) == "RuntimeError: sensitive error detail redacted"
