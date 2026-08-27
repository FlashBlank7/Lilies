"""服务端管家智能体：工具在服务端执行、动作入账、最终答复带真实数据。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


class ConciergeScript(ModelProvider):
    """第一轮点 list_workflows 工具，第二轮基于工具结果作答。"""

    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(self, *, model, system, messages, tools, max_output_tokens,
                     thinking_enabled, effort, tool_choice=None, user_id=None) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            assert any(t.name == "run_workflow" for t in tools)
            yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {
                "type": "tool_use", "id": "c1", "name": "list_workflows", "input": {}}})
            yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {
                "type": "input_json_delta", "partial_json": "{}"}})
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
            return
        payload = json.loads(messages[-1].content[0].content)
        text = f"平台上有 {payload['total']} 个已发布工作流"
        yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {"type": "text_delta", "text": text}})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


def test_concierge_executes_tools_server_side(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    app = create_app(settings, ConciergeScript())
    with TestClient(app) as client:
        r = client.post("/api/v1/assistant/agent",
                        headers={"Authorization": "Bearer workflow-test"},
                        json={"messages": [{"role": "user", "text": "有哪些工作流？"}]})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["actions"] == [{"tool": "list_workflows", "summary": "0 个工作流"}]
        assert data["text"] == "平台上有 0 个已发布工作流"


def test_overview_endpoint_shape(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    app = create_app(settings, ConciergeScript())
    with TestClient(app) as client:
        r = client.get("/api/v1/overview", headers={"Authorization": "Bearer workflow-test"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["runs_today"] == {"total": 0, "succeeded": 0, "failed": 0, "running": 0}
        assert data["schedules"] == [] and data["recent_failures"] == []
        assert data["builds_active"] == 0


class GenerateScript(ModelProvider):
    """第一轮让管家提交生成；其后任何调用都直接文本收尾（含构建循环的轮次）。"""

    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(self, *, model, system, messages, tools, max_output_tokens,
                     thinking_enabled, effort, tool_choice=None, user_id=None) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        if self.calls == 1:
            yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {
                "type": "tool_use", "id": "g1", "name": "generate_workflow",
                "input": {}}})
            yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {
                "type": "input_json_delta",
                "partial_json": json.dumps({"requirement": "输入文本，输出字数统计的工作流"})}})
            yield StreamEvent(type="content_block_stop", data={"index": 0})
            yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 1}})
            return
        yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {"type": "text_delta", "text": "已提交"}})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}})


def test_generate_action_carries_build_id_for_cli_follow(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    app = create_app(settings, GenerateScript())
    with TestClient(app) as client:
        r = client.post("/api/v1/assistant/agent",
                        headers={"Authorization": "Bearer workflow-test"},
                        json={"messages": [{"role": "user", "text": "做一个字数统计工作流"}]})
        assert r.status_code == 200, r.text
        action = r.json()["actions"][0]
        assert action["tool"] == "generate_workflow"
        assert action.get("build_id") and action.get("app_id")  # CLI 跟踪的锚点
        build = client.get(f"/api/v1/builds/{action['build_id']}",
                           headers={"Authorization": "Bearer workflow-test"})
        assert build.status_code == 200


def test_agent_stream_emits_action_and_final(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    app = create_app(settings, ConciergeScript())
    with TestClient(app) as client:
        r = client.post("/api/v1/assistant/agent/stream",
                        headers={"Authorization": "Bearer workflow-test"},
                        json={"messages": [{"role": "user", "text": "有哪些工作流？"}]})
        assert r.status_code == 200
        events = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]
        kinds = [e["type"] for e in events]
        assert "action" in kinds and kinds[-1] == "final"
        final = events[-1]
        assert final["text"] == "平台上有 0 个已发布工作流"


def test_recent_builds_and_resume_tools(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    app = create_app(settings, GenerateScript())
    with TestClient(app) as client:
        # 先经管家生成一个构建（GenerateScript 会提交后收尾，构建随即结束）
        first = client.post("/api/v1/assistant/agent",
                            headers={"Authorization": "Bearer workflow-test"},
                            json={"messages": [{"role": "user", "text": "做一个字数统计工作流"}]})
        build_id = first.json()["actions"][0]["build_id"]
        import time as _t
        for _ in range(200):
            b = client.get(f"/api/v1/builds/{build_id}", headers={"Authorization": "Bearer workflow-test"}).json()
            if b["status"] not in ("queued", "building"):
                break
            _t.sleep(0.01)

        from agent_platform.assistant_agent import WorkflowConcierge
        services = client.app.state.services
        concierge = WorkflowConcierge(services, settings)
        listed = client.portal.call(concierge._exec, "recent_builds", {}, {"name": "t"})
        assert any(item["build_id"] == build_id for item in listed["builds"])
        resumed = client.portal.call(concierge._exec, "resume_build",
                                     {"build_id": build_id, "message": "继续"}, {"name": "t"})
        assert resumed.get("status") == "queued", resumed


def test_derive_app_name_readable() -> None:
    from agent_platform.assistant_agent import _derive_app_name

    assert _derive_app_name(
        "给我做一个工作流：输入一段文本 text，输出行数。不要定时。") == "输入一段文本 text，输出行数"
    assert _derive_app_name("做一个每日销售对账，输出差异表。") == "每日销售对账，输出差异表"
    assert _derive_app_name("x" * 40) == "x" * 24
    assert _derive_app_name("") == "新工作流"


def test_health_report_tool_and_summary(monkeypatch) -> None:
    """管家能回答'有什么坏了'：工具走 build_health，动作摘要点名坏掉的工作流。"""
    from agent_platform import assistant_agent, overview
    from agent_platform.assistant_agent import WorkflowConcierge

    async def fake_health(services, days=7):
        assert days == 7
        return {
            "days": days,
            "counts": {"broken": 1, "stale": 0, "ok": 3},
            "items": [
                {"application_id": "a1", "workflow": "坏的日报", "state": "broken",
                 "reason": "近7天 6 次运行全部失败：连接超时", "runs": 6, "succeeded": 0},
                {"application_id": "a2", "workflow": "健康的", "state": "ok",
                 "reason": "", "runs": 3, "succeeded": 3},
            ],
        }

    monkeypatch.setattr(overview, "build_health", fake_health)
    settings = Settings(api_token="workflow-test")
    with TestClient(create_app(settings)) as client:
        services = client.app.state.services
        concierge = WorkflowConcierge(services, settings)
        result = client.portal.call(concierge._exec, "health_report", {}, {"name": "t"})
    assert result["counts"]["broken"] == 1
    assert [p["workflow"] for p in result["problems"]] == ["坏的日报"]  # ok 的不进 problems
    assert "连接超时" in result["problems"][0]["reason"]
    assert assistant_agent._summarize(result).startswith("⚠ 1 个要处理：坏的日报")


def test_health_summary_when_all_ok() -> None:
    from agent_platform.assistant_agent import _summarize

    assert _summarize({"counts": {"ok": 9}, "problems": []}) == "✓ 9 个工作流都正常"
