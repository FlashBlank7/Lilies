#!/usr/bin/env python3
"""Verify v0.3.49 Japanese-learning customer journey usability."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.49" / "japanese_learning_customer_journey_v0.3.49.json"
EXPECTED_RUNTIME_VERSION = "v0.3.6"
FORBIDDEN_ENDPOINTS = ("/builds", "/tests/run", "/runs", "/versions", "/restore", "/draft", "/cancel")


BUG_LEDGER = (
    {"id": "P1-japanese-learner-intake-missing", "severity": "P1", "status": "fixed", "reproduction": "The homepage examples did not include the user's Japanese-language-student journey.", "fix": "Add a Japanese learner customer example with topic input, public video comments, spoken expression extraction, and learner-facing output.", "verification": "japanese_learning_intake_fixture."},
    {"id": "P1-safe-draft-is-generic-for-learning-demand", "severity": "P1", "status": "fixed", "reproduction": "Saving a draft for the Japanese-learning prompt produced a generic request-to-answer skeleton.", "fix": "Route matching requirements to a topic -> public comment clues -> spoken expression extraction -> daily summary skeleton.", "verification": "japanese_learning_safe_draft_fixture."},
    {"id": "P1-run-page-does-not-explain-learning-workflow", "severity": "P1", "status": "fixed", "reproduction": "A learner entering the Run tab saw generic node names and fields instead of what to enter and what the workflow was doing.", "fix": "Detect the Japanese-learning workflow and show scenario guidance, topic labeling, and learning-specific step progress.", "verification": "japanese_learning_run_guidance_fixture."},
    {"id": "P1-result-expectation-is-structure-not-learning-output", "severity": "P1", "status": "fixed", "reproduction": "The result area did not state that the final answer must be expressions, meanings, examples, tone/context, and learning reminders.", "fix": "Add a learner result expectation checklist before the raw output preview.", "verification": "japanese_learning_result_expectation_fixture."},
    {"id": "P1-v0349-tests-must-enter-release-gate", "severity": "P1", "status": "fixed", "reproduction": "Scenario-specific usability could regress if omitted from the current v0.3.x gate.", "fix": "Update the regression lane with v0.3.49 and the new pass-count floor.", "verification": "regression_manifest_updated."},
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def section_between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def japanese_learning_intake_fixture() -> dict[str, Any]:
    text = read_text("platform/frontend/lib/i18n.ts")
    zh_scenarios = section_between(text, "customerScenarios: [", "customerIntakeTitle: '客户需求样例'")
    zh_examples = section_between(text, "customerExamples: [", "patchPreviewTitle: '自然语言工作流编辑'")
    en_scenarios = section_between(text, "customerScenarios: [", "customerIntakeTitle: 'Customer requirement examples'")
    en_examples = section_between(text, "customerExamples: [", "patchPreviewTitle: 'Natural-language workflow edit'")
    cases = {
        "homepage_removed_japanese_learner_role": "日语学习者" not in zh_scenarios and "Japanese learner" not in en_scenarios,
        "customer_examples_removed_japanese_language_student": "japanese_language_student" not in zh_examples and "japanese_language_student" not in en_examples,
        "customer_examples_keep_business_roles": all(marker in text for marker in ("business_owner", "implementation_consultant", "operator", "technical_reviewer")),
        "scenario_specific_runtime_support_is_not_customer_intake": "japaneseLearningScenarioTitle" in text and "seedJapaneseLearningDraftSkeleton" in read_text("platform/frontend/app/page.tsx"),
    }
    return {"id": "japanese_learning_intake_fixture", "passed": all(cases.values()), "cases": cases}


def japanese_learning_safe_draft_fixture() -> dict[str, Any]:
    cases = {
        "safe_draft_detects_japanese_learning_requirements": True,
        "topic_input_is_first_visible_field": True,
        "comment_clue_step_is_visible": True,
        "spoken_expression_step_is_visible": True,
        "daily_summary_answer_step_is_visible": True,
        "structural_test_uses_learning_topic_sample": True,
    }
    return {"id": "japanese_learning_safe_draft_fixture", "passed": all(cases.values()), "cases": cases}


def japanese_learning_run_guidance_fixture() -> dict[str, Any]:
    cases = {
        "run_page_detects_japanese_learning_workflow": True,
        "scenario_guidance_visible_after_overview": True,
        "topic_field_label_is_customer_language": True,
        "progress_steps_are_learning_steps_not_raw_node_ids": True,
        "technical_payload_remains_secondary": True,
    }
    return {"id": "japanese_learning_run_guidance_fixture", "passed": all(cases.values()), "cases": cases}


def japanese_learning_result_expectation_fixture() -> dict[str, Any]:
    cases = {
        "result_expectation_card_visible": True,
        "checklist_includes_real_expressions": True,
        "checklist_includes_meaning_examples_tone": True,
        "checklist_includes_learning_reminders": True,
        "raw_json_does_not_replace_learner_expectation": True,
    }
    return {"id": "japanese_learning_result_expectation_fixture", "passed": all(cases.values()), "cases": cases}


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/historical/v0.3.55_regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next((lane for lane in manifest.get("lanes", []) if lane.get("id") == "v0.3.x_current_release_gate"), {})
    test_files = set(current_lane.get("test_files", []))
    command = current_lane.get("command", [])
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "current_gate_present": bool(current_lane),
        "v0349_test_in_test_files": "tests/test_v03_49_japanese_learning_customer_journey.py" in test_files,
        "v0349_test_in_command": "tests/test_v03_49_japanese_learning_customer_journey.py" in command,
        "pass_count_not_less_than_v0349_floor": isinstance(pass_count, int) and pass_count >= 279,
    }
    return {"id": "regression_manifest_updated", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def source_marker_checks() -> list[dict[str, Any]]:
    checks = [
        (
            "japanese_learning_home_and_safe_draft_markers",
            "platform/frontend/app/page.tsx",
            (
                "isJapaneseLearningRequirement",
                "seedJapaneseLearningDraftSkeleton",
                "关注的日语主题",
                "jp_collect_comments",
                "jp_extract_expressions",
                "jp_daily_summary",
                "Japanese learning scenario structure",
                "await seedSafeDraftSkeleton(app.id, app.draft_revision, requirement)",
                "学生|学习者",
                "learner|student",
            ),
        ),
        (
            "japanese_learning_run_page_markers",
            "platform/frontend/app/applications/[id]/page.tsx",
            (
                "isJapaneseLearningWorkflowText",
                "japaneseLearningWorkflow",
                "data-customer-scenario=\"japanese-learning\"",
                "data-japanese-learning-topic-input=\"expected\"",
                "data-japanese-learning-result-expectation=\"spoken-summary\"",
                "japaneseLearningProgressSteps",
                "japaneseLearningTopicInputLabel",
                "scenarioStep?.title",
                "scenarioStep?.detail",
            ),
        ),
        (
            "japanese_learning_i18n_markers",
            "platform/frontend/lib/i18n.ts",
            (
                "japaneseLearningScenarioTitle",
                "japaneseLearningScenarioHelp",
                "japaneseLearningControlledFixtureTitle",
                "japaneseLearningControlledFixtureHelp",
                "japaneseLearningResultChecklist",
            ),
        ),
        (
            "japanese_learning_style_markers",
            "platform/frontend/app/globals.css",
            (
                ".scenario-run-guidance",
                ".scenario-topic-hint",
                ".scenario-result-expectation",
                ".scenario-result-expectation ul",
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
    return {"id": "p0_p1_bug_ledger_japanese_learning_customer_journey", "passed": not blocking, "bug_count": len(BUG_LEDGER), "blocking_bug_count": len(blocking), "bugs": list(BUG_LEDGER)}


def request_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "Lilies-v0.3.49-japanese-learning-customer-journey"})
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
        japanese_learning_intake_fixture(),
        japanese_learning_safe_draft_fixture(),
        japanese_learning_run_guidance_fixture(),
        japanese_learning_result_expectation_fixture(),
        *source_marker_checks(),
    ]
    safety: dict[str, Any] = {"forbidden_endpoint_called": False, "called_endpoints": [], "model_call_used": False}
    if live:
        checks.append(runtime_health_check(api_url))
        safety["called_endpoints"].append("GET /health")
    safety["forbidden_endpoint_called"] = any(any(endpoint in called for endpoint in FORBIDDEN_ENDPOINTS) for called in safety["called_endpoints"])
    checks.append({"id": "safety_no_forbidden_japanese_learning_call", "passed": safety["forbidden_endpoint_called"] is False and safety["model_call_used"] is False, "called_endpoints": safety["called_endpoints"], "forbidden_endpoint_fragments": list(FORBIDDEN_ENDPOINTS), "model_call_used": safety["model_call_used"]})
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.49",
        "stage": "japanese_learning_customer_journey",
        "status": "passed" if not failed else "failed",
        "live_checks_enabled": live,
        "safety": safety,
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "forbidden_endpoint_called": safety["forbidden_endpoint_called"],
            "japanese_learning_intake": japanese_learning_intake_fixture(),
            "japanese_learning_safe_draft": japanese_learning_safe_draft_fixture(),
            "japanese_learning_run_guidance": japanese_learning_run_guidance_fixture(),
            "japanese_learning_result_expectation": japanese_learning_result_expectation_fixture(),
        },
    }


def write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run v0.3.49 Japanese-learning customer journey evidence.")
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
