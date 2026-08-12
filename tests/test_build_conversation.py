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
    # 高负载全量回归下 8s 不够（曾偶发超时）——放宽到 30s
    for _ in range(3000):
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
        # 竞争点根治：status=building 落库发生在第 1 轮开场收信（_live_messages.pop）
        # 之前，中间还隔着若干 await。高负载下若此刻立刻投递留言，会被第 1 轮收走、
        # 进不了第 2 轮的提示词。等 provider 真正开始第 1 轮流式调用（calls 自增是
        # stream 体第一行，此时第 1 轮收信必已执行完）再投递，留言就只能落到第 2 轮。
        for _ in range(3000):
            if provider.calls >= 1:
                break
            time.sleep(0.01)
        assert provider.calls >= 1, "第 1 轮模型调用迟迟未开始"
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


def test_event_record_lands_in_transcript_as_system_badge(tmp_path: Path) -> None:
    """发布/等待/取消这类大事件必须以 kind=event 进入会话流，业主不该猜"到底发布没有"。"""

    from agent_platform.build_transcript import BuildTranscriptStore, event_record

    store = BuildTranscriptStore(tmp_path / "transcripts")
    store.append("b-1", event_record(text="工作流已发布为正式版 v2，现在可以试运行了", event="published"))
    store.append("b-1", event_record(text="莉莉丝暂停了搭建，等你回复上面的问题后继续", event="waiting_owner"))

    records = store.read("b-1")
    assert [r["kind"] for r in records] == ["event", "event"]
    assert records[0]["event"] == "published"
    assert "已发布" in records[0]["text"]
    # 事件也计入默认读取窗口（turn=1 > after_turn=0），且不冒充模型轮
    summary = store.summary("b-1")
    assert summary["turn_count"] == 0


class DefineViewProvider(ModelProvider):
    """第 1 轮就定义界面方案（含一个不存在的节点 id），第 2 轮收工。"""

    name = "define-view-provider"

    def __init__(self) -> None:
        self.calls = 0
        self.view_result = ""

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

    async def stream(
        self, *, model: str, system: str, messages: list[ChatMessage],
        tools: list[ToolDefinition], max_output_tokens: int, thinking_enabled: bool,
        effort: str, tool_choice: dict[str, str] | None = None, user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            for event in _tool_use(0, "view-1", "define_view", {
                "view_id": "operator",
                "name": "一线极简",
                "layout": "form",
                "hidden_nodes": ["ghost-node"],
            }):
                yield event
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1},
            })
            return
        self.view_result = json.dumps([
            getattr(block, "content", None) or getattr(block, "text", "")
            for message in messages for block in message.content
            if getattr(block, "type", "") in ("tool_result", "text")
        ], ensure_ascii=False, default=str)
        for event in _text(0, "界面已定义。"):
            yield event
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1},
        })


def test_builder_define_view_lands_in_storage(tmp_path: Path) -> None:
    provider = DefineViewProvider()
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        build_id = _start_build(client, "给一线操作员一个只看结论的界面。")
        _wait_status(client, build_id, {"succeeded", "needs_attention", "failed", "ready", "published"})

        application_id = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()["application_id"]
        views = client.get(
            f"/api/v1/applications/{application_id}/views", headers=HEADERS
        ).json()["views"]
        assert len(views) == 1
        view = views[0]
        assert view["view_id"] == "operator"
        assert view["name"] == "一线极简"
        # 不存在的节点 id 被过滤，不进方案，也不让调用失败
        assert view["hidden_nodes"] == []
        assert "unknown_nodes_ignored" in provider.view_result


def test_history_cost_gates_trim_and_archive() -> None:
    """上下文成本闸门：工具结果入史即截断，老轮次归档——账单病的两刀。"""

    from agent_platform.builder import (
        TOOL_RESULT_HISTORY_MAX_CHARS,
        TOOL_RESULT_KEEP_RECENT_TURNS,
        WorkflowBuilder,
    )
    from agent_platform.models import ContentBlock

    # 第一刀：超长结果截断且带"可重查"尾注；短结果原样
    big = "x" * (TOOL_RESULT_HISTORY_MAX_CHARS + 5_000)
    trimmed = WorkflowBuilder._trim_for_history(big)
    assert len(trimmed) < len(big) and "重新查询" in trimmed
    assert WorkflowBuilder._trim_for_history("short") == "short"

    # 第二刀：只保最近 N 轮工具结果，更早的归档成占位行（结构配对保留）
    def tool_turn(payload: str) -> list[ChatMessage]:
        return [
            ChatMessage(role="assistant", content=[ContentBlock(
                type="tool_use", id="t", name="draft_inspect", input={},
            )]),
            ChatMessage(role="user", content=[ContentBlock(
                type="tool_result", tool_use_id="t", content=payload,
            )]),
        ]

    messages: list[ChatMessage] = []
    total = TOOL_RESULT_KEEP_RECENT_TURNS + 3
    for index in range(total):
        messages.extend(tool_turn(f"payload-{index}-" + "y" * 500))
    WorkflowBuilder._compact_history(messages)
    payloads = [
        block.content
        for message in messages if message.role == "user"
        for block in message.content if getattr(block, "type", "") == "tool_result"
    ]
    archived = [p for p in payloads if "已归档" in str(p)]
    kept = [p for p in payloads if "已归档" not in str(p)]
    assert len(archived) == 3
    assert len(kept) == TOOL_RESULT_KEEP_RECENT_TURNS
    # 最近的轮次原样保留
    assert f"payload-{total - 1}-" in str(kept[-1])


def test_budget_note_stays_out_of_persistent_history_and_system() -> None:
    """缓存纪律：预算遥测临时并入末尾 user 消息，持久历史与 system 都不许碰。"""

    from agent_platform.builder import WorkflowBuilder
    from agent_platform.models import ContentBlock

    base = [
        ChatMessage(role="user", content=[ContentBlock(type="text", text="需求")]),
        ChatMessage(role="assistant", content=[ContentBlock(type="tool_use", id="t", name="draft_inspect", input={})]),
        ChatMessage(role="user", content=[ContentBlock(type="tool_result", tool_use_id="t", content="{}")]),
    ]
    merged = WorkflowBuilder._with_budget_note(base, "budget: turn 3/40")
    # 原列表未被修改（持久历史干净）
    assert len(base[-1].content) == 1
    # 遥测出现在临时副本末尾 user 消息的追加 text 块里
    assert merged[-1].role == "user"
    assert [b.type for b in merged[-1].content] == ["tool_result", "text"]
    assert "turn 3/40" in merged[-1].content[-1].text
    # 空遥测原样返回
    assert WorkflowBuilder._with_budget_note(base, "") is base
    # 末尾是 assistant 时另起一条 user 消息
    tail_assistant = base[:2]
    appended = WorkflowBuilder._with_budget_note(tail_assistant, "note")
    assert appended[-1].role == "user" and appended[-1].content[0].text == "note"


def test_read_labeled_rows_parses_and_rejects_escape(tmp_path: Path) -> None:
    """训练工具的数据入口：JSON/JSONL 两种形态、缺省单位补齐、越界即拒。"""

    import pytest as _pytest

    from agent_platform.builder import WorkflowBuilder
    from agent_platform.config import Settings
    from agent_platform.sandbox import SandboxManager

    settings = Settings(
        api_token="t", data_dir=tmp_path / "d", workspace_root=tmp_path / "w",
    )
    settings.prepare() if hasattr(settings, "prepare") else None
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    manager = SandboxManager(settings)

    class Stub:
        sandboxes = manager

    workspace = manager.resolve_workspace("app-1", create=True)
    (workspace / "rows.jsonl").write_text(
        '{"features": {"a": 1.5, "b": 2}, "label": 1}\n'
        '{"features": {"a": 0.5, "b": 9}, "units": {"a": "m/s"}, "label": 0}\n',
        encoding="utf-8",
    )
    rows = WorkflowBuilder._read_labeled_rows(Stub(), "app-1", "rows.jsonl")
    assert len(rows) == 2
    assert rows[0].features == {"a": 1.5, "b": 2.0}
    assert rows[0].units == {"a": "unitless", "b": "unitless"}
    assert rows[1].units["a"] == "m/s" and rows[1].units["b"] == "unitless"

    with _pytest.raises(RuntimeError, match="工作区内"):
        WorkflowBuilder._read_labeled_rows(Stub(), "app-1", "../escape.json")
