"""同一个被拒提案数到第几次——这个数是给模型看的，不能是假的。

反刍守卫的用处写在 builder.py 里：实测协调者对同一节点提交 61 次完全
相同的被拒配置，60 轮预算就这么烧光。所以同一 (工具,参数) 被拒第 3 次起，
反馈里追加一句强指令要它换做法。

2026-08-30 发现那一段**连着写了三遍**，三遍都在同一个 except 里顺序跑：
  · 第一次被拒 → 计数 1→2→3，于是**第一次就被告知"第 3 次"**
  · 第二次被拒 → 计数 4/5/6，同一段警告连贴三遍
给模型的数字是假的，而它正是靠这个数判断该不该换做法。
（早先那条"相邻重复语句"的 ast 扫描抓不到它：重复的是**四句一组**，
  组与组之间隔着那个 if，任何两条相邻语句都不相同。）

这里用本仓已有的那套真家伙（假 provider + create_app + 真构建循环），
让模型反复发同一个必然被拒的调用，然后读它下一轮**真正收到**的反馈。
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

HEADERS = {"Authorization": "Bearer reject-test", "Content-Type": "application/json"}
WARNING = "次提交完全相同的被拒提案"


class RepeatsARejectedCallProvider(ModelProvider):
    """每一轮都发同一个必然被拒的调用，并记下每轮收到的反馈。"""

    name = "repeats-rejected"

    def __init__(self, rounds: int = 3) -> None:
        self.rounds = rounds
        self.calls = 0
        self.feedback: list[str] = []

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
        # 把上一轮的工具结果原样记下来——反刍守卫的话就贴在这里面
        for message in messages:
            for block in message.content or []:
                text = getattr(block, "content", None) or getattr(block, "text", "") or ""
                if isinstance(text, str) and WARNING in text:
                    self.feedback.append(text)
        yield StreamEvent(type="message_start",
                          data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls <= self.rounds:
            yield StreamEvent(type="content_block_start", data={
                "index": 0,
                "content_block": {"type": "tool_use", "id": f"c{self.calls}",
                                  "name": "draft_add_node", "input": {}}})
            yield StreamEvent(type="content_block_delta", data={
                "index": 0,
                "delta": {"type": "input_json_delta",
                          "partial_json": json.dumps({"node": {"bad": True}})}})
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={
                "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
            return
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": "不试了"}})
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": "不试了"}})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="reject-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        complexity_router_default_mode="disabled",
        complexity_router_limited_default_enabled=False,
    )


def _run(tmp_path: Path, rounds: int) -> RepeatsARejectedCallProvider:
    provider = RepeatsARejectedCallProvider(rounds)
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        requirement = "做一个每天统计门店销量并发日报的工作流"
        application_id = client.post(
            "/api/v1/applications", headers=HEADERS,
            json={"name": "反刍守卫", "requirement": requirement},
        ).json()["id"]
        response = client.post(
            f"/api/v1/applications/{application_id}/builds", headers=HEADERS,
            json={"requirement": requirement, "auto_publish": False, "max_turns": 8},
        )
        assert response.status_code == 202, response.text
        build_id = response.json()["build_id"]
        for _ in range(3000):
            build = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()
            if build["status"] not in ("queued", "building"):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("构建没结束")
    return provider


def test_the_first_rejection_is_not_announced_as_the_third(tmp_path: Path) -> None:
    """**这就是那个 bug。** 只被拒一次，不该出现任何"第 N 次"的警告。"""
    provider = _run(tmp_path, rounds=1)
    assert provider.feedback == [], provider.feedback[:1]


def test_the_warning_shows_up_once_the_third_time(tmp_path: Path) -> None:
    """反向：真到第 3 次要说话——不然这道守卫等于没有。"""
    provider = _run(tmp_path, rounds=3)
    assert provider.feedback, "第 3 次被拒了却一句都没说"
    assert "第 3 次" in provider.feedback[0], provider.feedback[0][:120]


def test_the_warning_is_not_pasted_three_times(tmp_path: Path) -> None:
    """一次被拒只贴一遍。原来第二次起会连贴三遍，把反馈灌满。"""
    provider = _run(tmp_path, rounds=4)
    for text in provider.feedback:
        assert text.count(WARNING) == 1, text[:200]
