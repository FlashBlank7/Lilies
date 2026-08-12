"""业主侧工作区与导出：列文件、下载（防穿越）、导出工作流定义 JSON。"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities

HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


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


def test_workspace_files_download_and_export(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspaces"
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=workspace_root,
        ),
        SilentProvider(),
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications", headers=HEADERS,
            json={"name": "工作区导出测试", "requirement": "对账流程，测试文件列表与导出。"},
        ).json()["id"]

        # 工作区还没建：给空列表而不是报错
        empty = client.get(
            f"/api/v1/applications/{application_id}/workspace/files", headers=HEADERS
        )
        assert empty.status_code == 200
        assert empty.json() == []

        # 写两个文件（含子目录），列表按 path 排序且带 size / modified_at
        workspace = workspace_root / application_id
        (workspace / "reports").mkdir(parents=True)
        (workspace / "读我.txt").write_text("第一份文件", encoding="utf-8")
        (workspace / "reports" / "对账结果.csv").write_text(
            "单号,金额\nPO-1,100\n", encoding="utf-8"
        )

        listed = client.get(
            f"/api/v1/applications/{application_id}/workspace/files", headers=HEADERS
        )
        assert listed.status_code == 200
        files = listed.json()
        assert [item["path"] for item in files] == ["reports/对账结果.csv", "读我.txt"]
        for item in files:
            assert item["size"] > 0
            assert item["modified_at"]

        # 下载内容与写入一致（子目录里的文件也能下）
        downloaded = client.get(
            f"/api/v1/applications/{application_id}/workspace/files/reports/对账结果.csv",
            headers=HEADERS,
        )
        assert downloaded.status_code == 200
        assert downloaded.content.decode("utf-8") == "单号,金额\nPO-1,100\n"

        # ../ 穿越：工作区外的文件必须 404
        (workspace_root / "外面的秘密.txt").write_text("不该被下载", encoding="utf-8")
        escaped = client.get(
            f"/api/v1/applications/{application_id}/workspace/files/..%2F外面的秘密.txt",
            headers=HEADERS,
        )
        assert escaped.status_code == 404

        # 不存在的文件也是 404
        missing = client.get(
            f"/api/v1/applications/{application_id}/workspace/files/没有这个文件.txt",
            headers=HEADERS,
        )
        assert missing.status_code == 404

        # 导出：application / workflow / views 三键齐全，带附件下载头
        export = client.get(
            f"/api/v1/applications/{application_id}/export", headers=HEADERS
        )
        assert export.status_code == 200
        assert export.headers["content-disposition"] == (
            f'attachment; filename="workflow-{application_id}.json"'
        )
        payload = export.json()
        assert set(payload) == {"application", "workflow", "views"}
        assert payload["application"]["id"] == application_id
        assert payload["application"]["name"] == "工作区导出测试"
        assert payload["application"]["source"] == "draft"
        assert isinstance(payload["workflow"], dict)
        assert isinstance(payload["views"], list)

        # 不存在的应用导出给 404
        assert (
            client.get("/api/v1/applications/no-such-app/export", headers=HEADERS).status_code
            == 404
        )
