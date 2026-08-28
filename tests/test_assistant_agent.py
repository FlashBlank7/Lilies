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


def test_health_report_tool_and_summary(monkeypatch, tmp_path) -> None:
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
    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
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


def test_repair_workflow_builds_on_existing_app(tmp_path) -> None:
    """修复要开在原应用上（从现有草稿改起），需求里带上失败原因。"""
    from agent_platform.assistant_agent import WorkflowConcierge

    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    with TestClient(create_app(settings)) as client:
        services = client.app.state.services
        created = client.post("/api/v1/applications",
                              headers={"Authorization": "Bearer workflow-test"},
                              json={"name": "坏掉的日报", "requirement": "每天出一份日报"}).json()
        started: dict = {}
        services.builders.get("classic").start = lambda build_id: started.setdefault(
            "build_id", build_id)

        concierge = WorkflowConcierge(services, settings)
        result = client.portal.call(
            concierge._exec, "repair_workflow",
            {"name_or_id": "坏掉的日报", "instruction": "公式包含不支持的字符 '.'"},
            {"name": "t"})

        assert result["app_id"] == created["id"]        # 原应用，不是新建的
        assert result["repairing"] is True
        assert started["build_id"] == result["build_id"]  # 构建真的启动了
        build = client.get(f"/api/v1/builds/{result['build_id']}",
                           headers={"Authorization": "Bearer workflow-test"}).json()
        requirement = build["requirement"]
        assert "修复现有工作流" in requirement
        assert "公式包含不支持的字符" in requirement    # 失败原因进了需求
        assert "不要推倒重来" in requirement


def test_repair_workflow_pulls_reason_from_health(monkeypatch, tmp_path) -> None:
    """没给指示时自己去体检取失败原因，别让莉莉丝盲修。"""
    from agent_platform import overview
    from agent_platform.assistant_agent import WorkflowConcierge

    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    with TestClient(create_app(settings)) as client:
        services = client.app.state.services
        created = client.post("/api/v1/applications",
                              headers={"Authorization": "Bearer workflow-test"},
                              json={"name": "坏掉的日报", "requirement": "每天出一份日报"}).json()
        services.builders.get("classic").start = lambda build_id: None

        async def fake_health(_services, days=7):
            # 闸门要求：state=broken 且有可归因的 last_error 才允许自动开构建
            return {"days": days, "counts": {}, "items": [
                {"application_id": created["id"], "workflow": "坏掉的日报",
                 "state": "broken", "last_error": "连接超时",
                 "reason": "近7天 6 次运行全部失败：连接超时"}]}

        monkeypatch.setattr(overview, "build_health", fake_health)
        concierge = WorkflowConcierge(services, settings)
        result = client.portal.call(concierge._exec, "repair_workflow",
                                    {"name_or_id": "坏掉的日报"}, {"name": "t"})
        assert "连接超时" in result["instruction"]


def test_repair_workflow_unknown_name(tmp_path) -> None:
    from agent_platform.assistant_agent import WorkflowConcierge

    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    with TestClient(create_app(settings)) as client:
        concierge = WorkflowConcierge(client.app.state.services, settings)
        result = client.portal.call(concierge._exec, "repair_workflow",
                                    {"name_or_id": "不存在的"}, {"name": "t"})
        assert result["error"] == "找不到该工作流"


def test_run_workflow_reads_top_level_fields(tmp_path) -> None:
    """回归：get_run 返回的 state 是 pydantic 模型不是 dict。

    对它 .get() 会抛 AttributeError —— 真机表现是管家「跑一下 X」整轮 500，
    招牌功能直接不可用。outputs/error 都以顶层字段为准。
    """
    from agent_platform.assistant_agent import WorkflowConcierge

    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    with TestClient(create_app(settings)) as client:
        services = client.app.state.services

        class _State:  # 模拟 WorkflowRunState：没有 .get，也没有 error 字段
            outputs = {"end": {"report": "不该被读到的中间态"}}

        calls = {"n": 0}

        async def fake_get_run(run_id):
            calls["n"] += 1
            return {"id": run_id, "status": "failed", "state": _State(),
                    "outputs": {}, "error": "node start failed: missing required input: sales"}

        async def fake_create_run(application_id, body, **kwargs):
            return {"run_id": "r-1"}

        services.workflow_store.get_run = fake_get_run
        services.workflow_runtime.create_run = fake_create_run
        created = client.post("/api/v1/applications",
                              headers={"Authorization": "Bearer workflow-test"},
                              json={"name": "跑跑看", "requirement": "随便"}).json()
        assert created["id"]

        concierge = WorkflowConcierge(services, settings)
        result = client.portal.call(concierge._exec, "run_workflow",
                                    {"name_or_id": "跑跑看", "inputs": {}}, {"name": "t"})

    assert result["status"] == "failed"
    assert "missing required input: sales" in result["error"]   # 顶层 error
    assert result["outputs"] == {}                              # 不再吃 state 中间态
    assert calls["n"] == 1


def test_recent_runs_carries_error(tmp_path) -> None:
    """「问 run X 为什么失败」这条产品自己印在屏幕上的路径，之前拿不到原因。"""
    from agent_platform.assistant_agent import WorkflowConcierge

    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    with TestClient(create_app(settings)) as client:
        services = client.app.state.services
        created = client.post("/api/v1/applications",
                              headers={"Authorization": "Bearer workflow-test"},
                              json={"name": "查历史", "requirement": "随便"}).json()

        async def fake_list_runs(application_id, limit=5):
            return [{"id": "r-1", "status": "failed", "created_at": "2026-08-28",
                     "error": "node fetch failed: HTTPConnectionPool timeout"}]

        services.workflow_store.list_runs = fake_list_runs
        concierge = WorkflowConcierge(services, settings)
        result = client.portal.call(concierge._exec, "recent_runs",
                                    {"name_or_id": "查历史"}, {"name": "t"})

    assert result["runs"][0]["error"] == "HTTPConnectionPool timeout"  # 前缀已剥
    assert created["id"]


def test_repair_refuses_when_nothing_is_attributably_broken(monkeypatch, tmp_path) -> None:
    """没给指示、体检又说不出原因时，不能自动开构建——这是唯一会花钱的路径。"""
    from agent_platform import overview
    from agent_platform.assistant_agent import WorkflowConcierge

    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    with TestClient(create_app(settings)) as client:
        services = client.app.state.services
        created = client.post("/api/v1/applications",
                              headers={"Authorization": "Bearer workflow-test"},
                              json={"name": "在跑的", "requirement": "随便"}).json()
        started = []
        services.builders.get("classic").start = lambda build_id: started.append(build_id)

        async def fake_health(_services, days=7):
            return {"days": days, "counts": {}, "items": [
                {"application_id": created["id"], "workflow": "在跑的",
                 "state": "waiting", "last_error": "",
                 "reason": "有运行在进行或等待人工确认，尚无终态结果"}]}

        monkeypatch.setattr(overview, "build_health", fake_health)
        concierge = WorkflowConcierge(services, settings)
        result = client.portal.call(concierge._exec, "repair_workflow",
                                    {"name_or_id": "在跑的"}, {"name": "t"})

    assert "error" in result
    assert "waiting" in result["error"]
    assert not started, "不该启动任何构建"


def test_set_schedule_changes_time_and_republishes(tmp_path) -> None:
    """改定时是个再自然不过的需求，此前没有对应工具——实测管家绕了 10 秒、
    调了 4 个工具，最后什么都没做成。"""
    from agent_platform.assistant_agent import WorkflowConcierge

    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    headers = {"Authorization": "Bearer workflow-test"}
    with TestClient(create_app(settings)) as client:
        services = client.app.state.services
        created = client.post("/api/v1/applications", headers=headers,
                              json={"name": "每日日报", "requirement": "每天出日报"}).json()
        draft = client.get(f"/api/v1/applications/{created['id']}/draft",
                           headers=headers).json()
        client.post(f"/api/v1/applications/{created['id']}/draft", headers=headers,
                    json={"expected_revision": draft["revision"],
                          "idempotency_key": "add-sched", "op": "add_node",
                          "data": {"node": {
                              "id": "sched", "type": "schedule_trigger",
                              "title": "每天八点",
                              "config": {"hour": 8, "minute": 0,
                                         "timezone": "Asia/Shanghai", "inputs": {}}}}})

        published = {}
        async def fake_publish(app_id, acknowledge_warnings=False):
            published["app"] = app_id
            return {"version": 7}
        services.workflow_store.publish = fake_publish

        concierge = WorkflowConcierge(services, settings)
        result = client.portal.call(
            concierge._exec, "set_schedule",
            {"name_or_id": "每日日报", "hour": 7, "minute": 30}, {"name": "t"})

        after = client.get(f"/api/v1/applications/{created['id']}/draft",
                           headers=headers).json()

    assert result["before"].startswith("08:00")
    assert result["after"].startswith("07:30")
    assert result["published_version"] == 7
    assert published["app"] == created["id"]
    node = next(n for n in after["snapshot"]["workflow"]["nodes"]
                if n["type"] == "schedule_trigger")
    assert (node["config"]["hour"], node["config"]["minute"]) == (7, 30)
    assert node["config"]["timezone"] == "Asia/Shanghai"   # 没给就沿用原有


def test_set_schedule_rejects_bad_input(tmp_path) -> None:
    from agent_platform.assistant_agent import WorkflowConcierge

    settings = Settings(api_token="workflow-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    with TestClient(create_app(settings)) as client:
        services = client.app.state.services
        client.post("/api/v1/applications",
                    headers={"Authorization": "Bearer workflow-test"},
                    json={"name": "没有定时的", "requirement": "随便"})
        concierge = WorkflowConcierge(services, settings)
        call = lambda args: client.portal.call(  # noqa: E731
            concierge._exec, "set_schedule", args, {"name": "t"})

        assert "0-23" in call({"name_or_id": "没有定时的", "hour": 25})["error"]
        assert "0-59" in call({"name_or_id": "没有定时的", "hour": 8,
                               "minute": 90})["error"]
        assert "找不到" in call({"name_or_id": "不存在的", "hour": 8})["error"]
        # 没有定时节点：说清楚而不是默默失败
        assert "没有定时节点" in call({"name_or_id": "没有定时的", "hour": 8})["error"]


def test_system_prompt_carries_today() -> None:
    """不告诉它今天几号，它会从运行记录里猜「昨天」——实测猜错过。"""
    from datetime import datetime, timezone

    from agent_platform.assistant_agent import _system_prompt

    prompt = _system_prompt()
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") in prompt
    assert "别从运行记录里推算" in prompt
    assert "不要把推理过程写进回答" in prompt
