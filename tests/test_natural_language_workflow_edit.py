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


HEADERS = {
    "Authorization": "Bearer workflow-test",
    "Content-Type": "application/json",
}


class WorkflowEditProvider(ModelProvider):
    name = "natural-language-workflow-edit"

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
            "system": system,
            "message": messages[0].content[0].text if messages else "",
            "user_id": user_id,
        })
        text = json.dumps(self.payload, ensure_ascii=False)
        yield StreamEvent(
            type="message_start",
            data={"message": {"usage": {"input_tokens": 17}}},
        )
        yield StreamEvent(
            type="content_block_start",
            data={"index": 0, "content_block": {"type": "text", "text": ""}},
        )
        yield StreamEvent(
            type="content_block_delta",
            data={"index": 0, "delta": {"type": "text_delta", "text": text}},
        )
        yield StreamEvent(
            type="message_delta",
            data={"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 19}},
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )


def _mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    op: str,
    data: dict[str, object],
) -> int:
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
    return int(response.json()["revision"])


def _seed(client: TestClient) -> tuple[str, dict[str, object]]:
    created = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={
            "name": "Natural-language edit",
            "requirement": "Summarize one customer request.",
        },
    )
    assert created.status_code == 201, created.text
    application_id = created.json()["id"]
    revision = 0
    for node in (
        {
            "id": "start",
            "type": "start",
            "title": "Start",
            "config": {"inputs": [{"name": "request", "type": "string"}]},
        },
        {
            "id": "summarize",
            "type": "llm",
            "title": "Summarize",
            "description": "Create a short summary.",
            "config": {"prompt": "Summarize {{ request }}"},
        },
        {
            "id": "answer",
            "type": "answer",
            "title": "Answer",
            "config": {"answer": {"$ref": {"node_id": "summarize", "path": ["text"]}}},
        },
    ):
        revision = _mutate(
            client,
            application_id,
            revision,
            "add_node",
            {"node": node},
        )
    for edge in (
        {"id": "start-summary", "source": "start", "target": "summarize"},
        {"id": "summary-answer", "source": "summarize", "target": "answer"},
    ):
        revision = _mutate(
            client,
            application_id,
            revision,
            "add_edge",
            {"edge": edge},
        )
    draft = client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()
    assert draft["revision"] == revision
    return application_id, draft


def _seed_parallel_branches(
    client: TestClient,
    *,
    include_cross_edge: bool = False,
) -> tuple[str, dict[str, object]]:
    application_id, draft = _seed(client)
    revision = int(draft["revision"])
    for node in (
        {
            "id": "start2",
            "type": "start",
            "title": "Start 2",
            "config": {"inputs": [{"name": "request2", "type": "string"}]},
        },
        {
            "id": "summarize2",
            "type": "llm",
            "title": "Summarize 2",
            "description": "Create another short summary.",
            "config": {"prompt": "Summarize {{ request2 }}"},
        },
        {
            "id": "answer2",
            "type": "answer",
            "title": "Answer 2",
            "config": {
                "answer": {
                    "$ref": {
                        "node_id": "summarize2",
                        "path": ["text"],
                    },
                },
            },
        },
    ):
        revision = _mutate(
            client,
            application_id,
            revision,
            "add_node",
            {"node": node},
        )
    edges = [
        {
            "id": "start2-summary2",
            "source": "start2",
            "target": "summarize2",
        },
        {
            "id": "summary2-answer2",
            "source": "summarize2",
            "target": "answer2",
        },
    ]
    if include_cross_edge:
        edges.append({
            "id": "start-answer2-existing",
            "source": "start",
            "target": "answer2",
        })
    for edge in edges:
        revision = _mutate(
            client,
            application_id,
            revision,
            "add_edge",
            {"edge": edge},
        )
    draft = client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()
    assert draft["revision"] == revision
    return application_id, draft


def _seed_selected_chain(
    client: TestClient,
) -> tuple[str, dict[str, object]]:
    application_id, draft = _seed(client)
    revision = int(draft["revision"])
    revision = _mutate(
        client,
        application_id,
        revision,
        "add_node",
        {
            "node": {
                "id": "polish",
                "type": "llm",
                "title": "Polish",
                "description": "Polish the summary.",
                "config": {"prompt": "Polish the summary."},
            },
        },
    )
    revision = _mutate(
        client,
        application_id,
        revision,
        "remove_edge",
        {"edge_id": "summary-answer"},
    )
    for edge in (
        {
            "id": "summary-polish",
            "source": "summarize",
            "target": "polish",
        },
        {
            "id": "polish-answer",
            "source": "polish",
            "target": "answer",
        },
    ):
        revision = _mutate(
            client,
            application_id,
            revision,
            "add_edge",
            {"edge": edge},
        )
    revision = _mutate(
        client,
        application_id,
        revision,
        "update_node",
        {
            "node_id": "answer",
            "changes": {
                "config": {
                    "answer": {
                        "$ref": {
                            "node_id": "polish",
                            "path": ["text"],
                        },
                    },
                },
            },
            "merge_config": True,
        },
    )
    draft = client.get(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
    ).json()
    assert draft["revision"] == revision
    return application_id, draft


def _request(
    draft: dict[str, object],
    *,
    instruction: str,
    node_ids: list[str],
    edge_ids: list[str] | None = None,
    idempotency_key: str = "natural-edit-key-0001",
    preview_only: bool = True,
) -> dict[str, object]:
    return {
        "instruction": instruction,
        "node_ids": node_ids,
        "edge_ids": edge_ids or [],
        "expected_revision": draft["revision"],
        "expected_content_hash": draft["content_hash"],
        "idempotency_key": idempotency_key,
        "preview_only": preview_only,
    }


def test_selected_natural_language_edit_previews_then_atomically_applies_and_stales_evidence(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), WorkflowEditProvider({}))
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        client.portal.call(
            app.state.services.workflow_store.mark_tested,
            application_id,
            draft["revision"],
            draft["content_hash"],
            {"passed": True, "tests": []},
        )
        tested = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert tested["evidence"]["state"] == "current"
        request = _request(
            tested,
            instruction="把选中的节点标题改为客户可读结果",
            node_ids=["answer"],
        )

        preview = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=request,
        )
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        assert preview_body["supported"] is True
        assert preview_body["applied"] is False
        assert preview_body["preview_source"] == "deterministic"
        assert preview_body["node_ids"] == ["answer"]
        assert preview_body["edge_ids"] == []
        assert preview_body["expected_revision"] == tested["revision"]
        assert preview_body["expected_content_hash"] == tested["content_hash"]
        assert preview_body["draft"]["revision"] == tested["revision"]
        assert preview_body["evidence"]["state"] == "current"
        assert preview_body["operations"] == [{
            "expected_revision": tested["revision"],
            "op": "update_node",
            "data": {
                "node_id": "answer",
                "changes": {"title": "客户可读结果"},
                "merge_config": True,
            },
        }]

        tampered_digest = (
            preview_body["preview_digest"][:-1]
            + ("0" if preview_body["preview_digest"][-1] != "0" else "1")
        )
        tampered = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **request,
                "preview_only": False,
                "preview_task_id": preview_body["task_id"],
                "expected_preview_digest": tampered_digest,
            },
        )
        assert tampered.status_code == 409, tampered.text
        unchanged = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert unchanged["revision"] == tested["revision"]
        assert unchanged["content_hash"] == tested["content_hash"]

        preview_with_apply_fields = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **request,
                "preview_task_id": preview_body["task_id"],
                "expected_preview_digest": preview_body["preview_digest"],
            },
        )
        assert preview_with_apply_fields.status_code == 422

        unreviewed_apply = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **request,
                "preview_only": False,
            },
        )
        assert unreviewed_apply.status_code == 422, unreviewed_apply.text
        unchanged = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert unchanged["revision"] == tested["revision"]
        assert unchanged["content_hash"] == tested["content_hash"]

        apply_without_digest = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **request,
                "preview_only": False,
                "preview_task_id": preview_body["task_id"],
            },
        )
        assert apply_without_digest.status_code == 422

        apply_request = {
            **request,
            "preview_only": False,
            "preview_task_id": preview_body["task_id"],
            "expected_preview_digest": preview_body["preview_digest"],
        }
        applied = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=apply_request,
        )
        assert applied.status_code == 200, applied.text
        applied_body = applied.json()
        assert applied_body["supported"] is True
        assert applied_body["applied"] is True
        assert applied_body["preview_source"] == "stored_preview"
        assert applied_body["draft"]["revision"] == tested["revision"] + 1
        assert applied_body["evidence"]["state"] == "stale"
        answer = next(
            node
            for node in applied_body["draft"]["snapshot"]["workflow"]["nodes"]
            if node["id"] == "answer"
        )
        assert answer["title"] == "客户可读结果"
        assert applied_body["draft"]["evidence"]["change_summary"][-1]["operation"] == (
            "natural_language_edit"
        )

        replay = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=apply_request,
        )
        assert replay.status_code == 200, replay.text
        assert replay.json()["draft"]["revision"] == tested["revision"] + 1

        cross_path_reuse = client.post(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
            json={
                "expected_revision": tested["revision"] + 1,
                "idempotency_key": request["idempotency_key"],
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {"title": "Must not be reported as applied"},
                    "merge_config": True,
                },
            },
        )
        assert cross_path_reuse.status_code == 409, cross_path_reuse.text
        after_cross_path_reuse = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after_cross_path_reuse["revision"] == tested["revision"] + 1
        assert next(
            node
            for node in after_cross_path_reuse["snapshot"]["workflow"]["nodes"]
            if node["id"] == "answer"
        )["title"] == "客户可读结果"

        current = replay.json()["draft"]
        second_preview_request = _request(
            current,
            instruction="把选中的节点标题改为另一个标题",
            node_ids=["answer"],
            idempotency_key=str(request["idempotency_key"]),
        )
        second_preview = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=second_preview_request,
        )
        assert second_preview.status_code == 200, second_preview.text
        second_body = second_preview.json()
        conflicting_apply = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **second_preview_request,
                "preview_only": False,
                "preview_task_id": second_body["task_id"],
                "expected_preview_digest": second_body["preview_digest"],
            },
        )
        assert conflicting_apply.status_code == 409, conflicting_apply.text
        after_conflict = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after_conflict["revision"] == tested["revision"] + 1
        answer = next(
            node
            for node in after_conflict["snapshot"]["workflow"]["nodes"]
            if node["id"] == "answer"
        )
        assert answer["title"] == "客户可读结果"


def test_selection_and_model_prompt_include_nodes_and_edges(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "update_node_description",
        "message": "Update the selected node only.",
        "operations": [{
            "op": "update_node",
            "data": {
                "node_id": "summarize",
                "changes": {"description": "先检查输入，再生成客户摘要。"},
                "merge_config": True,
            },
        }],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="让框选的这段流程先检查输入再处理，其他部分保持不变",
                node_ids=["summarize"],
                edge_ids=["start-summary"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is True
        assert body["node_ids"] == ["summarize"]
        assert body["edge_ids"] == ["start-summary"]
        assert body["operations"][0]["data"]["node_id"] == "summarize"
        assert provider.calls
        prompt = str(provider.calls[0]["message"])
        assert '"node_ids": ["summarize"]' in prompt
        assert '"edge_ids": ["start-summary"]' in prompt
        assert "primary edit target" in str(provider.calls[0]["system"])
        assert "data.changes.config" in str(provider.calls[0]["system"])

        provider.payload = {
            "supported": True,
            "intent": "update_node_description",
            "message": "A different plan that must not replace the reviewed one.",
            "operations": [{
                "op": "update_node",
                "data": {
                    "node_id": "summarize",
                    "changes": {"description": "未经用户审阅的新结果。"},
                    "merge_config": True,
                },
            }],
            "warnings": [],
        }
        applied = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **_request(
                    draft,
                    instruction="让框选的这段流程先检查输入再处理，其他部分保持不变",
                    node_ids=["summarize"],
                    edge_ids=["start-summary"],
                    preview_only=False,
                ),
                "preview_task_id": body["task_id"],
                "expected_preview_digest": body["preview_digest"],
            },
        )
        assert applied.status_code == 200, applied.text
        summarize = next(
            node
            for node in applied.json()["draft"]["snapshot"]["workflow"]["nodes"]
            if node["id"] == "summarize"
        )
        assert summarize["description"] == "先检查输入，再生成客户摘要。"
        assert len(provider.calls) == 1


def test_model_rejects_block_config_at_the_wrong_node_field_without_revision_bump(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Update the selected model system instruction.",
        "operations": [{
            "op": "update_node",
            "data": {
                "node_id": "summarize",
                "changes": {"system": "Return one concise sentence."},
                "merge_config": True,
            },
        }],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="修改框选模型的系统提示，其他内容保持不变",
                node_ids=["summarize"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["applied"] is False
        assert body["operations"] == []
        after = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after["revision"] == draft["revision"]
        assert after["content_hash"] == draft["content_hash"]


def test_model_noop_preview_is_rejected_without_revision_bump(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "update_node_description",
        "message": "Keep the existing description.",
        "operations": [{
            "op": "update_node",
            "data": {
                "node_id": "summarize",
                "changes": {"description": "Create a short summary."},
                "merge_config": True,
            },
        }],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="检查框选节点的说明并保持准确",
                node_ids=["summarize"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["applied"] is False
        assert body["operations"] == []
        assert any(
            "would not change the workflow" in warning
            for warning in body["warnings"]
        )
        after = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after["revision"] == draft["revision"]
        assert after["content_hash"] == draft["content_hash"]


def test_model_cannot_use_selection_to_rewrite_an_unrelated_node(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "rename_node",
        "message": "Rewrite an unrelated node.",
        "operations": [{
            "op": "update_node",
            "data": {
                "node_id": "answer",
                "changes": {"title": "Not allowed"},
                "merge_config": True,
            },
        }],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="让框选节点增加输入检查，并保留其他结构",
                node_ids=["start"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["applied"] is False
        assert body["operations"] == []
        assert any("unselected node" in warning for warning in body["warnings"])
        after = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after["revision"] == draft["revision"]
        assert after["content_hash"] == draft["content_hash"]


def test_selected_deletion_cannot_leave_a_dangling_workflow_reference(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Delete the selected node without repairing its consumer.",
        "operations": [{
            "op": "remove_node",
            "data": {"node_id": "summarize"},
        }],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="删除选中的节点",
                node_ids=["summarize"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["applied"] is False
        assert body["operations"] == []
        assert any(
            "dangling workflow references" in warning
            and "answer -> summarize" in warning
            for warning in body["warnings"]
        )
        after = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after["revision"] == draft["revision"]
        assert after["content_hash"] == draft["content_hash"]
        assert any(
            node["id"] == "summarize"
            for node in after["snapshot"]["workflow"]["nodes"]
        )


def test_selected_middle_node_can_be_safely_spliced_out(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Remove the selected middle node and reconnect its boundary.",
        "operations": [
            {
                "op": "remove_node",
                "data": {"node_id": "summarize"},
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start-answer",
                        "source": "start",
                        "target": "answer",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        request = _request(
            draft,
            instruction="删除选中的节点并把前后步骤重新连接",
            node_ids=["summarize"],
            idempotency_key="natural-edit-splice-0001",
        )
        preview = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=request,
        )
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["supported"] is True
        assert body["preview_source"] == "model"
        assert any(
            "splices boundary edge start-answer" in warning
            for warning in body["warnings"]
        )
        assert any(
            "updates adjacent node answer" in warning
            for warning in body["warnings"]
        )

        applied = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **request,
                "preview_only": False,
                "preview_task_id": body["task_id"],
                "expected_preview_digest": body["preview_digest"],
            },
        )
        assert applied.status_code == 200, applied.text
        applied_draft = applied.json()["draft"]
        assert applied_draft["revision"] == int(draft["revision"]) + 1
        assert {
            node["id"]
            for node in applied_draft["snapshot"]["workflow"]["nodes"]
        } == {"start", "answer"}
        assert [
            (edge["source"], edge["target"])
            for edge in applied_draft["snapshot"]["workflow"]["edges"]
        ] == [("start", "answer")]
        answer = next(
            node
            for node in applied_draft["snapshot"]["workflow"]["nodes"]
            if node["id"] == "answer"
        )
        assert answer["config"]["answer"]["$ref"]["node_id"] == "start"


def test_multi_node_selected_chain_can_be_safely_spliced_out(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Remove the selected chain and reconnect its boundary.",
        "operations": [
            {
                "op": "remove_node",
                "data": {"node_id": "summarize"},
            },
            {
                "op": "remove_node",
                "data": {"node_id": "polish"},
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start-answer",
                        "source": "start",
                        "target": "answer",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed_selected_chain(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="删除选中的节点链并把前后步骤重新连接",
                node_ids=["summarize", "polish"],
                idempotency_key="natural-edit-chain-splice-0001",
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is True
        assert any(
            "splices boundary edge start-answer" in warning
            for warning in body["warnings"]
        )
        assert any(
            "updates adjacent node answer" in warning
            for warning in body["warnings"]
        )


def test_disconnected_selected_branches_reject_cross_branch_splices(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Remove both middle nodes but cross-connect their branches.",
        "operations": [
            {
                "op": "remove_node",
                "data": {"node_id": "summarize"},
            },
            {
                "op": "remove_node",
                "data": {"node_id": "summarize2"},
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start-answer2-cross",
                        "source": "start",
                        "target": "answer2",
                    },
                },
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start2-answer-cross",
                        "source": "start2",
                        "target": "answer",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start2",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer2",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed_parallel_branches(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="删除选中的两个中间节点并重新连接各自前后步骤",
                node_ids=["summarize", "summarize2"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "connect unrelated unselected nodes" in warning
            for warning in body["warnings"]
        )
        after = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after["revision"] == draft["revision"]
        assert {
            edge["id"]
            for edge in after["snapshot"]["workflow"]["edges"]
        } >= {
            "start-summary",
            "summary-answer",
            "start2-summary2",
            "summary2-answer2",
        }


def test_disconnected_selected_branches_allow_only_their_original_splices(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Remove both middle nodes and reconnect each original branch.",
        "operations": [
            {
                "op": "remove_node",
                "data": {"node_id": "summarize"},
            },
            {
                "op": "remove_node",
                "data": {"node_id": "summarize2"},
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start-answer",
                        "source": "start",
                        "target": "answer",
                    },
                },
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start2-answer2",
                        "source": "start2",
                        "target": "answer2",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer2",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start2",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed_parallel_branches(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="删除选中的两个中间节点并重新连接各自前后步骤",
                node_ids=["summarize", "summarize2"],
                idempotency_key="natural-edit-two-splices-0001",
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is True
        assert {
            warning
            for warning in body["warnings"]
            if "splices boundary edge" in warning
        } == {
            "Selection closure splices boundary edge start-answer.",
            "Selection closure splices boundary edge start2-answer2.",
        }
        assert {
            warning
            for warning in body["warnings"]
            if "updates adjacent node" in warning
        } == {
            "Selection closure updates adjacent node answer configuration.",
            "Selection closure updates adjacent node answer2 configuration.",
        }


def test_selected_branch_rejects_gratuitous_bypass_while_path_survives(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Bypass the selected node without removing its path.",
        "operations": [
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start-answer-bypass",
                        "source": "start",
                        "target": "answer",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="保留选中节点，同时让它前后的节点直接连接",
                node_ids=["summarize"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "connect unrelated unselected nodes" in warning
            for warning in body["warnings"]
        )


def test_disconnected_selected_branches_reject_cross_branch_reference_repair(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Remove one selected middle node but use the other branch input.",
        "operations": [
            {
                "op": "remove_node",
                "data": {"node_id": "summarize2"},
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer2",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed_parallel_branches(
            client,
            include_cross_edge=True,
        )
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="删除第二个选中节点并修复它的输出引用",
                node_ids=["summarize", "summarize2"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "update unselected node: answer2" in warning
            for warning in body["warnings"]
        )


def test_edge_only_selection_cannot_pool_endpoints_across_branches(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Cross-connect endpoints from two selected edges.",
        "operations": [
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "edge-only-cross",
                        "source": "start",
                        "target": "answer2",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer2",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed_parallel_branches(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="修改选中的两条连线，但不要把两个分支串在一起",
                node_ids=[],
                edge_ids=["start-summary", "summary2-answer2"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "connect unrelated unselected nodes" in warning
            for warning in body["warnings"]
        )


def test_edge_only_splice_cannot_traverse_an_unselected_middle_edge(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Splice across two edge-only selection components.",
        "operations": [
            {"op": "remove_edge", "data": {"edge_id": "selected-a-b"}},
            {"op": "remove_edge", "data": {"edge_id": "selected-c-d"}},
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start-answer-cross-components",
                        "source": "start",
                        "target": "answer",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "start",
                                    "path": ["output"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        revision = int(draft["revision"])
        for node_id in ("b", "c", "d"):
            revision = _mutate(
                client,
                application_id,
                revision,
                "add_node",
                {
                    "node": {
                        "id": node_id,
                        "type": "llm",
                        "title": node_id.upper(),
                        "config": {"prompt": f"Run {node_id}."},
                    },
                },
            )
        revision = _mutate(
            client,
            application_id,
            revision,
            "remove_edge",
            {"edge_id": "summary-answer"},
        )
        for edge in (
            {"id": "selected-a-b", "source": "summarize", "target": "b"},
            {"id": "unselected-b-c", "source": "b", "target": "c"},
            {"id": "selected-c-d", "source": "c", "target": "d"},
            {"id": "d-answer", "source": "d", "target": "answer"},
        ):
            revision = _mutate(
                client,
                application_id,
                revision,
                "add_edge",
                {"edge": edge},
            )
        revision = _mutate(
            client,
            application_id,
            revision,
            "update_node",
            {
                "node_id": "answer",
                "changes": {
                    "config": {
                        "answer": {
                            "$ref": {
                                "node_id": "d",
                                "path": ["text"],
                            },
                        },
                    },
                },
                "merge_config": True,
            },
        )
        draft = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert draft["revision"] == revision
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction=(
                    "重构两条指定连线并予以移除，保持中间连接两侧的分支隔离"
                ),
                node_ids=[],
                edge_ids=["selected-a-b", "selected-c-d"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "connect unrelated unselected nodes" in warning
            for warning in body["warnings"]
        )


def test_added_node_cannot_bridge_disconnected_selected_components(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Use a new node to bridge two selected branches.",
        "operations": [
            {
                "op": "remove_node",
                "data": {"node_id": "summarize2"},
            },
            {
                "op": "add_node",
                "data": {
                    "node": {
                        "id": "cross_bridge",
                        "type": "llm",
                        "title": "Cross bridge",
                        "config": {"prompt": "Bridge both branches."},
                    },
                },
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "summarize-cross-bridge",
                        "source": "summarize",
                        "target": "cross_bridge",
                    },
                },
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "cross-bridge-answer2",
                        "source": "cross_bridge",
                        "target": "answer2",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer2",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "cross_bridge",
                                    "path": ["text"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed_parallel_branches(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="删除第二个选中节点并增加一个替代节点，两个分支保持独立",
                node_ids=["summarize", "summarize2"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "bridge unrelated selected components" in warning
            for warning in body["warnings"]
        )


def test_added_node_can_replace_one_selected_component_without_crossing_scope(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Replace the selected node inside its original branch.",
        "operations": [
            {
                "op": "remove_node",
                "data": {"node_id": "summarize"},
            },
            {
                "op": "add_node",
                "data": {
                    "node": {
                        "id": "classify",
                        "type": "llm",
                        "title": "Classify",
                        "config": {"prompt": "Classify the customer request."},
                    },
                },
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "start-classify",
                        "source": "start",
                        "target": "classify",
                    },
                },
            },
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "classify-answer",
                        "source": "classify",
                        "target": "answer",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "classify",
                                    "path": ["text"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="删除选中的摘要节点，用新分类节点替代，并保持原分支前后连接",
                node_ids=["summarize"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is True
        assert {
            (operation["op"], operation["data"].get("node_id"))
            for operation in body["operations"]
        } >= {
            ("remove_node", "summarize"),
            ("update_node", "answer"),
        }
        assert {
            warning
            for warning in body["warnings"]
            if "adds boundary edge" in warning
        } == {
            "Selection closure adds boundary edge start-classify.",
            "Selection closure adds boundary edge classify-answer.",
        }


def test_selected_chain_cannot_bypass_a_live_suffix_to_its_consumer(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Bypass the live selected suffix.",
        "operations": [
            {
                "op": "add_edge",
                "data": {
                    "edge": {
                        "id": "summary-answer-bypass",
                        "source": "summarize",
                        "target": "answer",
                    },
                },
            },
            {
                "op": "update_node",
                "data": {
                    "node_id": "answer",
                    "changes": {
                        "config": {
                            "answer": {
                                "$ref": {
                                    "node_id": "summarize",
                                    "path": ["text"],
                                },
                            },
                        },
                    },
                    "merge_config": True,
                },
            },
        ],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed_selected_chain(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="保留两个选中节点，只让第一个节点直接输出到答案",
                node_ids=["summarize", "polish"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "connect unrelated unselected nodes" in warning
            for warning in body["warnings"]
        )


def test_selected_edge_deletion_cannot_disconnect_a_referenced_input(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Delete the edge without repairing its target config.",
        "operations": [{
            "op": "remove_edge",
            "data": {"edge_id": "summary-answer"},
        }],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="删除选中的连线",
                node_ids=["summarize", "answer"],
                edge_ids=["summary-answer"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "disconnect referenced workflow inputs" in warning
            and "summarize -> answer" in warning
            for warning in body["warnings"]
        )
        after = client.get(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after["revision"] == draft["revision"]
        assert any(
            edge["id"] == "summary-answer"
            for edge in after["snapshot"]["workflow"]["edges"]
        )


def test_whole_workflow_edit_cannot_remove_a_referenced_node_without_repair(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "remove_disconnected_node",
        "message": "Remove the node without repairing its consumer.",
        "operations": [{
            "op": "remove_node",
            "data": {"node_id": "summarize"},
        }],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="remove node summarize",
                node_ids=[],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "dangling workflow references" in warning
            for warning in body["warnings"]
        )


def test_model_cannot_introduce_a_reference_to_an_unknown_node(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({
        "supported": True,
        "intent": "multi_operation_edit",
        "message": "Point the selected answer at a hallucinated node.",
        "operations": [{
            "op": "update_node",
            "data": {
                "node_id": "answer",
                "changes": {
                    "config": {
                        "answer": {
                            "$ref": {
                                "node_id": "hallucinated-node",
                                "path": ["text"],
                            },
                        },
                    },
                },
                "merge_config": False,
            },
        }],
        "warnings": [],
    })
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=_request(
                draft,
                instruction="让选中的输出使用新的上游结果",
                node_ids=["answer"],
            ),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is False
        assert body["operations"] == []
        assert any(
            "hallucinated-node" in warning
            and "dangling workflow references" in warning
            for warning in body["warnings"]
        )


def test_natural_language_edit_rejects_unknown_selection_stale_identity_and_extra_fields(
    tmp_path: Path,
) -> None:
    provider = WorkflowEditProvider({})
    app = create_app(_settings(tmp_path), provider)
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        unknown = _request(
            draft,
            instruction="修改选中节点",
            node_ids=["missing-node"],
        )
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=unknown,
        )
        assert response.status_code == 422, response.text
        assert "selected node not found" in response.text
        assert provider.calls == []

        stale = {
            **_request(
                draft,
                instruction="把选中的节点标题改为新标题",
                node_ids=["answer"],
            ),
            "expected_revision": int(draft["revision"]) + 1,
        }
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=stale,
        )
        assert response.status_code == 409, response.text

        extra = {
            **_request(
                draft,
                instruction="把选中的节点标题改为新标题",
                node_ids=["answer"],
            ),
            "application_id": application_id,
        }
        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=extra,
        )
        assert response.status_code == 422, response.text

        response = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            json=_request(
                draft,
                instruction="把选中的节点标题改为新标题",
                node_ids=["answer"],
            ),
        )
        assert response.status_code in {401, 403}


def test_reviewed_preview_cannot_be_applied_to_another_application(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), WorkflowEditProvider({}))
    with TestClient(app) as client:
        first_application_id, first_draft = _seed(client)
        second_application_id, second_draft = _seed(client)
        request = _request(
            first_draft,
            instruction="把选中的节点标题改为客户结果",
            node_ids=["answer"],
            idempotency_key="natural-edit-cross-app-0001",
        )
        preview = client.post(
            f"/api/v1/applications/{first_application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=request,
        )
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()

        cross_application_apply = client.post(
            f"/api/v1/applications/{second_application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **request,
                "expected_revision": second_draft["revision"],
                "expected_content_hash": second_draft["content_hash"],
                "preview_only": False,
                "preview_task_id": preview_body["task_id"],
                "expected_preview_digest": preview_body["preview_digest"],
            },
        )
        assert cross_application_apply.status_code == 422
        unchanged = client.get(
            f"/api/v1/applications/{second_application_id}/draft",
            headers=HEADERS,
        ).json()
        assert unchanged["revision"] == second_draft["revision"]
        assert unchanged["content_hash"] == second_draft["content_hash"]


def test_multi_operation_selection_edit_is_one_revision_and_stale_preview_never_partially_applies(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path), WorkflowEditProvider({}))
    with TestClient(app) as client:
        application_id, draft = _seed(client)
        request = _request(
            draft,
            instruction="把选中流程的输出改成客户摘要格式",
            node_ids=["summarize"],
            idempotency_key="natural-edit-multi-0001",
        )
        preview = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=request,
        )
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        assert preview_body["supported"] is True
        assert len(preview_body["operations"]) >= 4
        assert any(
            "Selection closure" in warning
            for warning in preview_body["warnings"]
        )

        applied = client.post(
            f"/api/v1/applications/{application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **request,
                "preview_only": False,
                "preview_task_id": preview_body["task_id"],
                "expected_preview_digest": preview_body["preview_digest"],
            },
        )
        assert applied.status_code == 200, applied.text
        applied_body = applied.json()
        assert applied_body["draft"]["revision"] == int(draft["revision"]) + 1
        assert any(
            node["id"] == "workflow_edit_transform"
            for node in applied_body["draft"]["snapshot"]["workflow"]["nodes"]
        )

        second_application_id, second_draft = _seed(client)
        second_request = _request(
            second_draft,
            instruction="把选中流程的输出改成客户摘要格式",
            node_ids=["summarize"],
            idempotency_key="natural-edit-multi-0002",
        )
        second_preview = client.post(
            f"/api/v1/applications/{second_application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json=second_request,
        )
        assert second_preview.status_code == 200, second_preview.text
        second_preview_body = second_preview.json()

        changed_revision = _mutate(
            client,
            second_application_id,
            int(second_draft["revision"]),
            "update_node",
            {
                "node_id": "start",
                "changes": {"title": "Changed after preview"},
                "merge_config": True,
            },
        )
        stale_apply = client.post(
            f"/api/v1/applications/{second_application_id}/draft/natural-language-edit",
            headers=HEADERS,
            json={
                **second_request,
                "preview_only": False,
                "preview_task_id": second_preview_body["task_id"],
                "expected_preview_digest": second_preview_body["preview_digest"],
            },
        )
        assert stale_apply.status_code == 409, stale_apply.text
        after = client.get(
            f"/api/v1/applications/{second_application_id}/draft",
            headers=HEADERS,
        ).json()
        assert after["revision"] == changed_revision
        assert not any(
            node["id"].startswith("workflow_edit_transform")
            for node in after["snapshot"]["workflow"]["nodes"]
        )
        assert any(
            edge["id"] == "summary-answer"
            for edge in after["snapshot"]["workflow"]["edges"]
        )
