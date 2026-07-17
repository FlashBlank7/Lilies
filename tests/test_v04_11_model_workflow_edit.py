from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


class WorkflowEditProvider(ModelProvider):
    name = "scripted-workflow-edit"

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.calls: list[dict[str, object]] = []

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, False, False, False, False, 100_000, 8_000)

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
        self.calls.append({
            "model": model,
            "system": system,
            "message": messages[0].content[0].text if messages and messages[0].content else "",
            "tools": tools,
            "user_id": user_id,
        })
        text = json.dumps(self.payload, ensure_ascii=False)
        yield StreamEvent(type="message_start", data={"message": {"usage": {"input_tokens": 23}}})
        yield StreamEvent(type="content_block_start", data={"index": 0, "content_block": {"type": "text", "text": ""}})
        yield StreamEvent(type="content_block_delta", data={"index": 0, "delta": {"type": "text_delta", "text": text}})
        yield StreamEvent(type="message_delta", data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 31}})


def mutate(client: TestClient, application_id: str, revision: int, op: str, data: dict) -> int:
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


def test_unmatched_whole_workflow_edit_uses_model_and_validates_operations(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "update_node_description",
        "message": "Update the referenced result node description.",
        "operations": [{
            "op": "update_node",
            "data": {
                "node_id": "summarize",
                "changes": {
                    "description": "根据主题生成面向客户的简洁中文总结。",
                },
                "merge_config": True,
            },
        }],
        "warnings": [],
    })
    app = create_app(Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    ), provider)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "AI edit", "requirement": "Summarize a customer topic."},
        )
        application_id = created.json()["id"]
        revision = mutate(client, application_id, 0, "add_node", {
            "node": {
                "id": "summarize",
                "type": "llm",
                "title": "Summary",
                "description": "Old description",
                "config": {"prompt": "Summarize {{ topic }}"},
            },
        })
        before = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()

        response = client.post(
            f"/api/v1/applications/{application_id}/draft/preview-patch",
            headers=HEADERS,
            json={
                "instruction": "让 Summary 积木更清楚地说明它要生成面向客户的中文总结，其他部分保持不变。",
                "reference_node_ids": ["summarize"],
            },
        )

        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["supported"] is True, preview
        assert preview["intent"] == "update_node_description"
        assert preview["operations"] == [{
            "expected_revision": revision,
            "op": "update_node",
            "data": {
                "node_id": "summarize",
                "changes": {"description": "根据主题生成面向客户的简洁中文总结。"},
                "merge_config": True,
            },
        }]
        assert "AI-generated whole-workflow preview" in preview["warnings"][-1]
        assert provider.calls
        assert "Preserve everything the user did not ask to change" in str(provider.calls[0]["system"])
        assert '"reference_node_ids": ["summarize"]' in str(provider.calls[0]["message"])

        after = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after["revision"] == before["revision"]
        assert after["content_hash"] == before["content_hash"]
        tasks = client.get(
            "/api/v1/platform/harness/tasks",
            headers=HEADERS,
            params={"kind": "draft_patch_preview"},
        ).json()
        assert tasks[0]["metadata"]["preview_source"] == "model"
        assert tasks[0]["metadata"]["operation_count"] == 1
        assert tasks[0]["usage_counts"]["model_call"] == 1
