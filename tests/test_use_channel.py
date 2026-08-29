"""使用者通道：一应用一码，码不对不开门，总钥匙永不出场。"""

from __future__ import annotations

import base64
import time
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

        # 没发布版时开跑要给可读的回应，而不是 500。
        # 2026-08-29 起这一支单独回 409 并明说"还没发布"——
        # 原先它落到 create_run 的 KeyError 上，客户看到"这个应用找不到了"，
        # 于是去核对链接对不对，而应用明明在。
        # 状态码跟着旁边的"已收起来"那支走（都是 409：东西在，只是现在不能用）。
        run = client.post(
            f"/api/v1/use/{application_id}/runs?code={newcode}", json={"inputs": {}}
        )
        assert run.status_code in (404, 409, 422), run.text
        assert "还没发布" in run.json()["detail"], run.text


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


def test_repair_auto_replays_run_with_same_inputs(tmp_path: Path) -> None:
    """返修强制复现：平台自动用报障输入发起新鲜运行并写进证据（缺陷 #3）。

    盲测教训：历史错误结论会压过任何指令，只有新鲜账本能击穿旧信念。
    """

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
            json={"name": "复现测试", "requirement": "对账流程。"},
        ).json()["id"]
        # 造一个最小可发布工作流：start -> end
        from uuid import uuid4

        def mutate(revision: int, op: str, data: dict) -> int:
            response = client.post(
                f"/api/v1/applications/{application_id}/draft",
                headers=HEADERS,
                json={
                    "expected_revision": revision,
                    "idempotency_key": str(uuid4()),
                    "op": op,
                    "data": data,
                },
            )
            assert response.status_code == 200, response.text
            return response.json()["revision"]

        revision = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()["revision"]
        revision = mutate(revision, "add_node", {"node": {
            "id": "start", "type": "start", "title": "开始",
            "config": {"inputs": [{"name": "date", "label": "日期", "type": "string",
                                           "required": True, "example": "2026-08-07"}]}}})
        revision = mutate(revision, "add_node", {"node": {
            "id": "end", "type": "end", "title": "结束",
            "config": {"outputs": {"echo": {"$ref": {"node_id": "start", "path": ["date"]}}}}}})
        revision = mutate(revision, "add_edge", {"edge": {
            "id": "e1", "source": "start", "target": "end",
            "source_port": "output", "target_port": "input"}})
        published = client.post(
            f"/api/v1/applications/{application_id}/versions", headers=HEADERS,
            json={"acknowledge_warnings": True},
        )
        assert published.status_code == 200, published.text

        # repair 需要构建记录在案：造一个空转构建（SilentProvider 一轮收工）
        build_id = client.post(
            f"/api/v1/applications/{application_id}/builds", headers=HEADERS,
            json={"requirement": "把两张对账表按单号对起来给运营看。", "auto_publish": False, "max_turns": 5},
        ).json()["build_id"]
        for _ in range(400):
            status = client.get(f"/api/v1/builds/{build_id}", headers=HEADERS).json()["status"]
            if status not in {"queued", "building"}:
                break
            time.sleep(0.01)

        run = client.post(
            f"/api/v1/applications/{application_id}/runs", headers=HEADERS,
            json={"inputs": {"date": "2026-08-07"}},
        ).json()
        run_id = run["run_id"]
        # 等运行落终态
        for _ in range(200):
            record = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
            if record["status"] not in {"queued", "running"}:
                break
            time.sleep(0.01)

        before = client.get(
            f"/api/v1/applications/{application_id}/runs?limit=10", headers=HEADERS
        ).json()
        repair = client.post(
            f"/api/v1/applications/{application_id}/runs/{run_id}/repair",
            headers=HEADERS, json={"note": "结果不对"},
        )
        assert repair.status_code == 200, repair.text
        after = client.get(
            f"/api/v1/applications/{application_id}/runs?limit=10", headers=HEADERS
        ).json()
        # 平台自动多发起了一次复现运行，且输入与报障运行一致
        assert len(after) == len(before) + 1
        replay = after[0]
        assert replay["state"]["inputs"] == {"date": "2026-08-07"}
