"""测试:parallel_agents 积木(有界涌现层级 3)。

验证多个独立子智能体并行执行并聚合输出:
- 写手(writer)产出投稿草稿
- 审核(reviewer)产出审核意见
- 两者并行运行(provider 按 system prompt 区分,不依赖调用顺序)
- 输出聚合为 {writer: 草稿, reviewer: 意见}

对应 CA 的"同步并行局部更新":每个子智能体按局部规则(任务)独立运行,
全局模式(草稿+审核)从并行交互中涌现。
"""
from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import StreamEvent
from agent_platform.providers.base import ModelProvider, ProviderCapabilities

DRAFT = "【标题】并行团队的投稿\n【正文】写手独立产出的草稿。"
REVIEW = "通过。内容完整,可直接发布。"

H = {"Authorization": "Bearer workflow-test"}


class TeamProvider(ModelProvider):
    """按 system prompt 区分两个并行子智能体(并发顺序不确定,不能靠调用次数)。"""

    name = "team-scripted"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 10_000)

    async def stream(self, *, model, system, messages, tools, max_output_tokens,
                     thinking_enabled, effort, tool_choice=None, user_id=None):
        text = DRAFT if "写手" in (system or "") else REVIEW
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 10}}})
        yield StreamEvent(type="content_block_start", data={
            "index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={
            "index": 0, "delta": {"type": "text_delta", "text": text}})
        yield StreamEvent(type="content_block_stop", data={"index": 0})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}})


def mutate(client, app_id, revision, op, data):
    r = client.post(f"/api/v1/applications/{app_id}/draft", headers=H, json={
        "expected_revision": revision, "idempotency_key": f"pa-{op}-{revision}",
        "op": op, "data": data})
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def test_parallel_agents_runs_concurrently_and_aggregates() -> None:
    tmp = TemporaryDirectory()
    settings = Settings(api_token="workflow-test",
                        data_dir=Path(tmp.name) / "data",
                        workspace_root=Path(tmp.name) / "workspaces")
    settings.prepare()
    app = create_app(settings, TeamProvider())

    with TestClient(app) as client:
        app_id = client.post("/api/v1/applications", headers=H, json={
            "name": "并行双人组", "requirement": "写手与审核并行协作",
        }).json()["id"]
        rev = client.get(f"/api/v1/applications/{app_id}/draft", headers=H).json()["revision"]

        nodes = [
            {"id": "start", "type": "start", "title": "输入", "config": {
                "inputs": [{"name": "materials", "type": "string"}]}},
            {"id": "team", "type": "parallel_agents", "title": "并行团队", "config": {
                "settings": {
                    "parallelism": 2,
                    "agents": [
                        {"name": "writer",
                         "task": "根据材料撰写一篇公众号投稿。",
                         "system_prompt": "你是资深公众号写手,直接输出投稿草稿,不要解释任何过程。",
                         "budget": {"max_rounds": 2}},
                        {"name": "reviewer",
                         "task": "审核写手的投稿草稿,输出审核意见。",
                         "system_prompt": "你是公众号审核编辑,直接输出审核结论,不要解释任何过程。",
                         "budget": {"max_rounds": 2}},
                    ],
                }}},
            {"id": "end", "type": "end", "title": "输出", "config": {
                "outputs": {
                    "writer": {"$ref": {"node_id": "team", "path": ["output", "writer"]}},
                    "reviewer": {"$ref": {"node_id": "team", "path": ["output", "reviewer"]}},
                }}},
        ]
        for n in nodes:
            rev = mutate(client, app_id, rev, "add_node", {"node": n})
        for e in [
            {"id": "a", "source": "start", "target": "team", "source_port": "output", "target_port": "input"},
            {"id": "b", "source": "team", "target": "end", "source_port": "output", "target_port": "input"},
        ]:
            rev = mutate(client, app_id, rev, "add_edge", {"edge": e})

        created = client.post(f"/api/v1/applications/{app_id}/runs", headers=H, json={
            "inputs": {"materials": "并行协作的测试材料"}, "use_draft": True})
        assert created.status_code == 202, created.text
        run_id = created.json()["run_id"]
        for _ in range(300):
            run = client.get(f"/api/v1/runs/{run_id}", headers=H).json()
            if run["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.1)

        assert run["status"] == "succeeded", run.get("error", run)
        outputs = run.get("outputs", {})
        assert "草稿" in outputs.get("writer", ""), f"写手未并行产出: {outputs}"
        assert "通过" in outputs.get("reviewer", ""), f"审核未并行产出: {outputs}"

        # 层级4:并行子智能体的事件可观测
        events = client.get(f"/v1/streams/{run_id}", headers=H).json()
        started = [e for e in events if e.get("type") == "agent.started"]
        completed = [e for e in events if e.get("type") == "agent.completed"]
        assert len(started) == 2, "缺少并行 agent.started 事件"
        assert len(completed) == 2, "缺少并行 agent.completed 事件"
    tmp.cleanup()
