"""使用者通道：一应用一码，码不对不开门，总钥匙永不出场。"""

from __future__ import annotations

import base64
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


def test_use_channel_access_and_table_parse(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
        ),
        SilentProvider(),
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications", headers=HEADERS,
            json={"name": "对账（使用者通道测试）", "requirement": "两个表对起来，给普通用户用。"},
        ).json()["id"]

        # 未发码前：任何 code 都进不来
        assert client.get(f"/api/v1/use/{application_id}/definition?code=guess").status_code == 403

        rotated = client.post(
            f"/api/v1/applications/{application_id}/access-code", headers=HEADERS
        ).json()
        code = rotated["code"]
        assert rotated["use_path"].endswith(code)

        # 正码开门；错码闭门；无 Studio 鉴权头也能用（这就是"链接即交付"）
        definition = client.get(f"/api/v1/use/{application_id}/definition?code={code}")
        assert definition.status_code == 200
        assert definition.json()["application_name"].startswith("对账")
        assert client.get(f"/api/v1/use/{application_id}/definition?code=wrong").status_code == 403

        # 换码后旧码作废
        newcode = client.post(
            f"/api/v1/applications/{application_id}/access-code", headers=HEADERS
        ).json()["code"]
        assert client.get(f"/api/v1/use/{application_id}/definition?code={code}").status_code == 403
        assert client.get(f"/api/v1/use/{application_id}/definition?code={newcode}").status_code == 200

        # 表格进料走同一把码
        csv_bytes = "单号,金额\nPO-1,100\n".encode("utf-8")
        parsed = client.post(
            f"/api/v1/use/{application_id}/parse-table?code={newcode}",
            json={"filename": "流水.csv", "content_base64": base64.b64encode(csv_bytes).decode()},
        )
        assert parsed.status_code == 200
        assert parsed.json()["rows"] == [{"单号": "PO-1", "金额": 100}]

        # 没发布版时开跑给可读的 422，而不是 500
        run = client.post(
            f"/api/v1/use/{application_id}/runs?code={newcode}", json={"inputs": {}}
        )
        assert run.status_code in (404, 422)


def test_run_ledger_summary_flags_template_echo() -> None:
    from agent_platform.api import _summarize_run_ledger

    class Event:
        def __init__(self, type_, data):
            self.type = type_
            self.data = data

    events = [
        Event("node.completed", {"node_id": "web_search", "outputs": {"output": {"query": "x", "results": []}}}),
        Event("node.completed", {"node_id": "analyze", "outputs": {"structured": {"analysis": {"a": 1}}}}),
        Event("node.failed", {"node_id": "later", "error": "boom"}),
    ]
    ledger, suspicions = _summarize_run_ledger(
        events, {"topical_items": {"title": "示例标题"}, "note": ""}
    )
    assert "web_search" in ledger and "results=[]" in ledger
    assert "later 失败" in ledger
    assert suspicions and "疑似" in suspicions[0]

    # 上游有真数据时不误报
    healthy = [
        Event("node.completed", {"node_id": "web_search", "outputs": {"output": {"results": [{"t": 1}]}}}),
    ]
    _, clean = _summarize_run_ledger(healthy, {"topical_items": [{"t": 1}]})
    assert clean == []
