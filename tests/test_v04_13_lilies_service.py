from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import NAMESPACE_URL, uuid4, uuid5

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
from agent_platform.lilies_service import (
    LiliesBudgetExceeded,
    LocalLiliesService,
    TurnMetrics,
)
from agent_platform.lilies_storage import LiliesConflictError
from agent_platform.lilies_platform_client import (
    ZERO_CONTRACT_DIGEST,
    PlatformToolEnvelope,
)
from agent_platform.lilies_platform_tools import PlatformHttpTool
from agent_platform.lilies_tools import (
    LiliesTool,
    LiliesToolContext,
    LiliesToolResult,
    StrictToolInput,
)
from agent_platform.models import (
    ChatMessage,
    ContentBlock,
    StreamEvent,
    ToolDefinition,
    Usage,
)
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


class ParallelToolProvider(ModelProvider):
    name = "parallel-tool"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[ChatMessage]] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.seen_messages.append(kwargs["messages"])
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 10}}},
        )
        if self.calls == 1:
            for index, (tool_use_id, tool_name) in enumerate(
                (
                    ("parallel-success-1", "local_time"),
                    ("parallel-failure", "missing_local_tool"),
                    ("parallel-success-2", "local_time"),
                )
            ):
                yield StreamEvent(
                    type="content_block_start",
                    data={
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": tool_name,
                            "input": {},
                        },
                    },
                )
                yield StreamEvent(type="content_block_stop", data={"index": index})
            stop_reason = "tool_use"
        else:
            yield StreamEvent(
                type="content_block_start",
                data={
                    "index": 0,
                    "content_block": {"type": "text", "text": "parallel complete"},
                },
            )
            stop_reason = "end_turn"
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 5}},
        )


class TranscriptCaptureProvider(ModelProvider):
    name = "transcript-capture"

    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[ChatMessage]] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(False, True, True, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.seen_messages.append(kwargs["messages"])
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 5}}},
        )
        yield StreamEvent(
            type="content_block_start",
            data={
                "index": 0,
                "content_block": {"type": "text", "text": "resumed safely"},
            },
        )
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        )


class DraftApplyBatchProvider(ModelProvider):
    name = "draft-apply-batch"

    def __init__(
        self,
        inputs: list[dict[str, Any]],
        *,
        tool_names: list[str] | None = None,
    ) -> None:
        self.inputs = inputs
        self.tool_names = tool_names or [
            "platform_draft_apply"
            for _ in inputs
        ]
        if len(self.tool_names) != len(inputs):
            raise ValueError("tool_names must match inputs")
        self.calls = 0
        self.seen_messages: list[list[ChatMessage]] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(self, **kwargs: Any) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.seen_messages.append(kwargs["messages"])
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 10}}},
        )
        if self.calls == 1:
            for index, tool_input in enumerate(self.inputs):
                yield StreamEvent(
                    type="content_block_start",
                    data={
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": f"draft-apply-{index + 1}",
                            "name": self.tool_names[index],
                            "input": tool_input,
                        },
                    },
                )
                yield StreamEvent(type="content_block_stop", data={"index": index})
            stop_reason = "tool_use"
        else:
            yield StreamEvent(
                type="content_block_start",
                data={
                    "index": 0,
                    "content_block": {"type": "text", "text": "draft batch complete"},
                },
            )
            stop_reason = "end_turn"
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": stop_reason}, "usage": {"output_tokens": 5}},
        )


class FakeDraftApplyInput(StrictToolInput):
    application_id: str
    expected_revision: int
    idempotency_key: str
    op: str
    data: dict[str, Any]


class FakeDraftApplyTool(LiliesTool):
    name = "platform_draft_apply"
    description = "Apply one revision-guarded fake draft mutation."
    input_model = FakeDraftApplyInput
    mutating = True
    side_effecting = True
    requires_permission = False
    preserve_result_integrity = True

    def __init__(
        self,
        revisions: dict[str, int],
        *,
        force_failure_calls: set[int] | None = None,
        external_bump_after_success_calls: set[int] | None = None,
    ) -> None:
        self.revisions = dict(revisions)
        self.force_failure_calls = force_failure_calls or set()
        self.external_bump_after_success_calls = (
            external_bump_after_success_calls or set()
        )
        self.calls: list[dict[str, Any]] = []

    async def execute(
        self,
        data: dict[str, Any],
        context: LiliesToolContext,
    ) -> LiliesToolResult:
        self.calls.append(dict(data))
        call_number = len(self.calls)
        application_id = str(data["application_id"])
        expected_revision = int(data["expected_revision"])
        current_revision = self.revisions[application_id]
        if (
            call_number in self.force_failure_calls
            or expected_revision != current_revision
        ):
            return LiliesToolResult(
                json.dumps(
                    {
                        "ok": False,
                        "operation": self.name,
                        "status_code": 409,
                        "data": {},
                        "error": {
                            "code": "revision_conflict",
                            "message": (
                                f"expected {expected_revision}, "
                                f"current {current_revision}"
                            ),
                            "retryable": False,
                        },
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                is_error=True,
            )
        revision = current_revision + 1
        self.revisions[application_id] = revision
        result = LiliesToolResult(
            json.dumps(
                {
                    "ok": True,
                    "operation": self.name,
                    "status_code": 200,
                    "data": {
                        "application_id": application_id,
                        "revision": revision,
                        "content_hash": f"sha256:{revision:064x}",
                    },
                    "error": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        if call_number in self.external_bump_after_success_calls:
            self.revisions[application_id] = revision + 1
        return result


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


async def seed_platform_assignment_completion_transcript(
    service: LocalLiliesService,
    *,
    tests_passed: bool = True,
    apply_after_tests: bool = False,
    repeat_failed_tests: bool = False,
) -> tuple[str, str]:
    session_id = str(uuid4())
    assignment_id = str(uuid4())
    application_id = str(uuid4())
    await service.storage.create_session(
        session_id=session_id,
        config={"kind": "platform"},
    )
    assignment = {
        "assignment_id": assignment_id,
        "idempotency_key": f"assignment:{assignment_id}",
        "target": {
            "mode": "existing",
            "application_id": application_id,
        },
        "platform": {
            "contract_digest": ZERO_CONTRACT_DIGEST,
        },
    }
    receipt = await service.storage.accept_assignment(
        session_id,
        assignment,
        session_config={"kind": "platform"},
        start_message_id=str(uuid4()),
        start_message_content=[
            {
                "type": "text",
                "text": "Build and test the assigned workflow.",
            },
        ],
        start_turn_id=str(uuid4()),
        turn_checkpoint={
            "metrics": {
                "usage": {},
                "model_calls": 0,
                "tool_calls": 0,
            }
        },
    )
    turn_id = str(receipt["turn_id"])
    first_hash = "a" * 64

    async def tool_exchange(
        tool_use_id: str,
        name: str,
        data: dict[str, Any],
    ) -> None:
        assistant_message = await service.storage.add_message(
            session_id,
            "assistant",
            [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": name,
                    "input": {"application_id": application_id},
                }
            ],
            turn_id=turn_id,
        )
        await service.storage.add_message(
            session_id,
            "tool",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": json.dumps(
                        {
                            "ok": True,
                            "operation": name,
                            "data": data,
                        },
                        separators=(",", ":"),
                    ),
                    "is_error": False,
                }
            ],
            turn_id=turn_id,
            message_id=service._tool_result_message_id(
                turn_id,
                str(assistant_message["id"]),
                0,
                tool_use_id,
            ),
        )

    await tool_exchange(
        "apply-final-draft",
        "platform_draft_apply",
        {
            "application_id": application_id,
            "revision": 7,
            "content_hash": first_hash,
        },
    )
    passing_count = 2 if tests_passed else 1
    failed_count = 0 if tests_passed else 1
    await tool_exchange(
        "test-final-draft",
        "platform_tests_run",
        {
            "application_id": application_id,
            "passed": tests_passed,
            "validation": {
                "valid": True,
                "revision": 7,
                "content_hash": first_hash,
                "test_count": 2,
            },
            "summary": {
                "total": 2,
                "passed": passing_count,
                "failed": failed_count,
                "mandatory_failed": failed_count,
            },
            "tests": [
                {"test_id": "one", "passed": True},
                {"test_id": "two", "passed": tests_passed},
            ],
        },
    )
    if repeat_failed_tests:
        # Reuse the provider's tool call id in another assistant round. The
        # deterministic result message identity must keep the rounds distinct,
        # and the later failed attempt must supersede the earlier pass.
        await tool_exchange(
            "test-final-draft",
            "platform_tests_run",
            {
                "application_id": application_id,
                "passed": False,
                "validation": {
                    "valid": True,
                    "revision": 7,
                    "content_hash": first_hash,
                    "test_count": 2,
                },
                "summary": {
                    "total": 2,
                    "passed": 1,
                    "failed": 1,
                    "mandatory_failed": 1,
                },
                "tests": [
                    {"test_id": "one", "passed": True},
                    {"test_id": "two", "passed": False},
                ],
            },
        )
    if apply_after_tests:
        await tool_exchange(
            "edit-after-tests",
            "platform_draft_apply",
            {
                "application_id": application_id,
                "revision": 8,
                "content_hash": "b" * 64,
            },
        )
    await service.storage.add_message(
        session_id,
        "assistant",
        [{"type": "text", "text": "The tested workflow is complete and ready."}],
        turn_id=turn_id,
    )
    return session_id, turn_id


async def seed_model_loop_assignment(
    service: LocalLiliesService,
    *,
    mode: str = "formal_experiment",
    max_model_calls: int = 10,
) -> tuple[str, str]:
    session_id = str(uuid4())
    assignment_id = str(uuid4())
    application_id = str(uuid4())
    config = {
        "kind": "platform",
        "max_model_calls": max_model_calls,
        "max_turns": max_model_calls,
        "max_tokens": 100_000,
        "max_tool_calls": 100,
        "max_budget_usd": 10.0,
        "deadline_seconds": 300,
    }
    await service.storage.create_session(session_id=session_id, config=config)
    receipt = await service.storage.accept_assignment(
        session_id,
        {
            "assignment_id": assignment_id,
            "idempotency_key": f"assignment:{assignment_id}",
            "mode": mode,
            "target": {
                "mode": "existing",
                "application_id": application_id,
            },
            "platform": {"contract_digest": ZERO_CONTRACT_DIGEST},
        },
        session_config=config,
        start_message_id=str(uuid4()),
        start_message_content=[
            {"type": "text", "text": "Continue the assigned work."}
        ],
        start_turn_id=str(uuid4()),
        turn_checkpoint={
            "metrics": {
                "usage": {},
                "model_calls": 0,
                "usage_backed_model_calls": 0,
                "tool_calls": 0,
            }
        },
    )

    async def local_tools(
        session_id: str,
        *,
        session: dict[str, Any] | None = None,
    ) -> Any:
        del session_id, session
        return service.tools

    service.tool_registry_for_session = local_tools  # type: ignore[method-assign]
    return session_id, str(receipt["turn_id"])


@pytest.mark.asyncio
async def test_formal_model_loop_adds_one_same_turn_completion_continuation(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider()
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", model="test-model"),
        provider=provider,
    )
    await service.initialize()
    session_id, turn_id = await seed_model_loop_assignment(service)

    await service._run_model_loop(session_id, turn_id, TurnMetrics(Usage()))

    messages = await service.storage.list_messages_for_compaction(session_id)
    continuation_id = str(
        uuid5(
            NAMESPACE_URL,
            f"lilies:formal-completion-continuation:{turn_id}:1",
        )
    )
    continuations = [item for item in messages if item["id"] == continuation_id]
    assert provider.calls == 2
    assert len(continuations) == 1
    assert continuations[0]["role"] == "user"
    assert continuations[0]["turn_id"] == turn_id
    text = continuations[0]["content"][0]["text"].lower()
    assert "platform-generated" in text
    assert "not a new user instruction or authorization" in text
    assert "does not change" in text
    assert "permissions, budget, or acceptance" in text
    assert "durable completion evidence" in text
    assert "public persisted evidence" in text
    assert "hidden criteria" in text
    assert "treat prose as completion" in text
    assert "durable wait" in text
    for forbidden in (
        "report",
        "artifact",
        "claim",
        "host",
        "task",
        "scenario",
        "network",
        "secret",
    ):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_formal_completion_continuation_is_idempotent_across_reentry(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider()
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", model="test-model"),
        provider=provider,
    )
    await service.initialize()
    session_id, turn_id = await seed_model_loop_assignment(service)
    assert await service._append_formal_completion_continuation(
        session_id,
        turn_id,
    )
    assert not await service._append_formal_completion_continuation(
        session_id,
        turn_id,
    )

    await service._run_model_loop(session_id, turn_id, TurnMetrics(Usage()))

    messages = await service.storage.list_messages_for_compaction(session_id)
    continuation_id = str(
        uuid5(
            NAMESPACE_URL,
            f"lilies:formal-completion-continuation:{turn_id}:1",
        )
    )
    assert provider.calls == 1
    assert sum(item["id"] == continuation_id for item in messages) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "content"),
    [
        (
            "assistant",
            [
                {
                    "type": "text",
                    "text": "Platform-generated formal-protocol continuation.",
                }
            ],
        ),
        ("user", [{"type": "text", "text": "conflicting continuation"}]),
    ],
)
async def test_formal_completion_continuation_identity_conflict_fails_closed(
    tmp_path: Path,
    role: str,
    content: list[dict[str, str]],
) -> None:
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", model="test-model"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    session_id, turn_id = await seed_model_loop_assignment(service)
    continuation_id = str(
        uuid5(
            NAMESPACE_URL,
            f"lilies:formal-completion-continuation:{turn_id}:1",
        )
    )
    await service.storage.add_message(
        session_id,
        role,
        content,
        turn_id=turn_id,
        message_id=continuation_id,
    )

    with pytest.raises(
        LiliesConflictError,
        match="formal completion continuation identity conflicts",
    ):
        await service._append_formal_completion_continuation(
            session_id,
            turn_id,
        )


@pytest.mark.asyncio
async def test_customer_model_loop_does_not_add_formal_completion_continuation(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider()
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", model="test-model"),
        provider=provider,
    )
    await service.initialize()
    session_id, turn_id = await seed_model_loop_assignment(
        service,
        mode="customer",
    )

    await service._run_model_loop(session_id, turn_id, TurnMetrics(Usage()))

    messages = await service.storage.list_messages_for_compaction(session_id)
    continuation_id = str(
        uuid5(
            NAMESPACE_URL,
            f"lilies:formal-completion-continuation:{turn_id}:1",
        )
    )
    assert provider.calls == 1
    assert all(item["id"] != continuation_id for item in messages)


@pytest.mark.asyncio
async def test_accepted_completion_does_not_add_formal_completion_continuation(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider()
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", model="test-model"),
        provider=provider,
    )
    await service.initialize()
    session_id, turn_id = await seed_model_loop_assignment(service)
    service._platform_assignment_completion_evidence = AsyncMock(  # type: ignore[method-assign]
        return_value=object()
    )

    await service._run_model_loop(session_id, turn_id, TurnMetrics(Usage()))

    assert provider.calls == 1
    assert not await service._append_formal_completion_continuation(
        session_id,
        turn_id,
    )


@pytest.mark.asyncio
async def test_formal_completion_continuation_uses_same_model_call_budget(
    tmp_path: Path,
) -> None:
    provider = ScriptedLocalProvider()
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", model="test-model"),
        provider=provider,
    )
    await service.initialize()
    session_id, turn_id = await seed_model_loop_assignment(
        service,
        max_model_calls=1,
    )

    with pytest.raises(LiliesBudgetExceeded, match="maximum model turns exceeded"):
        await service._run_model_loop(
            session_id,
            turn_id,
            TurnMetrics(Usage()),
        )

    continuation_id = str(
        uuid5(
            NAMESPACE_URL,
            f"lilies:formal-completion-continuation:{turn_id}:1",
        )
    )
    messages = await service.storage.list_messages_for_compaction(session_id)
    assert provider.calls == 1
    assert sum(item["id"] == continuation_id for item in messages) == 1


@pytest.mark.asyncio
async def test_platform_assignment_turn_becomes_completed_only_with_current_test_claim(
    tmp_path: Path,
) -> None:
    service = LocalLiliesService(
        LiliesSettings(data_dir=tmp_path / "lilies", model="test-model"),
        provider=ScriptedLocalProvider(),
    )
    await service.initialize()
    completed_session_id, completed_turn_id = (
        await seed_platform_assignment_completion_transcript(service)
    )
    stale_session_id, stale_turn_id = (
        await seed_platform_assignment_completion_transcript(
            service,
            apply_after_tests=True,
        )
    )
    retested_session_id, retested_turn_id = (
        await seed_platform_assignment_completion_transcript(
            service,
            repeat_failed_tests=True,
        )
    )

    async def no_model_loop(
        session_id: str,
        turn_id: str,
        metrics: Any,
    ) -> None:
        del session_id, turn_id, metrics

    service._run_model_loop = no_model_loop  # type: ignore[method-assign]
    await service._run_turn(completed_session_id, completed_turn_id)
    await service._run_turn(stale_session_id, stale_turn_id)
    await service._run_turn(retested_session_id, retested_turn_id)

    assert (
        await service.storage.get_session(completed_session_id)
    )["status"] == "completed"
    assert (await service.storage.get_turn(completed_turn_id))["status"] == "completed"
    assert (await service.storage.get_session(stale_session_id))["status"] == "ready"
    assert (await service.storage.get_turn(stale_turn_id))["status"] == "completed"
    assert (
        await service.storage.get_session(retested_session_id)
    )["status"] == "ready"
    assert (await service.storage.get_turn(retested_turn_id))["status"] == "completed"


@pytest.mark.asyncio
async def test_restart_reconciles_only_current_passing_platform_assignment_claim(
    tmp_path: Path,
) -> None:
    settings = LiliesSettings(data_dir=tmp_path / "lilies", model="test-model")
    service = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    await service.initialize()
    completed_session_id, completed_turn_id = (
        await seed_platform_assignment_completion_transcript(service)
    )
    failed_session_id, failed_turn_id = (
        await seed_platform_assignment_completion_transcript(
            service,
            tests_passed=False,
        )
    )
    await service.storage.finish_turn(completed_turn_id, "completed")
    await service.storage.finish_turn(failed_turn_id, "completed")
    assert (
        await service.storage.get_session(completed_session_id)
    )["status"] == "ready"

    restarted = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    recovery = await restarted.initialize()

    assert recovery["completed_assignment_sessions"] == 1
    assert (
        await restarted.storage.get_session(completed_session_id)
    )["status"] == "completed"
    assert (await restarted.storage.get_session(failed_session_id))["status"] == "ready"

    replayed_restart = LocalLiliesService(settings, provider=ScriptedLocalProvider())
    replayed_recovery = await replayed_restart.initialize()
    assert replayed_recovery["completed_assignment_sessions"] == 0


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
async def test_local_service_batches_parallel_tool_results_for_provider(
    tmp_path: Path,
) -> None:
    provider = ParallelToolProvider()
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)

    await send(service, client_id, session_id, "Run three independent checks.")
    session = await wait_for_status(service, session_id, "ready")

    assert provider.calls == 2
    assert session["tool_count"] == 3
    durable_messages = await service.storage.list_messages(session_id)
    tool_messages = [item for item in durable_messages if item["role"] == "tool"]
    assert len(tool_messages) == 3
    durable_results = [
        ContentBlock.model_validate(block)
        for message in tool_messages
        for block in message["content"]
    ]
    assert [block.tool_use_id for block in durable_results] == [
        "parallel-success-1",
        "parallel-failure",
        "parallel-success-2",
    ]
    assert [
        block.get("is_error", False)
        for message in tool_messages
        for block in message["content"]
    ] == [
        False,
        True,
        False,
    ]

    provider_messages = provider.seen_messages[1]
    assert [message.role for message in provider_messages[-2:]] == [
        "assistant",
        "user",
    ]
    assert [
        block.tool_use_id for block in provider_messages[-1].content
    ] == [
        "parallel-success-1",
        "parallel-failure",
        "parallel-success-2",
    ]
    events = await service.storage.list_events(session_id)
    event_types = [event["event_type"] for event in events]
    assert event_types.count("tool.requested") == 3
    assert event_types.count("tool.completed") == 2
    assert event_types.count("tool.failed") == 1
    turn_id = (await service.storage.list_turns(session_id))[-1]["id"]
    assistant_message = next(
        item
        for item in durable_messages
        if item["role"] == "assistant"
        and any(block.get("type") == "tool_use" for block in item["content"])
    )
    assert [item["id"] for item in tool_messages] == [
        service._tool_result_message_id(
            turn_id,
            assistant_message["id"],
            index,
            result.tool_use_id,
        )
        for index, result in enumerate(durable_results)
    ]
    message_event_count = event_types.count("message.created")
    for message, result in zip(tool_messages, durable_results, strict=True):
        await service._add_tool_result_message(
            session_id,
            turn_id,
            result,
            message_id=message["id"],
        )
    assert len(
        [
            item
            for item in await service.storage.list_messages(session_id)
            if item["role"] == "tool"
        ]
    ) == 3
    assert [
        event["event_type"]
        for event in await service.storage.list_events(session_id)
    ].count("message.created") == message_event_count


@pytest.mark.asyncio
async def test_tool_budget_failure_preserves_completed_sibling_and_seals_batch(
    tmp_path: Path,
) -> None:
    provider = DraftApplyBatchProvider(
        [{}, {}],
        tool_names=["local_time", "local_time"],
    )
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    session = await service.storage.get_session(session_id)
    await service.storage.update_session_context(
        session_id,
        config={
            **session["config"],
            "max_tool_calls": 1,
            "max_model_calls": 10,
        },
    )

    await send(service, client_id, session_id, "Run both checks.")
    await wait_for_status(service, session_id, "error")

    messages = await service.storage.list_messages(session_id)
    completed = [item for item in messages if item["role"] == "tool"]
    assert len(completed) == 1
    exact_first_result = completed[0]["content"][0]
    assert exact_first_result["tool_use_id"] == "draft-apply-1"
    assert exact_first_result.get("is_error", False) is False
    events = await service.storage.list_events(session_id)
    assert [event["event_type"] for event in events].count("tool.started") == 1

    restarted = LocalLiliesService(
        service.settings,
        provider=TranscriptCaptureProvider(),
    )
    await restarted.initialize()
    await restarted._close_uncertain_tool_calls(session_id)
    closed_messages = await restarted.storage.list_messages(session_id)
    durable_results = [
        block
        for message in closed_messages
        if message["role"] == "tool"
        for block in message["content"]
    ]
    assert durable_results[0] == exact_first_result
    assert [block["tool_use_id"] for block in durable_results] == [
        "draft-apply-1",
        "draft-apply-2",
    ]
    assert durable_results[1]["is_error"] is True

    transcript = await restarted._model_messages(session_id)
    assert [message.role for message in transcript[-2:]] == [
        "assistant",
        "user",
    ]
    assert [
        block.tool_use_id for block in transcript[-1].content
    ] == ["draft-apply-1", "draft-apply-2"]


@pytest.mark.asyncio
async def test_uncertain_closure_scopes_reused_tool_call_id_to_assistant_message(
    tmp_path: Path,
) -> None:
    service, client_id = await paired_service(
        tmp_path,
        TranscriptCaptureProvider(),
    )
    session_id = await create_session(service, client_id)
    await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "run the repeated tool twice"}],
    )
    first_assistant = await service.storage.add_message(
        session_id,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "provider-reused-id",
                "name": "local_time",
                "input": {},
            }
        ],
    )
    await service._add_tool_result_message(
        session_id,
        None,
        ContentBlock(
            type="tool_result",
            tool_use_id="provider-reused-id",
            content="first exact result",
        ),
        message_id=service._tool_result_message_id(
            f"session:{session_id}",
            first_assistant["id"],
            0,
            "provider-reused-id",
        ),
    )
    second_assistant = await service.storage.add_message(
        session_id,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "provider-reused-id",
                "name": "local_time",
                "input": {},
            }
        ],
    )

    await service._close_uncertain_tool_calls(session_id)

    tool_messages = [
        item
        for item in await service.storage.list_messages(session_id)
        if item["role"] == "tool"
    ]
    assert len(tool_messages) == 2
    assert tool_messages[0]["content"][0]["content"] == "first exact result"
    assert tool_messages[1]["content"][0]["is_error"] is True
    assert tool_messages[0]["id"] != tool_messages[1]["id"]
    assert tool_messages[1]["id"] == service._tool_result_message_id(
        f"session:{session_id}",
        second_assistant["id"],
        0,
        "provider-reused-id",
    )


def draft_apply_input(
    application_id: str,
    expected_revision: int,
    index: int,
) -> dict[str, Any]:
    return {
        "application_id": application_id,
        "expected_revision": expected_revision,
        "idempotency_key": f"draft-batch:{application_id}:{index}",
        "op": "set_metadata",
        "data": {"description": f"mutation-{index}"},
    }


def test_draft_apply_success_parses_real_platform_envelope_wire_shape() -> None:
    application_id = str(uuid4())
    content_hash = "sha256:" + "a" * 64
    envelope = PlatformToolEnvelope(
        ok=True,
        operation="platform_draft_apply",
        request_id=uuid4(),
        status_code=200,
        contract_digest=ZERO_CONTRACT_DIGEST,
        data={
            "application_id": application_id,
            "revision": 4,
            "content_hash": content_hash,
            "evidence_state": "missing",
            "operation": "set_metadata",
        },
        error=None,
        evidence_refs=[],
    )
    wire_result = ContentBlock(
        type="tool_result",
        tool_use_id="real-envelope-result",
        content=PlatformHttpTool._serialize(envelope),
        is_error=False,
    )

    parsed = LocalLiliesService._draft_apply_success(wire_result)

    assert parsed is not None
    assert parsed.application_id == application_id
    assert parsed.revision == 4
    assert parsed.content_hash == content_hash


@pytest.mark.asyncio
async def test_same_turn_draft_apply_batch_chains_successful_revisions(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    provider = DraftApplyBatchProvider(
        [draft_apply_input(application_id, 0, index) for index in range(3)]
    )
    tool = FakeDraftApplyTool({application_id: 0})
    service, client_id = await paired_service(tmp_path, provider)
    service.tools.register(tool)
    session_id = await create_session(service, client_id)

    await send(service, client_id, session_id, "Apply all three draft mutations.")
    await wait_for_status(service, session_id, "ready")

    assert [call["expected_revision"] for call in tool.calls] == [0, 1, 2]
    assert tool.revisions[application_id] == 3
    messages = await service.storage.list_messages(session_id)
    original_tool_uses = next(
        item["content"]
        for item in messages
        if item["role"] == "assistant"
        and any(block.get("type") == "tool_use" for block in item["content"])
    )
    assert [
        block["input"]["expected_revision"]
        for block in original_tool_uses
    ] == [0, 0, 0]
    result_blocks = provider.seen_messages[1][-1].content
    assert [block.is_error for block in result_blocks] == [False, False, False]
    rebase_events = [
        event
        for event in await service.storage.list_events(session_id)
        if event["event_type"] == "tool.input_rebased"
    ]
    assert [
        (
            event["data"]["original_expected_revision"],
            event["data"]["effective_expected_revision"],
        )
        for event in rebase_events
    ] == [(0, 1), (0, 2)]
    assert all(
        event["data"]["reason"]
        == "prior_same_turn_same_application_draft_apply_succeeded"
        for event in rebase_events
    )


@pytest.mark.asyncio
async def test_failed_draft_apply_does_not_rebase_its_successor(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    provider = DraftApplyBatchProvider(
        [draft_apply_input(application_id, 0, index) for index in range(2)]
    )
    tool = FakeDraftApplyTool({application_id: 0}, force_failure_calls={1})
    service, client_id = await paired_service(tmp_path, provider)
    service.tools.register(tool)
    session_id = await create_session(service, client_id)

    await send(service, client_id, session_id, "Apply the draft batch.")
    await wait_for_status(service, session_id, "ready")

    assert [call["expected_revision"] for call in tool.calls] == [0, 0]
    assert [
        block.is_error for block in provider.seen_messages[1][-1].content
    ] == [True, False]
    assert not [
        event
        for event in await service.storage.list_events(session_id)
        if event["event_type"] == "tool.input_rebased"
    ]


@pytest.mark.asyncio
async def test_draft_apply_batch_preserves_explicitly_different_revision(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    provider = DraftApplyBatchProvider(
        [
            draft_apply_input(application_id, 0, 0),
            draft_apply_input(application_id, 3, 1),
        ]
    )
    tool = FakeDraftApplyTool({application_id: 0})
    service, client_id = await paired_service(tmp_path, provider)
    service.tools.register(tool)
    session_id = await create_session(service, client_id)

    await send(service, client_id, session_id, "Apply the explicitly versioned batch.")
    await wait_for_status(service, session_id, "ready")

    assert [call["expected_revision"] for call in tool.calls] == [0, 3]
    assert [
        block.is_error for block in provider.seen_messages[1][-1].content
    ] == [False, True]
    assert not [
        event
        for event in await service.storage.list_events(session_id)
        if event["event_type"] == "tool.input_rebased"
    ]


@pytest.mark.asyncio
async def test_non_draft_tool_between_mutations_breaks_revision_chain(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    provider = DraftApplyBatchProvider(
        [
            draft_apply_input(application_id, 0, 0),
            {},
            draft_apply_input(application_id, 0, 2),
        ],
        tool_names=[
            "platform_draft_apply",
            "local_time",
            "platform_draft_apply",
        ],
    )
    tool = FakeDraftApplyTool({application_id: 0})
    service, client_id = await paired_service(tmp_path, provider)
    service.tools.register(tool)
    session_id = await create_session(service, client_id)

    await send(service, client_id, session_id, "Apply, inspect time, then apply again.")
    await wait_for_status(service, session_id, "ready")

    assert [call["expected_revision"] for call in tool.calls] == [0, 0]
    results = provider.seen_messages[1][-1].content
    assert [block.is_error for block in results] == [False, False, True]
    assert not [
        event
        for event in await service.storage.list_events(session_id)
        if event["event_type"] == "tool.input_rebased"
    ]


@pytest.mark.asyncio
async def test_draft_apply_batch_never_chains_across_applications(
    tmp_path: Path,
) -> None:
    first_application_id = str(uuid4())
    second_application_id = str(uuid4())
    provider = DraftApplyBatchProvider(
        [
            draft_apply_input(first_application_id, 0, 0),
            draft_apply_input(second_application_id, 0, 1),
        ]
    )
    tool = FakeDraftApplyTool(
        {
            first_application_id: 0,
            second_application_id: 7,
        }
    )
    service, client_id = await paired_service(tmp_path, provider)
    service.tools.register(tool)
    session_id = await create_session(service, client_id)

    await send(service, client_id, session_id, "Apply mutations to both drafts.")
    await wait_for_status(service, session_id, "ready")

    assert [
        (call["application_id"], call["expected_revision"])
        for call in tool.calls
    ] == [
        (first_application_id, 0),
        (second_application_id, 0),
    ]
    assert [
        block.is_error for block in provider.seen_messages[1][-1].content
    ] == [False, True]
    assert not [
        event
        for event in await service.storage.list_events(session_id)
        if event["event_type"] == "tool.input_rebased"
    ]


@pytest.mark.asyncio
async def test_draft_apply_batch_preserves_external_revision_conflict(
    tmp_path: Path,
) -> None:
    application_id = str(uuid4())
    provider = DraftApplyBatchProvider(
        [draft_apply_input(application_id, 0, index) for index in range(2)]
    )
    tool = FakeDraftApplyTool(
        {application_id: 0},
        external_bump_after_success_calls={1},
    )
    service, client_id = await paired_service(tmp_path, provider)
    service.tools.register(tool)
    session_id = await create_session(service, client_id)

    await send(service, client_id, session_id, "Apply the draft batch.")
    await wait_for_status(service, session_id, "ready")

    assert [call["expected_revision"] for call in tool.calls] == [0, 1]
    results = provider.seen_messages[1][-1].content
    assert [block.is_error for block in results] == [False, True]
    conflict = json.loads(str(results[1].content))
    assert conflict["status_code"] == 409
    assert conflict["error"]["code"] == "revision_conflict"
    rebase_events = [
        event
        for event in await service.storage.list_events(session_id)
        if event["event_type"] == "tool.input_rebased"
    ]
    assert len(rebase_events) == 1
    assert rebase_events[0]["data"]["effective_expected_revision"] == 1


@pytest.mark.asyncio
async def test_resume_coalesces_legacy_split_tool_results_without_rewriting_history(
    tmp_path: Path,
) -> None:
    provider = TranscriptCaptureProvider()
    service, client_id = await paired_service(tmp_path, provider)
    session_id = await create_session(service, client_id)
    await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "legacy parallel request"}],
    )
    await service.storage.add_message(
        session_id,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": tool_use_id,
                "name": "local_time",
                "input": {},
            }
            for tool_use_id in ("legacy-1", "legacy-2", "legacy-3")
        ],
    )
    for tool_use_id in ("legacy-1", "legacy-2", "legacy-3"):
        await service.storage.add_message(
            session_id,
            "tool",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": f"result:{tool_use_id}",
                    "is_error": tool_use_id == "legacy-2",
                }
            ],
        )
    await service.storage.transition_session(session_id, "error")
    legacy_event_cursor = (
        await service.storage.list_events(session_id, client_id=client_id)
    )[-1]["seq"]

    await service.resume_session(
        session_id,
        SessionResumeRequest(
            idempotency_key=f"resume:{uuid4().hex}",
            expected_status=SessionStatus.error,
            reason="recover the provider transcript",
        ),
        client_id=client_id,
    )
    await wait_for_status(service, session_id, "ready")

    assert provider.calls == 1
    transcript = provider.seen_messages[0]
    assert [message.role for message in transcript] == ["user", "assistant", "user"]
    assert [
        block.tool_use_id
        for block in transcript[-1].content
        if block.type == "tool_result"
    ] == ["legacy-1", "legacy-2", "legacy-3"]
    assert transcript[-1].content[-1].type == "text"
    assert "Resume this session" in (transcript[-1].content[-1].text or "")

    durable_messages = await service.storage.list_messages(
        session_id,
        client_id=client_id,
    )
    assert len([item for item in durable_messages if item["role"] == "tool"]) == 3
    post_events = await service.storage.list_events(
        session_id,
        after=legacy_event_cursor,
        client_id=client_id,
    )
    assert post_events
    assert all(event["seq"] > legacy_event_cursor for event in post_events)


@pytest.mark.asyncio
async def test_model_message_compatibility_does_not_merge_ordinary_user_prompts(
    tmp_path: Path,
) -> None:
    service, client_id = await paired_service(tmp_path, TranscriptCaptureProvider())
    session_id = await create_session(service, client_id)
    await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "first logical prompt"}],
    )
    await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "second logical prompt"}],
    )

    transcript = await service._model_messages(session_id)

    assert [message.role for message in transcript] == ["user", "user"]
    assert [message.content[0].text for message in transcript] == [
        "first logical prompt",
        "second logical prompt",
    ]


@pytest.mark.asyncio
async def test_compacted_model_tail_keeps_tool_use_and_results_together(
    tmp_path: Path,
) -> None:
    service, client_id = await paired_service(
        tmp_path,
        TranscriptCaptureProvider(),
    )
    session_id = await create_session(service, client_id)
    await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "start the compacted task"}],
    )
    for index in range(4):
        tool_use_id = f"compacted-tool-{index}"
        await service.storage.add_message(
            session_id,
            "assistant",
            [{
                "type": "tool_use",
                "id": tool_use_id,
                "name": "local_time",
                "input": {},
            }],
        )
        await service.storage.add_message(
            session_id,
            "tool",
            [{
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": f"result-{index}",
            }],
        )
    await service.storage.add_message(
        session_id,
        "user",
        [{"type": "text", "text": "resume after compaction"}],
    )
    await service.storage.update_session_context(
        session_id,
        context_summary="durable compacted evidence",
        summary_through_event_seq=1,
    )

    transcript = await service._model_messages(session_id)

    assert transcript[0].role == "user"
    assert transcript[0].content[0].text == "start the compacted task"
    for index, message in enumerate(transcript):
        result_ids = {
            block.tool_use_id
            for block in message.content
            if block.type == "tool_result"
        }
        if not result_ids:
            continue
        assert index > 0
        previous = transcript[index - 1]
        assert previous.role == "assistant"
        request_ids = {
            block.id
            for block in previous.content
            if block.type == "tool_use"
        }
        assert result_ids == request_ids


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
