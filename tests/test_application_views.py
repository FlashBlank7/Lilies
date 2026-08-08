"""界面方案：同一工作流按节点显隐标注长出不同使用界面。

三条铁律：
1. 隐藏发生在服务端投影——被隐藏环节的输出根本不出后端；
2. 终端节点（end/answer）是交付合同，永远可见；
3. 零标注也有像样的默认界面（水管环节自动隐藏，业务环节自动可见）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.customer_runtime_projection import (
    AUTO_VIEW_CHAT,
    AUTO_VIEW_SIMPLE,
    auto_view_tabs,
    default_hidden_nodes,
    project_view_definition,
    project_view_run,
    resolve_view_layout,
    synthesize_auto_view,
)
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities

HEADERS = {"Authorization": "Bearer view-test", "Content-Type": "application/json"}


class SilentProvider(ModelProvider):
    name = "silent-provider"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

    async def stream(
        self, *, model: str, system: str, messages: list[ChatMessage],
        tools: list[ToolDefinition], max_output_tokens: int, thinking_enabled: bool,
        effort: str, tool_choice: dict[str, str] | None = None, user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 1}}})
        yield StreamEvent(type="message_delta", data={
            "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1},
        })


SNAPSHOT = {
    "workflow": {
        "nodes": [
            {"id": "start", "type": "start", "title": "用户输入"},
            {"id": "search", "type": "web_search", "title": "行业检索"},
            {"id": "shape", "type": "template_transform", "title": "整形"},
            {"id": "judge", "type": "llm", "title": "风险判断"},
            {"id": "end", "type": "end", "title": "输出"},
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "search"},
            {"id": "e2", "source": "search", "target": "shape"},
            {"id": "e3", "source": "shape", "target": "judge"},
            {"id": "e4", "source": "judge", "target": "end"},
        ],
    }
}

RUN = {
    "id": "run-1",
    "status": "succeeded",
    # 真实存储形状：run.outputs 顶层已扁平成终端字段，逐节点账本在 state.outputs。
    "outputs": {"verdict": "有风险", "confidence": 0.9},
    "state": {
        "snapshot": SNAPSHOT,
        "outputs": {
            "search": {"results": [{"title": "真条目"}], "query": "内部检索词"},
            "shape": {"text": "内部中间产物"},
            "judge": {"text": "有风险"},
            "end": {"verdict": "有风险", "confidence": 0.9},
        },
    },
}


def test_default_view_hides_plumbing_shows_business() -> None:
    hidden = set(default_hidden_nodes(SNAPSHOT))
    assert "shape" in hidden and "start" in hidden and "end" in hidden
    assert "search" not in hidden and "judge" not in hidden

    view = project_view_definition(SNAPSHOT, None)
    stage_ids = [node["id"] for node in view["stage_nodes"]]
    assert stage_ids == ["search", "judge"]  # 拓扑序
    assert view["layout"] == "form"


def test_chat_layout_resolved_from_answer_node() -> None:
    chat_snapshot = json.loads(json.dumps(SNAPSHOT))
    chat_snapshot["workflow"]["nodes"][-1] = {"id": "end", "type": "answer", "title": "回答"}
    assert resolve_view_layout(chat_snapshot, "auto") == "chat"
    assert resolve_view_layout(chat_snapshot, "form") == "form"
    assert resolve_view_layout(SNAPSHOT, "auto") == "form"


def test_hidden_stage_outputs_never_leave_backend() -> None:
    view = {"view_id": "operator", "name": "一线极简", "layout": "form",
            "hidden_nodes": ["start", "shape", "search"]}
    projected = project_view_run(RUN, view)
    blob = json.dumps(projected, ensure_ascii=False)
    # 隐藏环节（search/shape）的输出一个字都不许出现
    assert "内部检索词" not in blob and "内部中间产物" not in blob and "真条目" not in blob
    # 终端输出（合同）永远在
    assert projected["outputs"] == {"verdict": "有风险", "confidence": 0.9}
    # 可见环节只剩 judge
    assert [stage["node_id"] for stage in projected["stages"]] == ["judge"]
    assert projected["stages"][0]["outputs"] == {"text": "有风险"}


def test_view_crud_and_use_channel_projection(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            api_token="view-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
        ),
        SilentProvider(),
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications", headers=HEADERS,
            json={"name": "界面方案测试", "requirement": "有中间环节的流程。"},
        ).json()["id"]

        # 节点清单 + 默认隐藏推导可供编辑器渲染
        inventory = client.get(
            f"/api/v1/applications/{application_id}/views", headers=HEADERS
        ).json()
        assert "nodes" in inventory and "default_hidden_nodes" in inventory
        assert inventory["views"] == []
        # 编辑器拿到自动界面全量配置（至少有管理界面），存储值覆盖在其上展示
        assert inventory["auto_views"][0]["storage_id"] == "default"
        assert inventory["auto_views"][0]["name"] == "管理界面"

        # 建两套视图
        put = client.put(
            f"/api/v1/applications/{application_id}/views/operator", headers=HEADERS,
            json={"name": "一线操作", "layout": "form", "hidden_nodes": ["a", "b"]},
        )
        assert put.status_code == 200
        client.put(
            f"/api/v1/applications/{application_id}/views/manager", headers=HEADERS,
            json={"name": "主管审查", "layout": "auto", "hidden_nodes": []},
        )
        views = client.get(
            f"/api/v1/applications/{application_id}/views", headers=HEADERS
        ).json()["views"]
        assert {v["view_id"] for v in views} == {"operator", "manager"}

        # 非法视图标识给可读 422
        bad = client.put(
            f"/api/v1/applications/{application_id}/views/主管", headers=HEADERS,
            json={"name": "x", "layout": "form", "hidden_nodes": []},
        )
        assert bad.status_code == 422

        # GET 取码不轮换：两次同码；use definition 带视图投影
        code1 = client.get(
            f"/api/v1/applications/{application_id}/access-code", headers=HEADERS
        ).json()["code"]
        code2 = client.get(
            f"/api/v1/applications/{application_id}/access-code", headers=HEADERS
        ).json()["code"]
        assert code1 == code2

        definition = client.get(
            f"/api/v1/use/{application_id}/definition?code={code1}&view=operator"
        )
        assert definition.status_code == 200
        assert definition.json()["view"]["view_id"] == "operator"
        # WaaS 标签栏：自动生成的管理界面排第一，业主命名的界面跟在后面
        tabs = definition.json()["views"]
        assert tabs[0]["name"] == "管理界面"
        assert {tab["view_id"] for tab in tabs} == {"", "operator", "manager"}
        # 查不到的视图回落（默认视图或自动推导），不 404
        fallback = client.get(
            f"/api/v1/use/{application_id}/definition?code={code1}&view=ghost"
        )
        assert fallback.status_code == 200

        # 删除
        assert client.delete(
            f"/api/v1/applications/{application_id}/views/manager", headers=HEADERS
        ).status_code == 200
        views = client.get(
            f"/api/v1/applications/{application_id}/views", headers=HEADERS
        ).json()["views"]
        assert {v["view_id"] for v in views} == {"operator"}


def test_every_workflow_gets_auto_views() -> None:
    """每个工作流自动生成一组界面：管理 + 极简 +（模型类）对话——标注只是定制。"""

    tabs = auto_view_tabs(SNAPSHOT)
    # judge 是 llm 环节 → 支持对话；search/shape/judge 是中间环节 → 有极简
    assert [(t["view_id"], t["name"], t["layout"]) for t in tabs] == [
        ("", "管理界面", "form"),
        (AUTO_VIEW_SIMPLE, "极简界面", "form"),
        (AUTO_VIEW_CHAT, "对话界面", "chat"),
    ]

    # answer 终端的工作流：管理界面本身就是对话形态，不再重复"对话界面"标签
    chat_snapshot = json.loads(json.dumps(SNAPSHOT))
    chat_snapshot["workflow"]["nodes"][-1] = {"id": "end", "type": "answer", "title": "回答"}
    chat_tabs = auto_view_tabs(chat_snapshot)
    assert [t["view_id"] for t in chat_tabs] == ["", AUTO_VIEW_SIMPLE]
    assert chat_tabs[0]["layout"] == "chat"


def test_auto_simple_view_hides_every_stage() -> None:
    simple = synthesize_auto_view(SNAPSHOT, AUTO_VIEW_SIMPLE)
    assert simple is not None
    assert set(simple["hidden_nodes"]) == {"search", "shape", "judge"}

    projected = project_view_run(RUN, simple)
    assert projected["stages"] == []
    assert projected["outputs"] == {"verdict": "有风险", "confidence": 0.9}

    # 对话自动界面只对模型类工作流存在
    no_model = json.loads(json.dumps(SNAPSHOT))
    for node in no_model["workflow"]["nodes"]:
        if node["type"] == "llm":
            node["type"] = "record_match"
    assert synthesize_auto_view(no_model, AUTO_VIEW_CHAT) is None
