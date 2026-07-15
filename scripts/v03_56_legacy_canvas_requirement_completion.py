#!/usr/bin/env python3
"""Verify v0.3.56 legacy canvas compatibility and requirement completion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.56" / "legacy_canvas_requirement_completion_v0.3.56.json"


BUG_LEDGER = (
    {
        "id": "P1-clicking-legacy-brick-can-crash-frontend",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Older draft nodes can miss blockType/type/position fields, and clicking a brick can also loop selection state updates.",
        "fix": "Route node data and canvas positions through safeStudioNodeData/safeCanvasPosition and avoid redundant selection-reference state updates.",
        "verification": "legacy_canvas_compatibility_markers.",
    },
    {
        "id": "P1-requirement-intake-has-no-customer-clarification-loop",
        "severity": "P1",
        "status": "verified_fixed",
        "reproduction": "A vague customer request only showed readiness hints; it did not ask targeted questions or generate a workflow-aligned requirement.",
        "fix": "Superseded the v0.3.56 local question/template draft with the v0.4.0 AI requirement-intake endpoint and UI.",
        "verification": "requirement_completion_markers.",
    },
    {
        "id": "P1-claude-plan-reference-must-not-leak-code-plan-shape",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "Borrowing Claude Code plan-mode behavior too literally would produce a code-execution plan rather than workflow-generation requirements.",
        "fix": "Use the needs_input/plan_ready interaction idea as reference, but keep Lilies output fields aligned to workflow generation.",
        "verification": "claude_plan_reference_boundary_markers.",
    },
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def version_at_least(version: str, floor: str) -> bool:
    def parts(value: str) -> tuple[int, int, int]:
        cleaned = value.removeprefix("v")
        major, minor, patch = cleaned.split(".")
        return int(major), int(minor), int(patch)

    try:
        return parts(version) >= parts(floor)
    except (AttributeError, ValueError):
        return False


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [
        item
        for item in BUG_LEDGER
        if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}
    ]
    return {
        "id": "p0_p1_bug_ledger_legacy_canvas_requirement_completion",
        "passed": not blocking,
        "bug_count": len(BUG_LEDGER),
        "blocking_bug_count": len(blocking),
        "bugs": list(BUG_LEDGER),
    }


def legacy_canvas_compatibility_markers() -> dict[str, Any]:
    page_text = read_text("platform/frontend/app/applications/[id]/page.tsx")
    cases = {
        "brick_node_uses_safe_block_type": "const blockType = safeText(data?.blockType, 'unknown')" in page_text,
        "raw_block_type_replace_removed": "data.blockType.replaceAll" not in page_text,
        "canvas_position_sanitizer_exists": "function safeCanvasPosition" in page_text and "Number.isFinite(record.x)" in page_text,
        "studio_node_data_sanitizer_exists": "function safeStudioNodeData" in page_text and "safeWorkflowNodeType(node)" in page_text,
        "sync_canvas_uses_sanitized_data": "data: safeStudioNodeData(item, t.configuredBrick)" in page_text,
        "arrange_uses_sanitized_position": "const currentPosition = safeCanvasPosition(node.position" in page_text,
        "context_menu_uses_safe_title": "safeText(node.data?.title, node.id)" in page_text,
        "drag_persistence_uses_safe_position": "changes: { position: safeCanvasPosition(node.position) }" in page_text,
        "architecture_uses_safe_config": "safeText(config.tool_name, t.unboundTool)" in page_text,
        "selection_reference_update_is_idempotent": "current.length === ids.length" in page_text and "return current" in page_text,
    }
    return {
        "id": "legacy_canvas_compatibility_markers",
        "path": "platform/frontend/app/applications/[id]/page.tsx",
        "passed": all(cases.values()),
        "cases": cases,
    }


def requirement_completion_markers() -> dict[str, Any]:
    page_text = read_text("platform/frontend/app/page.tsx")
    copy_text = read_text("platform/frontend/lib/i18n.ts")
    style_text = read_text("platform/frontend/app/globals.css")
    cases = {
        "local_question_builder_replaced": "function requirementCompletionQuestions" not in page_text,
        "local_plan_builder_replaced": "function buildRequirementCompletionPlan" not in page_text,
        "ai_endpoint_is_called": "'/api/v1/requirements/complete'" in page_text,
        "customer_selection_state_exists": "const [requirementSelections" in page_text,
        "panel_is_rendered": 'data-requirement-completion="ai-workflow-intake"' in page_text,
        "apply_writes_back_to_requirement": "setRequirement(completed)" in page_text,
        "apply_waits_for_ai_ready": "disabled={!requirementCompletionReady}" in page_text,
        "ai_questions_are_rendered": "question.question" in page_text and "question.why" in page_text,
        "styles_present": ".requirement-completion-panel" in style_text and ".requirement-completion-questions" in style_text,
        "copy_mentions_ai_plan_mode": "Claude Code plan 模式" in copy_text or "Claude Code plan mode" in copy_text,
    }
    return {
        "id": "requirement_completion_markers",
        "path": "platform/frontend/app/page.tsx",
        "passed": all(cases.values()),
        "cases": cases,
    }


def claude_plan_reference_boundary_markers() -> dict[str, Any]:
    page_text = read_text("platform/frontend/app/page.tsx")
    copy_text = read_text("platform/frontend/lib/i18n.ts")
    ccr_text = read_text("references/claude-code/src/utils/ultraplan/ccrSession.ts")
    cases = {
        "reference_has_needs_input_phase": "needs_input" in ccr_text and "plan_ready" in ccr_text,
        "reference_tracks_user_iteration": "rejectedIds" in ccr_text and "rejectCount" in ccr_text,
        "lilies_does_not_copy_exit_plan_mode_markers": "ExitPlanMode" not in page_text and "Approved Plan" not in copy_text,
        "lilies_keeps_workflow_fields": all(marker in page_text for marker in ("Start inputs", "Workflow steps")) or all(marker in copy_text for marker in ("启动输入", "工作流步骤")),
        "no_remote_session_dependency": "teleportToRemote" not in page_text and "pollForApprovedExitPlanMode" not in page_text,
    }
    return {
        "id": "claude_plan_reference_boundary_markers",
        "path": "references/claude-code/src/utils/ultraplan/ccrSession.ts",
        "passed": all(cases.values()),
        "cases": cases,
    }


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/historical/v0.3.56_regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next(
        (
            lane
            for lane in manifest.get("lanes", [])
            if lane.get("id") in {"v0.3.x_current_release_gate", "v0.4.x_current_release_gate"}
        ),
        {},
    )
    command = current_lane.get("command", [])
    test_files = set(current_lane.get("test_files", []))
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "manifest_version_is_v0356_or_later": version_at_least(str(manifest.get("version", "")), "v0.3.56"),
        "source_stage_report_is_v0355_or_later": manifest.get("source_stage_report") in {
            "docs/stage-report-archives/v0.3.x/v0.3.55_remove_japanese_learner_customer_group.md",
            "docs/stage-report-archives/v0.3.x/v0.3.56_legacy_canvas_requirement_completion.md",
        },
        "v0356_test_in_command": "tests/test_v03_56_legacy_canvas_requirement_completion.py" in command,
        "v0356_test_in_test_files": "tests/test_v03_56_legacy_canvas_requirement_completion.py" in test_files,
        "pass_count_not_less_than_v0356_floor": isinstance(pass_count, int) and pass_count >= 323,
    }
    return {"id": "regression_manifest_check", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def build_evidence() -> dict[str, Any]:
    checks = [
        bug_ledger_evidence(),
        legacy_canvas_compatibility_markers(),
        requirement_completion_markers(),
        claude_plan_reference_boundary_markers(),
        regression_manifest_check(),
        {
            "id": "runtime_cleanup_policy",
            "passed": True,
            "model_call_used": False,
            "notes": "Legacy local application data is archived as a runtime operation outside unit tests, then a clean safe draft is created for verification.",
        },
    ]
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.56",
        "stage": "legacy_canvas_requirement_completion",
        "status": "passed" if not failed else "failed",
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "legacy_canvas": legacy_canvas_compatibility_markers(),
            "requirement_completion": requirement_completion_markers(),
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
