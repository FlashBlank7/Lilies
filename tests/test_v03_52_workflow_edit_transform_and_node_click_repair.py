from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_52_workflow_edit_transform_and_node_click_repair.py"
    spec = importlib.util.spec_from_file_location("v03_52_workflow_edit_transform_and_node_click_repair_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def mutate(client: TestClient, app_id: str, revision: int, op: str, data: dict) -> int:
    response = client.post(
        f"/api/v1/applications/{app_id}/draft",
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


def seed_workflow(client: TestClient, app_id: str) -> int:
    revision = 0
    for node in [
        {"id": "start", "type": "start", "title": "Input", "config": {"inputs": [{"name": "topic", "type": "string"}]}},
        {"id": "summarize", "type": "llm", "title": "Summarize", "config": {"prompt": "Summarize {{ topic }}"}},
        {"id": "end", "type": "end", "title": "Result", "config": {"outputs": {"answer": {"$ref": {"node_id": "summarize", "path": ["text"]}}}}},
    ]:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    revision = mutate(client, app_id, revision, "add_edge", {"edge": {"id": "a", "source": "start", "target": "summarize"}})
    revision = mutate(client, app_id, revision, "add_edge", {"edge": {"id": "b", "source": "summarize", "target": "end"}})
    return revision


def test_v03_52_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_52_transform_preview_generates_graph_operations() -> None:
    module = load_audit_module()
    fixture = module.workflow_edit_transform_preview_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["adds_template_transform"] is True
    assert fixture["cases"]["updates_terminal_output"] is True


def test_v03_52_unmatched_instruction_no_longer_returns_unsupported() -> None:
    module = load_audit_module()
    fixture = module.workflow_edit_no_unsupported_fallback_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["fallback_is_supported"] is True
    assert fixture["cases"]["message_does_not_say_unsupported"] is True


def test_v03_52_node_click_edit_panel_uses_safe_rendering_guards() -> None:
    module = load_audit_module()
    fixture = module.node_click_crash_guard_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["raw_replaceall_removed_from_node_type"] is True


def test_v03_52_preview_operations_apply_to_real_draft(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Workflow edit apply", "requirement": "Summarize a topic."},
        )
        assert response.status_code == 201, response.text
        app_id = response.json()["id"]
        revision = seed_workflow(client, app_id)

        preview_response = client.post(
            f"/api/v1/applications/{app_id}/draft/preview-patch",
            headers=HEADERS,
            json={"instruction": "把工作流输出改成面向客户的今日总结格式", "reference_node_ids": ["summarize"]},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["supported"] is True
        assert preview["intent"] == "upsert_template_transform"

        current_revision = revision
        for operation in preview["operations"]:
            current_revision = mutate(client, app_id, current_revision, operation["op"], operation["data"])

        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=HEADERS).json()
        node_types = {node["id"]: node["type"] for node in draft["snapshot"]["workflow"]["nodes"]}
        assert node_types["workflow_edit_transform"] == "template_transform"
        assert any(edge["source"] == "summarize" and edge["target"] == "workflow_edit_transform" for edge in draft["snapshot"]["workflow"]["edges"])
        assert any(edge["source"] == "workflow_edit_transform" and edge["target"] == "end" for edge in draft["snapshot"]["workflow"]["edges"])
        end = next(node for node in draft["snapshot"]["workflow"]["nodes"] if node["id"] == "end")
        assert end["config"]["outputs"]["result"]["$ref"]["node_id"] == "workflow_edit_transform"


def test_v03_52_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence()
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_52_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_52_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence())
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.52"
    assert loaded["status"] == "passed"
