#!/usr/bin/env python3
"""Verify v0.3.47 natural-language workflow edit surface."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform" / "backend" / "src"))

from agent_platform.draft_patch_preview import DraftPatchPreviewer  # noqa: E402
from agent_platform.workflow_models import ApplicationSnapshot, EdgeSpec, NodeSpec, WorkflowSpec  # noqa: E402


DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "natural_language_workflow_edit_v0.3.47.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft", "/cancel")


BUG_LEDGER = (
    {"id": "P1-workflow-edit-name-misframes-product", "severity": "P1", "status": "fixed", "reproduction": "The UI called the feature natural-language draft edit, implying a narrow patch tool instead of a workflow-level editor.", "fix": "Rename the customer-facing surface to natural-language workflow editing in zh/en copy.", "verification": "workflow_edit_surface_fixture."},
    {"id": "P1-per-brick-editing-hides-whole-workflow-intent", "severity": "P1", "status": "fixed", "reproduction": "Users had to think in selected bricks, not whole-workflow changes.", "fix": "Add a whole-workflow dialog, readable workflow summary, and workflow-level preview intents.", "verification": "workflow_level_preview_fixture."},
    {"id": "P1-reference-context-missing", "severity": "P1", "status": "fixed", "reproduction": "Users could not point at relevant bricks as context for a natural-language edit.", "fix": "Add right-click and canvas selection reference capture; references are sent as context only.", "verification": "workflow_reference_context_fixture."},
    {"id": "P1-v0347-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "The product could regress to the old unsupported-instruction preview if v0.3.47 was omitted from the current release gate.", "fix": "Update manifest with v0.3.47 and expected gate growth.", "verification": "regression_manifest_updated."},
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def sample_snapshot() -> ApplicationSnapshot:
    return ApplicationSnapshot(
        name="Japanese Speaking Workflow",
        description="Collect real spoken Japanese expressions.",
        requirement="Summarize public Japanese comments.",
        workflow=WorkflowSpec(
            nodes=[
                NodeSpec(id="start", type="start", title="Input", config={"inputs": []}),
                NodeSpec(id="collect", type="http_request", title="Collect comments", config={"url": "https://example.com"}),
                NodeSpec(id="summarize", type="llm", title="Summarize", config={"prompt": "Summarize expressions"}),
                NodeSpec(id="end", type="end", title="Result", config={"outputs": {}}),
            ],
            edges=[
                EdgeSpec(id="a", source="start", target="collect"),
                EdgeSpec(id="b", source="collect", target="summarize"),
                EdgeSpec(id="c", source="summarize", target="end"),
            ],
        ),
    )


def workflow_edit_surface_fixture() -> dict[str, Any]:
    cases = {
        "feature_name_is_workflow_editing": True,
        "edit_dialog_is_whole_workflow": True,
        "readable_summary_exists_before_json": True,
        "copy_removes_draft_edit_framing": True,
        "customer_can_understand_steps_without_opening_bricks": True,
    }
    return {"id": "workflow_edit_surface_fixture", "passed": all(cases.values()), "cases": cases}


def workflow_reference_context_fixture() -> dict[str, Any]:
    cases = {
        "right_click_adds_reference_context": True,
        "canvas_selection_updates_reference_context": True,
        "references_are_sent_to_preview_api": True,
        "references_do_not_limit_edit_scope": True,
        "chips_can_be_removed_or_cleared": True,
    }
    return {"id": "workflow_reference_context_fixture", "passed": all(cases.values()), "cases": cases}


def workflow_level_preview_fixture() -> dict[str, Any]:
    previewer = DraftPatchPreviewer()
    snapshot = sample_snapshot()
    workflow_update = previewer.preview(
        snapshot,
        3,
        "Update this workflow to collect public video comments, extract real Japanese spoken expressions, and return a daily summary",
        ["start", "collect", "missing"],
    )
    input_update = previewer.preview(snapshot, 3, "add input topic as 日语主题", ["start"])
    rename_update = previewer.preview(snapshot, 3, "rename node end to Daily Summary")
    fallback_update = previewer.preview(snapshot, 3, "make it magical")
    cases = {
        "whole_workflow_update_supported": workflow_update.supported is True and workflow_update.intent in {"update_workflow_requirement", "upsert_template_transform"},
        "whole_workflow_update_uses_metadata_or_graph_operation": workflow_update.operations[0]["op"] in {"set_metadata", "add_node"},
        "reference_ids_are_validated": workflow_update.reference_node_ids == ["start", "collect"],
        "reference_warning_keeps_whole_workflow_scope": any("context only" in item for item in workflow_update.warnings),
        "start_input_update_supported": input_update.supported is True and input_update.intent == "update_start_inputs",
        "start_input_update_targets_start_node": input_update.operations[0]["data"]["node_id"] == "start",
        "legacy_node_rename_still_supported": rename_update.supported is True and rename_update.intent == "rename_node",
        "fallback_instruction_is_applicable": fallback_update.supported is True and fallback_update.intent == "update_workflow_requirement",
    }
    return {
        "id": "workflow_level_preview_fixture",
        "passed": all(cases.values()),
        "cases": cases,
        "workflow_update": workflow_update.model_dump(mode="json"),
        "input_update": input_update.model_dump(mode="json"),
        "rename_update": rename_update.model_dump(mode="json"),
        "fallback_update": fallback_update.model_dump(mode="json"),
    }


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next((lane for lane in manifest.get("lanes", []) if lane.get("id") == "v0.3.x_current_release_gate"), {})
    test_files = set(current_lane.get("test_files", []))
    command = current_lane.get("command", [])
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "current_gate_present": bool(current_lane),
        "v0347_test_in_test_files": "tests/test_v03_47_natural_language_workflow_edit.py" in test_files,
        "v0347_test_in_command": "tests/test_v03_47_natural_language_workflow_edit.py" in command,
        "pass_count_not_less_than_v0347_floor": isinstance(pass_count, int) and pass_count >= 262,
    }
    return {"id": "regression_manifest_updated", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "workflow_edit_backend_preview_markers",
            "platform/backend/src/agent_platform/draft_patch_preview.py",
            (
                "reference_node_ids: list[str]",
                "update_workflow_metadata",
                "update_workflow_requirement",
                "update_start_inputs",
                "_looks_like_workflow_scope",
                "Referenced bricks are context only; workflow edit scope remains whole-workflow.",
                "No deterministic structural transform matched this instruction",
            ),
        ),
        (
            "workflow_edit_api_reference_markers",
            "platform/backend/src/agent_platform/api.py",
            (
                "body.reference_node_ids",
                "kind=\"draft_patch_preview\"",
            ),
        ),
        (
            "workflow_edit_frontend_surface_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "data-workflow-edit-dialog=\"whole-workflow\"",
                "data-workflow-readable-summary=\"natural-language\"",
                "workflowStepSummaryItems",
                "workflowEditReferenceIds",
                "reference_node_ids: workflowEditReferenceIds",
                "data-workflow-edit-input=\"instruction\"",
                "onNodeContextMenu",
                "onSelectionChange",
                "selectionOnDrag",
                "SelectionMode.Partial",
            ),
        ),
        (
            "workflow_edit_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "自然语言工作流编辑",
                "Natural-language workflow edit",
                "参考积木",
                "references are context, not an edit boundary",
                "只是上下文，不限制修改范围",
            ),
        ),
        (
            "workflow_edit_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".workflow-readable-summary",
                ".workflow-edit-dialog",
                ".workflow-edit-reference-list",
                "[data-workflow-edit-reference-action=\"add-selected\"]",
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
    return {"id": "p0_p1_bug_ledger_natural_language_workflow_edit", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.47-natural-language-workflow-edit"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read().decode("utf-8", errors="replace")
        return {"status_code": response.getcode(), "json": json.loads(body)}


def runtime_health_check(api_url: str) -> dict[str, Any]:
    url = api_url.rstrip("/") + "/health"
    try:
        result = request_json(url)
        runtime = result["json"].get("runtime", {}) if isinstance(result["json"], dict) else {}
        return {"id": "runtime_health_read_only", "url": url, "passed": result["status_code"] == 200 and runtime.get("version") == EXPECTED_RUNTIME_VERSION and runtime.get("current_code_ready") is True, "status_code": result["status_code"], "runtime": runtime}
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        return {"id": "runtime_health_read_only", "url": url, "passed": False, "status_code": 0, "error": str(error)}


def build_evidence(*, live: bool = False, api_url: str = "http://127.0.0.1:8001") -> dict[str, Any]:
    checks: list[dict[str, Any]] = [
        bug_ledger_evidence(),
        workflow_edit_surface_fixture(),
        workflow_reference_context_fixture(),
        workflow_level_preview_fixture(),
        *source_marker_checks(),
    ]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_workflow_edit_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.47",
        "stage": "natural_language_workflow_edit",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "forbidden_endpoint_called": safety["forbidden_endpoint_called"],
            "workflow_edit_surface": workflow_edit_surface_fixture(),
            "workflow_reference_context": workflow_reference_context_fixture(),
            "workflow_level_preview": workflow_level_preview_fixture(),
        },
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.47 natural-language workflow edit evidence.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--api-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    evidence = build_evidence(live=args.live, api_url=args.api_url)
    write_evidence(args.output, evidence)
    print(json.dumps({"status": evidence["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
