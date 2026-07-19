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
    module_path = ROOT / "scripts" / "v03_54_acceptance_auto_repair.py"
    spec = importlib.util.spec_from_file_location("v03_54_acceptance_auto_repair_under_test", module_path)
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


def seed_minimal_agent_acceptance_fixture(client: TestClient, app_id: str) -> int:
    revision = 0
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "Prompt",
            "config": {"inputs": [{"name": "prompt", "type": "string", "required": False}]},
            "position": {"x": 0, "y": 0},
        },
        {
            "id": "end",
            "type": "end",
            "title": "Raw Result",
            "config": {"outputs": {"output": {"$ref": {"node_id": "start", "path": ["prompt"]}}}},
            "position": {"x": 220, "y": 0},
        },
    ]
    for node in nodes:
        revision = mutate(client, app_id, revision, "add_node", {"node": node})
    revision = mutate(client, app_id, revision, "add_edge", {"edge": {"id": "start_end", "source": "start", "target": "end"}})
    tests = [
        {
            "id": "safety_guardrails",
            "name": "Safety Guardrails",
            "requirement": "Agent must have permission gate and sandbox boundary with network access enabled for general-purpose use.",
            "required_node_types": ["permission_gate", "sandbox_boundary"],
            "assertions": [],
            "mandatory": True,
        },
        {
            "id": "architecture_compliance",
            "name": "Architecture Compliance",
            "requirement": "Workflow must contain all essential agent architecture blocks.",
            "required_node_types": [
                "start",
                "context_assembler",
                "workspace_context_injector",
                "skill_loader",
                "mcp_gateway",
                "capability_registry",
                "conversation_memory",
                "context_compactor",
                "budget_gate",
                "round_limit",
                "permission_gate",
                "sandbox_boundary",
                "loop",
                "template_transform",
                "answer",
            ],
            "assertions": [],
            "mandatory": True,
        },
        {
            "id": "general_knowledge_question",
            "name": "General Knowledge Question",
            "requirement": "Agent must be able to answer a simple knowledge question without requiring external tools.",
            "inputs": {"prompt": "What is the capital of France?"},
            "required_node_types": ["start", "context_assembler", "loop", "answer"],
            "assertions": [
                {"path": ["answer"], "operator": "exists"},
                {"path": ["answer"], "operator": "min_length", "expected": 5},
            ],
            "mandatory": True,
        },
    ]
    for test in tests:
        revision = mutate(client, app_id, revision, "add_test", {"test": test})
    return revision


def test_v03_54_source_markers_pass() -> None:
    module = load_audit_module()
    assert module.backend_repair_preview_markers()["passed"] is True
    assert module.frontend_acceptance_repair_markers()["passed"] is True


def test_v03_54_acceptance_failure_preview_apply_and_rerun_passes(tmp_path: Path) -> None:
    settings = Settings(api_token="workflow-test", data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Acceptance auto repair", "requirement": "Build a visible agent workflow."},
        )
        assert response.status_code == 201, response.text
        app_id = response.json()["id"]
        revision = seed_minimal_agent_acceptance_fixture(client, app_id)

        failed = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=HEADERS)
        assert failed.status_code == 200, failed.text
        failed_report = failed.json()
        assert failed_report["passed"] is False
        assert failed_report["validation"]["valid"] is False
        assert len(failed_report["tests"]) == 3
        assert all(item["passed"] is False for item in failed_report["tests"])
        assert all(item["run_id"] == "" for item in failed_report["tests"])
        assert all(
            "test was not run because draft validation failed"
            in item["readable_report"]["failed_checks"]
            for item in failed_report["tests"]
        )
        assert len(failed_report["validation"]["errors"]) == 3

        preview_response = client.post(
            f"/api/v1/applications/{app_id}/tests/repair-preview",
            headers=HEADERS,
            json={"report": failed_report},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["supported"] is True
        assert preview["operations"]
        assert not preview["unsupported_node_types"]
        assert {"permission_gate", "sandbox_boundary", "context_assembler", "loop", "template_transform"}.issubset(set(preview["missing_node_types"]))

        added_node_types = {
            operation["data"]["node"]["type"]
            for operation in preview["operations"]
            if operation["op"] == "add_node"
        }
        assert {"permission_gate", "sandbox_boundary", "context_assembler", "loop", "template_transform"}.issubset(added_node_types)
        assert any(
            operation["op"] == "update_node"
            and operation["data"]["node_id"] == "end"
            and operation["data"]["changes"]["type"] == "answer"
            for operation in preview["operations"]
        )

        current_revision = revision
        for operation in preview["operations"]:
            current_revision = mutate(client, app_id, current_revision, operation["op"], operation["data"])

        validation = client.post(f"/api/v1/applications/{app_id}/draft/validate", headers=HEADERS)
        assert validation.status_code == 200, validation.text
        assert validation.json()["valid"] is True

        draft = client.get(f"/api/v1/applications/{app_id}/draft", headers=HEADERS).json()
        node_types = [node["type"] for node in draft["snapshot"]["workflow"]["nodes"]]
        for block_type in [
            "context_assembler",
            "workspace_context_injector",
            "skill_loader",
            "mcp_gateway",
            "capability_registry",
            "conversation_memory",
            "context_compactor",
            "budget_gate",
            "round_limit",
            "permission_gate",
            "sandbox_boundary",
            "loop",
            "template_transform",
            "answer",
        ]:
            assert block_type in node_types
        assert "end" not in node_types

        repaired = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=HEADERS)
        assert repaired.status_code == 200, repaired.text
        repaired_report = repaired.json()
        assert repaired_report["passed"] is True
        assert all(item["passed"] for item in repaired_report["tests"])
        general = next(item for item in repaired_report["tests"] if item["test_id"] == "general_knowledge_question")
        answer_assertions = [item for item in general["assertions"] if item.get("path") == ["answer"]]
        assert answer_assertions
        assert all(item["passed"] for item in answer_assertions)
        assert any(len(str(item.get("actual", ""))) >= 5 for item in answer_assertions)


def test_v03_54_backend_defers_unsafe_required_node_types() -> None:
    from agent_platform.acceptance_repair import AcceptanceRepairPreviewer
    from agent_platform.blocks import build_block_registry
    from agent_platform.workflow_models import ApplicationSnapshot

    snapshot = ApplicationSnapshot.model_validate({
        "name": "Unsafe repair",
        "description": "",
        "requirement": "Needs real tools",
        "workflow": {
            "nodes": [
                {"id": "start", "type": "start", "title": "Start", "config": {"inputs": []}},
                {"id": "end", "type": "end", "title": "End", "config": {"outputs": {"output": "ok"}}},
            ],
            "edges": [{"id": "start_end", "source": "start", "target": "end"}],
        },
        "agents": {},
        "tests": [
            {
                "id": "tool_gate",
                "name": "Tool gate",
                "requirement": "Must call real tools.",
                "required_node_types": ["tool_executor", "permission_gate"],
                "mandatory": True,
            }
        ],
    })
    preview = AcceptanceRepairPreviewer(build_block_registry()).preview(snapshot, 0)
    assert preview.supported is True
    assert "tool_executor" in preview.unsupported_node_types
    assert any("tool_executor" in warning for warning in preview.warnings)
    assert any(operation.get("data", {}).get("node", {}).get("type") == "permission_gate" for operation in preview.operations)
    assert not any(operation.get("data", {}).get("node", {}).get("type") == "tool_executor" for operation in preview.operations)


def test_v03_54_repair_uses_existing_business_output_instead_of_start_input() -> None:
    from agent_platform.acceptance_repair import AcceptanceRepairPreviewer
    from agent_platform.blocks import build_block_registry
    from agent_platform.workflow_models import ApplicationSnapshot

    snapshot = ApplicationSnapshot.model_validate({
        "name": "Preserve business output",
        "description": "",
        "requirement": "Return the formatted workflow result.",
        "workflow": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "Start",
                    "config": {"inputs": [{"name": "request"}]},
                },
                {
                    "id": "format",
                    "type": "template_transform",
                    "title": "Format",
                    "config": {
                        "template": "Result: {{ request }}",
                        "variables": {
                            "request": {
                                "$ref": {"node_id": "start", "path": ["request"]}
                            }
                        },
                    },
                },
                {
                    "id": "answer",
                    "type": "answer",
                    "title": "Answer",
                    "config": {
                        "answer": {
                            "$ref": {"node_id": "start", "path": ["output"]}
                        }
                    },
                },
            ],
            "edges": [
                {"id": "start_format", "source": "start", "target": "format"},
                {"id": "format_answer", "source": "format", "target": "answer"},
                {"id": "start_answer", "source": "start", "target": "answer"},
            ],
        },
        "tests": [{
            "id": "formatted_answer",
            "name": "Formatted answer",
            "requirement": "The customer sees the formatted business result.",
            "inputs": {"request": "hello"},
            "assertions": [
                {"path": ["answer"], "operator": "contains", "expected": "Result:"}
            ],
            "mandatory": True,
        }],
    })

    preview = AcceptanceRepairPreviewer(build_block_registry()).preview(
        snapshot,
        3,
        {
            "passed": False,
            "tests": [{
                "test_id": "formatted_answer",
                "passed": False,
                "assertions": [{
                    "path": ["answer"],
                    "operator": "contains",
                    "expected": "Result:",
                    "actual": {"request": "hello"},
                    "passed": False,
                }],
            }],
        },
    )

    answer_update = next(
        operation
        for operation in preview.operations
        if operation["op"] == "update_node"
        and operation["data"]["node_id"] == "answer"
    )
    assert answer_update["data"]["changes"]["config"]["answer"] == {
        "$ref": {"node_id": "format", "path": ["text"]}
    }
    assert any(
        operation["op"] == "remove_edge"
        and operation["data"]["edge_id"] == "start_answer"
        for operation in preview.operations
    )
    assert not any(
        operation["op"] == "add_edge"
        and operation["data"]["edge"]["source"] == "start"
        and operation["data"]["edge"]["target"] == "answer"
        for operation in preview.operations
    )


def test_v03_54_static_evidence_passes_and_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    evidence = module.build_evidence()
    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    output = tmp_path / "evidence.json"
    module.write_evidence(output, evidence)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.54"
    assert loaded["status"] == "passed"


def test_v03_54_regression_manifest_contains_current_test() -> None:
    module = load_audit_module()
    check = module.regression_manifest_check()
    assert check["passed"] is True
    assert check["cases"]["v0354_test_in_command"] is True
    assert check["cases"]["pass_count_not_less_than_v0354_floor"] is True
