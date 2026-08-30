"""思考被截断时的"自救"最多两次——这道花钱闸此前零测试。

背景写在 builder.py 里：思考吃光输出上限（stop_reason=max_tokens）
不等于"她收工了"。金蝶盲测实况：第 3 轮 5.5 万字思考被 8192 上限掐断、
零工具调用，循环把这判成主动结束，交出一份空草稿。
所以加了"截断就再给一次机会"，并**限定连续最多救 2 次**。

变异验证（2026-08-30，全量 2706 条）漏两个：
  · `truncated_rescues < 2` 改成 `< 20` —— 全绿
  · `truncated_rescues += 1` 换成 pass（**计数器永远不动 ⇒ 永远救下去**）
    —— 也全绿

两个都是**持续付费**的方向：每一次自救都是一次完整的模型调用，
而且不会有任何报错，只是钱在走、轮次在耗。

这里用本仓已有的那套真家伙（假 provider + create_app + 真构建循环，
和 test_build_conversation 同一个路子），不搭桩塔：
provider 每轮都回"被 max_tokens 掐断且没有工具调用"，
数它到底被叫了几次。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.providers.base import (
    ChatMessage,
    ModelProvider,
    ProviderCapabilities,
    StreamEvent,
    ToolDefinition,
)

HEADERS = {"Authorization": "Bearer rescue-test", "Content-Type": "application/json"}


class AlwaysTruncatedProvider(ModelProvider):
    """每一轮都在思考里被掐断，且一个工具都不调——最坏的那种局面。"""

    name = "always-truncated"

    def __init__(self) -> None:
        self.calls = 0

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
        yield StreamEvent(type="message_start",
                          data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": "想了很多但没说完"}})
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": "想了很多但没说完"}})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 1}})


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="rescue-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        complexity_router_default_mode="disabled",
        complexity_router_limited_default_enabled=False,
    )


def _run_build(tmp_path: Path, *, max_turns: int) -> AlwaysTruncatedProvider:
    provider = AlwaysTruncatedProvider()
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications", headers=HEADERS,
            json={"name": "自救上限", "requirement": "做一个每天统计门店销量并发日报的工作流"},
        ).json()["id"]
        response = client.post(
            f"/api/v1/applications/{application_id}/builds", headers=HEADERS,
            json={"requirement": "做一个每天统计门店销量并发日报的工作流", "auto_publish": False,
                  "max_turns": max_turns},
        )
        assert response.status_code == 202, response.text
        build_id = response.json()["build_id"]
        for _ in range(3000):
            build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
            if build["status"] not in ("queued", "building"):
                break
            time.sleep(0.01)
        else:
            raise AssertionError(f"构建没结束：{build.get('status')}")
    return provider


def test_a_run_of_truncated_turns_stops_after_two_rescues(tmp_path: Path) -> None:
    """一轮正常 + 最多两次自救 = 模型最多被叫 3 次。

    max_turns 给到 20，好让"自救没上限"这件事真的能表现出来——
    上限若失效，它会一路耗到 20 轮，每一轮都是一次付费调用。
    """
    provider = _run_build(tmp_path, max_turns=20)
    assert provider.calls <= 3, f"自救没刹住：模型被叫了 {provider.calls} 次"


def test_it_does_rescue_at_least_once(tmp_path: Path) -> None:
    """反向：不能把自救整个关掉——那正是当初加它要救的那种空草稿。"""
    provider = _run_build(tmp_path, max_turns=20)
    assert provider.calls >= 2, f"一次都没救：模型只被叫了 {provider.calls} 次"


def test_the_limit_does_not_depend_on_max_turns(tmp_path: Path) -> None:
    """把 max_turns 放到 40，调用次数不该跟着涨——刹车是自救计数，不是轮数。"""
    provider = _run_build(tmp_path, max_turns=40)
    assert provider.calls <= 3, f"自救跟着轮数涨了：{provider.calls} 次"
