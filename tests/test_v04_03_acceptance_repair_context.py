from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def _mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    operation: str,
    data: dict,
) -> int:
    response = client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": operation,
            "data": data,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def _seed_repairable_case(
    client: TestClient,
    *,
    required_node_type: str = "permission_gate",
    include_assertion: bool = True,
) -> tuple[str, int]:
    created = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={
            "name": "Repair context",
            "requirement": "Repair a failed acceptance case without hiding the edit.",
        },
    )
    assert created.status_code == 201, created.text
    application_id = created.json()["id"]
    revision = 0
    revision = _mutate(client, application_id, revision, "add_node", {"node": {
        "id": "start",
        "type": "start",
        "title": "Input",
        "config": {"inputs": [{"name": "prompt", "type": "string"}]},
    }})
    revision = _mutate(client, application_id, revision, "add_node", {"node": {
        "id": "end",
        "type": "end",
        "title": "Output",
        "config": {"outputs": {"answer": "not repaired"}},
    }})
    revision = _mutate(client, application_id, revision, "add_edge", {"edge": {
        "id": "start-end",
        "source": "start",
        "target": "end",
    }})
    revision = _mutate(client, application_id, revision, "add_test", {"test": {
        "id": "safety-case",
        "name": "Safety case",
        "requirement": "The workflow must expose a permission decision before output.",
        "required_node_types": [required_node_type],
        "required_tools": ["permission.audit"],
        "assertions": ([{"path": ["answer"], "operator": "exists"}] if include_assertion else []),
        "mandatory": True,
    }})
    return application_id, revision


def _failed_report() -> dict:
    return {
        "passed": False,
        "tests": [{
            "test_id": "safety-case",
            "name": "Safety case",
            "passed": False,
            "run_id": "failed-run-1",
            "assertions": [{
                "path": ["answer"],
                "operator": "exists",
                "passed": False,
                "error": "answer missing",
            }],
            "readable_report": {
                "failed_checks": ["missing required node types: ['permission_gate']"],
                "failure_target": "start",
            },
        }],
    }


def test_failed_case_preview_carries_context_and_applies_atomically(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        application_id, revision = _seed_repairable_case(client)
        original = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        asyncio.run(app.state.services.workflow_store.mark_tested(
            application_id,
            revision,
            original["content_hash"],
            {"passed": True, "summary": {"total": 1, "passed": 1}},
        ))
        asyncio.run(app.state.services.storage.append_event(
            "failed-run-1",
            "workflow.node.failed",
            {"node_id": "start", "status": "failed", "error": "permission gate absent"},
        ))

        preview_response = client.post(
            f"/api/v1/applications/{application_id}/tests/repair-preview",
            headers=HEADERS,
            json={
                "report": _failed_report(),
                "test_id": "safety-case",
                "reference_node_ids": ["start"],
            },
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        context = preview["repair_context"]
        assert preview["supported"] is True
        assert preview["expected_revision"] == revision
        assert preview["expected_content_hash"] == original["content_hash"]
        assert context["test_id"] == "safety-case"
        assert context["requirement"].startswith("The workflow must expose")
        assert context["failed_assertions"][0]["error"] == "answer missing"
        assert context["required_node_types"] == ["permission_gate"]
        assert context["required_tools"] == ["permission.audit"]
        assert context["run_id"] == "failed-run-1"
        assert any("workflow.node.failed" in item for item in context["trace_excerpts"])
        assert "start" in context["relevant_node_ids"]
        assert "whole workflow" in preview["instruction"]
        assert preview["rationale_markdown"].startswith("## Failed acceptance")
        assert preview["workflow_edit_preview"]["reference_node_ids"] == ["start"]

        applied = client.post(
            f"/api/v1/applications/{application_id}/tests/repair-apply",
            headers=HEADERS,
            json={
                "expected_revision": preview["expected_revision"],
                "expected_content_hash": preview["expected_content_hash"],
                "operations": preview["operations"],
                "idempotency_key": str(uuid4()),
            },
        )
        assert applied.status_code == 200, applied.text
        result = applied.json()
        assert result["revision"] == revision + 1
        assert result["operations_applied"] == len(preview["operations"])
        assert result["content_hash"] != original["content_hash"]
        assert result["evidence_state"] == "stale"

        repaired = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        assert repaired["revision"] == revision + 1
        assert "permission_gate" in {
            node["type"] for node in repaired["snapshot"]["workflow"]["nodes"]
        }
        assert repaired["evidence"]["change_summary"][-1]["operation"] == "acceptance_repair"


def test_stale_preview_is_rejected_without_additional_mutation(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        application_id, revision = _seed_repairable_case(client)
        preview = client.post(
            f"/api/v1/applications/{application_id}/tests/repair-preview",
            headers=HEADERS,
            json={"report": _failed_report(), "test_id": "safety-case"},
        ).json()
        _mutate(
            client,
            application_id,
            revision,
            "set_metadata",
            {"description": "A concurrent user edit."},
        )
        before = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        rejected = client.post(
            f"/api/v1/applications/{application_id}/tests/repair-apply",
            headers=HEADERS,
            json={
                "expected_revision": preview["expected_revision"],
                "expected_content_hash": preview["expected_content_hash"],
                "operations": preview["operations"],
                "idempotency_key": str(uuid4()),
            },
        )
        assert rejected.status_code == 409
        after = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        assert after["revision"] == before["revision"]
        assert after["content_hash"] == before["content_hash"]


def test_unsupported_preview_and_invalid_atomic_batch_leave_draft_unchanged(
    tmp_path: Path,
) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        unsupported_id, _ = _seed_repairable_case(
            client,
            required_node_type="tool_executor",
            include_assertion=False,
        )
        before_unsupported = client.get(
            f"/api/v1/applications/{unsupported_id}/draft", headers=HEADERS
        ).json()
        unsupported = client.post(
            f"/api/v1/applications/{unsupported_id}/tests/repair-preview",
            headers=HEADERS,
            json={"report": _failed_report(), "test_id": "safety-case"},
        )
        assert unsupported.status_code == 200, unsupported.text
        unsupported_body = unsupported.json()
        assert unsupported_body["supported"] is False
        assert unsupported_body["operations"] == []
        assert unsupported_body["unsupported_node_types"] == ["tool_executor"]
        after_unsupported = client.get(
            f"/api/v1/applications/{unsupported_id}/draft", headers=HEADERS
        ).json()
        assert after_unsupported["content_hash"] == before_unsupported["content_hash"]

        application_id, revision = _seed_repairable_case(client)
        before_invalid = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        invalid = client.post(
            f"/api/v1/applications/{application_id}/tests/repair-apply",
            headers=HEADERS,
            json={
                "expected_revision": revision,
                "expected_content_hash": before_invalid["content_hash"],
                "idempotency_key": str(uuid4()),
                "operations": [
                    {
                        "expected_revision": revision,
                        "op": "add_node",
                        "data": {"node": {
                            "id": "would-be-partial",
                            "type": "permission_gate",
                            "title": "Would be partial",
                            "config": {"input": {}, "settings": {}},
                        }},
                    },
                    {
                        "expected_revision": revision,
                        "op": "add_edge",
                        "data": {"edge": {
                            "id": "invalid-edge",
                            "source": "would-be-partial",
                            "target": "missing-target",
                        }},
                    },
                ],
            },
        )
        assert invalid.status_code in {404, 422}
        after_invalid = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        assert after_invalid["revision"] == before_invalid["revision"]
        assert after_invalid["content_hash"] == before_invalid["content_hash"]
        assert "would-be-partial" not in {
            node["id"] for node in after_invalid["snapshot"]["workflow"]["nodes"]
        }


def test_frontend_uses_contextual_preview_and_atomic_apply_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    page = (root / "platform/frontend/app/applications/[id]/page.tsx").read_text()
    types = (root / "platform/frontend/lib/platform.ts").read_text()

    assert "/tests/repair-preview" in page
    assert "/tests/repair-apply" in page
    assert "expected_content_hash: acceptanceRepairPreview.expected_content_hash" in page
    assert "reference_node_ids: workflowEditReferenceIds" in page
    assert 'dataSurface="acceptance-repair-rationale"' in page
    assert "acceptanceRepairThisCase" in page
    assert "repair_context:" in types
    assert "workflow_edit_preview?: DraftPatchPreview" in types


def test_whole_workflow_preview_failure_is_explicit_and_non_mutating(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        application_id, _ = _seed_repairable_case(client)
        before = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        original_preview = app.state.services.draft_patcher.preview

        def fail_preview(*_args, **_kwargs):
            raise RuntimeError("preview provider unavailable")

        app.state.services.draft_patcher.preview = fail_preview
        try:
            response = client.post(
                f"/api/v1/applications/{application_id}/tests/repair-preview",
                headers=HEADERS,
                json={"report": _failed_report(), "test_id": "safety-case"},
            )
        finally:
            app.state.services.draft_patcher.preview = original_preview

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["supported"] is True
        assert body["workflow_edit_preview"]["supported"] is False
        assert "preview provider unavailable" in body["workflow_edit_preview"]["message"]
        assert any("preview provider unavailable" in item for item in body["warnings"])
        after = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=HEADERS
        ).json()
        assert after["content_hash"] == before["content_hash"]
