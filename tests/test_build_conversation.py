"""Conversational build loop: ask_owner pauses, live messages reach the next turn."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}
QUESTION = "输出需要哪些字段？金额匹配容差是多少？"
ANSWER = "输出 order_no 和 amount，容差 0.01。"
LIVE_NOTE = "顺便：报表每行要带日期字段。"


def _tool_use(index: int, call_id: str, name: str, payload: dict[str, object]) -> list[StreamEvent]:
    return [
        StreamEvent(type="content_block_start", data={
            "index": index,
            "content_block": {"type": "tool_use", "id": call_id, "name": name, "input": {}},
        }),
        StreamEvent(type="content_block_delta", data={
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(payload)},
        }),
        StreamEvent(type="content_block_stop", data={"index": index}),
    ]


def _text(index: int, text: str) -> list[StreamEvent]:
    return [
        StreamEvent(type="content_block_start", data={
            "index": index,
            "content_block": {"type": "text", "text": text},
        }),
        StreamEvent(type="content_block_delta", data={
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }),
    ]


def _last_user_text(messages: list[ChatMessage]) -> str:
    return "".join(
        getattr(block, "text", "") or ""
        for block in messages[-1].content
        if getattr(block, "type", "") == "text"
    )


class AskOwnerProvider(ModelProvider):
    name = "ask-owner-provider"

    def __init__(self) -> None:
        self.calls = 0
        self.resume_prompts: list[str] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

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
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            for event in _tool_use(0, "ask-1", "ask_owner", {"question": QUESTION}):
                yield event
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })
            return
        self.resume_prompts.append(_last_user_text(messages))
        for event in _text(0, "收到，继续。"):
            yield event
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1},
        })


class LiveMessageProvider(ModelProvider):
    name = "live-message-provider"

    def __init__(self) -> None:
        self.calls = 0
        self.release = False
        self.turn2_prompt = ""

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

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
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            # Hold turn 1 open until the test has posted a live owner message.
            while not self.release:
                await asyncio.sleep(0.01)
            for event in _tool_use(0, "inspect-1", "draft_inspect", {}):
                yield event
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })
            return
        self.turn2_prompt = _last_user_text(messages)
        for event in _text(0, "done"):
            yield event
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1},
        })


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        complexity_router_default_mode="disabled",
        complexity_router_limited_default_enabled=False,
    )


def _wait_status(client: TestClient, build_id: str, statuses: set[str]) -> dict[str, object]:
    build: dict[str, object] = {}
    for _ in range(800):
        build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
        if build["status"] in statuses:
            return build
        time.sleep(0.01)
    raise AssertionError(f"timeout waiting for {statuses}, last: {build.get('status')}")


def _start_build(client: TestClient, requirement: str) -> str:
    application_id = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={"name": "会话闭环", "requirement": requirement},
    ).json()["id"]
    response = client.post(
        f"/api/v1/applications/{application_id}/builds",
        headers=HEADERS,
        json={"requirement": requirement, "auto_publish": False, "max_turns": 5},
    )
    assert response.status_code == 202, response.text
    return response.json()["build_id"]


def test_ask_owner_pauses_build_and_resume_carries_question_and_answer(tmp_path: Path) -> None:
    provider = AskOwnerProvider()
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        build_id = _start_build(client, "做一个采购订单对账工作流。")

        build = _wait_status(client, build_id, {"needs_attention"})
        assert build["team_state"]["pending_question"] == QUESTION
        assert not build.get("error")

        transcript = client.get(f"/api/v1/builds/{build_id}/transcript", headers=HEADERS).json()
        kinds = [record.get("kind") for record in transcript["records"]]
        assert kinds[0] == "owner"  # the kickoff requirement is part of the conversation
        assert any(
            call["tool"] == "ask_owner"
            for record in transcript["records"]
            for call in record.get("tool_calls") or []
        )

        response = client.post(
            f"/api/v1/builds/{build_id}/resume",
            headers=HEADERS,
            json={"message": ANSWER},
        )
        assert response.status_code == 200, response.text

        build = _wait_status(client, build_id, {"needs_attention", "ready", "published"})
        assert not build["team_state"]["pending_question"]
        assert provider.resume_prompts, "resumed turn never reached the provider"
        prompt = provider.resume_prompts[0]
        assert "The owner replied" in prompt
        assert QUESTION in prompt
        assert ANSWER in prompt

        transcript = client.get(f"/api/v1/builds/{build_id}/transcript", headers=HEADERS).json()
        kinds = [record.get("kind") for record in transcript["records"]]
        assert kinds.count("owner") == 2  # kickoff + the owner's answer


def test_live_message_reaches_next_coordinator_turn(tmp_path: Path) -> None:
    provider = LiveMessageProvider()
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        build_id = _start_build(client, "做一个日报统计报表工作流。")

        _wait_status(client, build_id, {"building"})
        response = client.post(
            f"/api/v1/builds/{build_id}/messages",
            headers=HEADERS,
            json={"message": LIVE_NOTE},
        )
        assert response.status_code == 200, response.text
        assert response.json()["delivered"] is True
        provider.release = True

        build = _wait_status(client, build_id, {"needs_attention", "ready"})
        assert "The owner sent new instructions" in provider.turn2_prompt
        assert LIVE_NOTE in provider.turn2_prompt

        transcript = client.get(f"/api/v1/builds/{build_id}/transcript", headers=HEADERS).json()
        kinds = [record.get("kind") for record in transcript["records"]]
        assert kinds.count("owner") == 2  # kickoff + the live note

        # Terminal builds reject the live channel and point at /resume instead.
        response = client.post(
            f"/api/v1/builds/{build_id}/messages",
            headers=HEADERS,
            json={"message": "再改一点"},
        )
        assert response.status_code == 409
