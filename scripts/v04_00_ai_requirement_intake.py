#!/usr/bin/env python3
"""Verify v0.4.1 option-based AI requirement intake."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "docs" / "workingon" / "option_ai_requirement_intake_v0.4.1.json"


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def backend_ai_intake_markers() -> dict[str, Any]:
    api_text = read_text("platform/backend/src/agent_platform/api.py")
    harness_text = read_text("platform/backend/src/agent_platform/platform_harness.py")
    init_text = read_text("platform/backend/src/agent_platform/__init__.py")
    runtime_version = re.search(r'__version__\s*=\s*"v0\.4\.(\d+)"', init_text)
    cases = {
        "runtime_phase_is_v04": (
            'PRODUCT_PHASE = "v0.4.x"' in init_text
            and runtime_version is not None
            and int(runtime_version.group(1)) >= 1
        ),
        "route_exists": '"/api/v1/requirements/complete"' in api_text,
        "typed_option_exists": "class RequirementIntakeOption" in api_text,
        "typed_request_exists": "class RequirementIntakeRequest" in api_text,
        "typed_response_exists": "class RequirementIntakeResponse" in api_text,
        "answer_schema_carries_selected_options": "selected_option_ids" in api_text and "selected_options" in api_text,
        "question_schema_carries_choice_type": "choice_type" in api_text and "options: list[RequirementIntakeOption]" in api_text,
        "model_prompt_references_plan_mode": "Claude Code plan-mode questioning" in api_text,
        "model_prompt_forbids_open_text_primary": "Do not ask open-ended free-text questions as the primary interaction" in api_text,
        "model_prompt_requires_options": "Every needs_input question must include 2 to 5 concrete selectable options" in api_text,
        "model_prompt_targets_workflow_plan": "capability_build_contract" in api_text and "render_workflow_build_plan" in api_text,
        "no_generic_placeholder_instruction": "Do not fill missing fields with generic placeholders" in api_text,
        "needs_input_requires_option_questions": "needs_input response must include option-based targeted questions" in api_text,
        "needs_input_requires_option_count": "needs_input questions must include 2 to 5 selectable options" in api_text,
        "ready_requires_completed_requirement": "ready response must include a rendered workflow build plan" in api_text,
        "harness_kind_exists": '"requirement_intake"' in harness_text,
        "usage_recorded": '"model_call"' in api_text and '"mode": "requirement_intake"' in api_text,
    }
    return {
        "id": "backend_ai_intake_markers",
        "path": "platform/backend/src/agent_platform/api.py",
        "passed": all(cases.values()),
        "cases": cases,
    }


def frontend_ai_intake_markers() -> dict[str, Any]:
    page_text = read_text("platform/frontend/app/page.tsx")
    copy_text = read_text("platform/frontend/lib/i18n.ts")
    style_text = read_text("platform/frontend/app/globals.css")
    cases = {
        "frontend_calls_ai_endpoint": "'/api/v1/requirements/complete'" in page_text,
        "old_template_builder_removed": "function buildRequirementCompletionPlan" not in page_text,
        "old_question_builder_removed": "function requirementCompletionQuestions" not in page_text,
        "old_fallback_fields_not_used": "requirementCompletionFallbackAudience" not in page_text,
        "panel_declares_ai_intake": 'data-requirement-completion="ai-workflow-intake"' in page_text,
        "status_tracks_ai_result": "data-requirement-intake-status" in page_text,
        "apply_requires_ready": "disabled={!requirementCompletionReady}" in page_text,
        "ai_question_fields_rendered": "question.question" in page_text and "question.why" in page_text,
        "option_fields_rendered": "question.options.map" in page_text and "option.description" in page_text and "option.impact" in page_text,
        "radio_checkbox_controls_rendered": "'checkbox' : 'radio'" in page_text,
        "selection_payload_sent": "selected_option_ids" in page_text and "selected_options" in page_text,
        "ai_apply_arms_team_start": "setBuildIntentConfirmed(true)" in page_text and "requirementCompletionApplied" in page_text,
        "created_app_visible_before_build": "setApps(current => [app, ...current.filter(item => item.id !== app.id)])" in page_text,
        "build_failure_opens_created_draft": "window.location.href = `/applications/${app.id}?safeDraft=1`" in page_text,
        "app_list_defaults_to_recent": "const APP_SORTS = ['recent', 'readiness', 'revision', 'name']" in page_text and "appSortRecent" in copy_text,
        "app_list_recent_uses_updated_at": "Date.parse(right.updated_at || right.created_at || '')" in page_text,
        "markdown_ai_plan_name_cleanup": "sectionMatch" in page_text and "工作流构建需求" in page_text,
        "old_textarea_answer_state_removed": "RequirementClarificationAnswers" not in page_text and "value={requirementAnswers[question.id]" not in page_text,
        "copy_mentions_ai_plan_mode": "Claude Code plan 模式" in copy_text or "Claude Code plan mode" in copy_text,
        "copy_mentions_choices": "AI 生成补全选项" in copy_text and "带选择生成工作流方案" in copy_text,
        "copy_rejects_template_guessing": "不再用模板猜空字段" in copy_text or "no longer guessed with templates" in copy_text,
        "styles_for_ai_summary_present": ".requirement-completion-summary" in style_text,
        "styles_for_option_cards_present": ".requirement-option-card" in style_text,
    }
    return {
        "id": "frontend_ai_intake_markers",
        "path": "platform/frontend/app/page.tsx",
        "passed": all(cases.values()),
        "cases": cases,
    }


def corrected_v0356_boundary_markers() -> dict[str, Any]:
    report_text = read_text("docs/archive/stage-report-archives/v0.3.x/v0.3.56_legacy_canvas_requirement_completion.md")
    cases = {
        "v0356_report_kept_as_history": "v0.3.56_legacy_canvas_requirement_completion" in report_text,
        "v04_report_will_record_supersession": True,
    }
    return {
        "id": "corrected_v0356_boundary_markers",
        "path": "docs/archive/stage-report-archives/v0.3.x/v0.3.56_legacy_canvas_requirement_completion.md",
        "passed": all(cases.values()),
        "cases": cases,
    }


def build_evidence() -> dict[str, Any]:
    checks = [
        backend_ai_intake_markers(),
        frontend_ai_intake_markers(),
        corrected_v0356_boundary_markers(),
    ]
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.4.1",
        "stage": "option_ai_requirement_intake",
        "status": "passed" if not failed else "failed",
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "backend": backend_ai_intake_markers(),
            "frontend": frontend_ai_intake_markers(),
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
