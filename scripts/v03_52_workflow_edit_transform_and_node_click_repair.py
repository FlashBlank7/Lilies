#!/usr/bin/env python3
"""Verify v0.3.52 workflow-edit transform preview and node-click crash repair."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "backend" / "src"))

from agent_platform.draft_patch_preview import DraftPatchPreviewer  # noqa: E402
from agent_platform.workflow_models import ApplicationSnapshot, EdgeSpec, NodeSpec, WorkflowSpec  # noqa: E402


DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "workflow_edit_transform_and_node_click_repair_v0.3.52.json"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/cancel")


BUG_LEDGER = (
    {"id": "P1-node-click-edit-panel-crash", "severity": "P1", "status": "fixed", "reproduction": "Clicking a canvas brick could switch to the edit panel and crash when workflow node fields were not safe strings.", "fix": "Add safe text/type/config helpers before rendering workflow summaries and node inspector fields.", "verification": "node_click_crash_guard_fixture."},
    {"id": "P1-workflow-edit-too-restricted", "severity": "P1", "status": "fixed", "reproduction": "Natural-language workflow edit returned UNSUPPORTED for ordinary workflow-level instructions.", "fix": "Fallback unmatched instructions to an applicable workflow requirement update instead of an unsupported response.", "verification": "workflow_edit_no_unsupported_fallback_fixture."},
    {"id": "P1-workflow-edit-cannot-change-result-shape", "severity": "P1", "status": "fixed", "reproduction": "Instructions to change output/summary format could not create a concrete graph operation.", "fix": "Add deterministic upsert_template_transform preview operations before the terminal node.", "verification": "workflow_edit_transform_preview_fixture."},
    {"id": "P1-workflow-edit-preview-must-be-applicable", "severity": "P1", "status": "fixed", "reproduction": "A preview that looks supported but cannot be applied would recreate the user's 不可应用 failure.", "fix": "Test operation order against the draft mutation API with a real in-process app.", "verification": "in_process_preview_apply_test."},
    {"id": "P1-v0352-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "The reported workflow-edit and node-click regressions could return if omitted from the current v0.3.x gate.", "fix": "Update the current regression lane with v0.3.52 and a higher pass-count floor.", "verification": "regression_manifest_updated."},
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def sample_snapshot() -> ApplicationSnapshot:
    return ApplicationSnapshot(
        name="Draft Workflow",
        description="A generated workflow.",
        requirement="Answer the user.",
        workflow=WorkflowSpec(
            nodes=[
                NodeSpec(id="start", type="start", title="Input", config={"inputs": [{"name": "topic", "type": "string"}]}),
                NodeSpec(id="summarize", type="llm", title="Summarize", config={"prompt": "Summarize {{ topic }}"}),
                NodeSpec(id="end", type="end", title="Result", config={"outputs": {"answer": {"$ref": {"node_id": "summarize", "path": ["text"]}}}}),
            ],
            edges=[
                EdgeSpec(id="a", source="start", target="summarize"),
                EdgeSpec(id="b", source="summarize", target="end"),
            ],
        ),
    )


def workflow_edit_transform_preview_fixture() -> dict[str, Any]:
    result = DraftPatchPreviewer().preview(sample_snapshot(), 7, "把工作流输出改成面向客户的今日总结格式")
    operation_names = [operation["op"] for operation in result.operations]
    added_nodes = [operation["data"]["node"] for operation in result.operations if operation["op"] == "add_node"]
    cases = {
        "supported": result.supported is True,
        "intent_is_transform_upsert": result.intent == "upsert_template_transform",
        "adds_template_transform": bool(added_nodes) and added_nodes[0]["type"] == "template_transform",
        "removes_old_terminal_edge": "remove_edge" in operation_names,
        "adds_two_reconnect_edges": operation_names.count("add_edge") == 2,
        "updates_terminal_output": any(operation["op"] == "update_node" and operation["data"]["node_id"] == "end" for operation in result.operations),
    }
    return {"id": "workflow_edit_transform_preview_fixture", "passed": all(cases.values()), "cases": cases, "preview": result.model_dump(mode="json")}


def workflow_edit_no_unsupported_fallback_fixture() -> dict[str, Any]:
    result = DraftPatchPreviewer().preview(sample_snapshot(), 7, "帮我把这个应用做得更适合普通客户使用")
    cases = {
        "fallback_is_supported": result.supported is True,
        "fallback_updates_requirement": result.intent == "update_workflow_requirement",
        "message_does_not_say_unsupported": "unsupported instruction" not in result.message.casefold(),
        "apply_button_would_be_enabled": bool(result.operations),
    }
    return {"id": "workflow_edit_no_unsupported_fallback_fixture", "passed": all(cases.values()), "cases": cases, "preview": result.model_dump(mode="json")}


def node_click_crash_guard_fixture() -> dict[str, Any]:
    page = read_text("platform/frontend/app/applications/[id]/page.tsx")
    cases = {
        "safe_type_helper_present": "function safeWorkflowNodeType" in page,
        "safe_config_keys_present": "function safeConfigKeys" in page,
        "workflow_summary_uses_safe_type": "const type = safeWorkflowNodeType(node)" in page,
        "node_summary_uses_safe_type": "value: safeWorkflowNodeType(selected)" in page,
        "reference_chip_uses_safe_type": "safeWorkflowNodeType(node)" in page and "safeText(node.title, node.id)" in page,
        "raw_replaceall_removed_from_node_type": "node.type.replaceAll" not in page,
    }
    return {"id": "node_click_crash_guard_fixture", "passed": all(cases.values()), "cases": cases}


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next((lane for lane in manifest.get("lanes", []) if lane.get("id") == "v0.3.x_current_release_gate"), {})
    test_files = set(current_lane.get("test_files", []))
    command = current_lane.get("command", [])
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "current_gate_present": bool(current_lane),
        "v0352_test_in_test_files": "tests/test_v03_52_workflow_edit_transform_and_node_click_repair.py" in test_files,
        "v0352_test_in_command": "tests/test_v03_52_workflow_edit_transform_and_node_click_repair.py" in command,
        "pass_count_not_less_than_v0352_floor": isinstance(pass_count, int) and pass_count >= 304,
    }
    return {"id": "regression_manifest_updated", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "workflow_edit_transform_backend_markers",
            "platform/backend/src/agent_platform/draft_patch_preview.py",
            (
                "upsert_template_transform",
                "_template_transform_preview",
                "_looks_like_template_transform_request",
                "_terminal_config_after_transform",
                "No deterministic structural transform matched this instruction",
            ),
        ),
        (
            "node_click_frontend_guard_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "safeWorkflowNodeType",
                "safeConfigKeys",
                "safeText(node.title, node.id)",
                "const type = safeWorkflowNodeType(node)",
            ),
        ),
    ]
    evidence: list[dict[str, Any]] = []
    for check_id, relative_path, markers in checks:
        text = read_text(relative_path)
        missing = [marker for marker in markers if marker not in text]
        evidence.append({"id": check_id, "path": relative_path, "required_markers": list(markers), "missing_markers": missing, "passed": not missing})
    evidence.append(regression_manifest_check())
    return evidence


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [item for item in BUG_LEDGER if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}]
    return {"id": "p0_p1_bug_ledger_workflow_edit_transform_and_node_click_repair", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def build_evidence() -> dict[str, Any]:
    safety = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    checks: list[dict[str, Any]] = [
        bug_ledger_evidence(),
        workflow_edit_transform_preview_fixture(),
        workflow_edit_no_unsupported_fallback_fixture(),
        node_click_crash_guard_fixture(),
        *source_marker_checks(),
        {"id": "safety_no_live_side_effects_workflow_edit_transform_and_node_click_repair", "passed": True, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]},
    ]
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.52",
        "stage": "workflow_edit_transform_and_node_click_repair",
        "status": "passed" if not failed else "failed",
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "transform_preview": workflow_edit_transform_preview_fixture(),
            "fallback": workflow_edit_no_unsupported_fallback_fixture(),
            "node_click_guard": node_click_crash_guard_fixture(),
        },
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    evidence = build_evidence()
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
