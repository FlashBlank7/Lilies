#!/usr/bin/env python3
"""Verify v0.3.55 removal of Japanese learner from customer groups."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".tmp" / "historical-evidence" / "v0.3.55" / "remove_japanese_learner_customer_group_v0.3.55.json"


BUG_LEDGER = (
    {
        "id": "P1-japanese-learner-should-not-be-customer-persona",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "The homepage customer group list and requirement examples still included Japanese learner / 日语学习者.",
        "fix": "Remove the Japanese learner persona and japanese_language_student example from bilingual customer intake copy.",
        "verification": "customer_group_removal_checks.",
    },
    {
        "id": "P1-archived-japanese-tests-conflict-with-current-persona-decision",
        "severity": "P1",
        "status": "fixed",
        "reproduction": "v0.3.49 tests required the removed Japanese learner customer example to remain visible.",
        "fix": "Update the v0.3.49 compatibility check to treat the customer persona as superseded while preserving explicit workflow support.",
        "verification": "v0349_supersession_checks.",
    },
)


def read_text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def section_between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def version_at_least(version: str, floor: str) -> bool:
    def parts(value: str) -> tuple[int, int, int]:
        cleaned = value.removeprefix("v")
        major, minor, patch = cleaned.split(".")
        return int(major), int(minor), int(patch)

    try:
        return parts(version) >= parts(floor)
    except (AttributeError, ValueError):
        return False


def customer_group_removal_checks() -> dict[str, Any]:
    text = read_text("platform/frontend/lib/i18n.ts")
    zh_scenarios = section_between(text, "customerScenarios: [", "customerIntakeTitle: '客户需求样例'")
    zh_examples = section_between(text, "customerExamples: [", "productStepsTitle: '产品路径'")
    en_scenarios = section_between(text, "customerScenarios: [", "customerIntakeTitle: 'Customer requirement examples'")
    en_examples = section_between(text, "customerExamples: [", "productStepsTitle: 'Product path'")
    cases = {
        "zh_customer_scenarios_do_not_include_japanese_learner": "日语学习者" not in zh_scenarios,
        "en_customer_scenarios_do_not_include_japanese_learner": "Japanese learner" not in en_scenarios,
        "zh_customer_examples_do_not_include_japanese_student": "japanese_language_student" not in zh_examples and "今日日语口语总结" not in zh_examples,
        "en_customer_examples_do_not_include_japanese_student": "japanese_language_student" not in en_examples and "Daily spoken Japanese summary" not in en_examples,
        "core_customer_groups_remain": all(marker in text for marker in ("业务负责人", "实施顾问", "运营人员", "技术审阅者", "Business owner", "Implementation consultant", "Operator", "Technical reviewer")),
    }
    return {
        "id": "customer_group_removal_checks",
        "path": "platform/frontend/lib/i18n.ts",
        "passed": all(cases.values()),
        "cases": cases,
    }


def explicit_japanese_workflow_support_preserved_checks() -> dict[str, Any]:
    home_text = read_text("platform/frontend/app/page.tsx")
    detail_text = read_text("platform/frontend/app/applications/[id]/page.tsx")
    i18n_text = read_text("platform/frontend/lib/i18n.ts")
    cases = {
        "typed_requirement_detector_remains": "isJapaneseLearningRequirement" in home_text,
        "safe_draft_seed_remains_for_explicit_requirement": "seedJapaneseLearningDraftSkeleton" in home_text,
        "run_guidance_remains_for_existing_workflows": "isJapaneseLearningWorkflowText" in detail_text and "japaneseLearningWorkflow" in detail_text,
        "scenario_copy_remains_outside_customer_groups": "japaneseLearningScenarioTitle" in i18n_text and "japaneseLearningResultChecklist" in i18n_text,
    }
    return {
        "id": "explicit_japanese_workflow_support_preserved_checks",
        "passed": all(cases.values()),
        "cases": cases,
    }


def v0349_supersession_checks() -> dict[str, Any]:
    script_text = read_text("scripts/v03_49_japanese_learning_customer_journey.py")
    test_text = read_text("tests/test_v03_49_japanese_learning_customer_journey.py")
    cases = {
        "v0349_fixture_checks_removal": "homepage_removed_japanese_learner_role" in script_text,
        "v0349_fixture_checks_example_id_removed": "customer_examples_removed_japanese_language_student" in script_text,
        "v0349_tests_expect_removal": "test_v03_49_intake_no_longer_exposes_japanese_learner_customer_example" in test_text,
        "old_presence_assertion_removed": "homepage_has_japanese_learner_role" not in test_text,
    }
    return {
        "id": "v0349_supersession_checks",
        "passed": all(cases.values()),
        "cases": cases,
    }


def regression_manifest_check() -> dict[str, Any]:
    relative_path = "docs/testing/historical/v0.3.55_regression_lanes.json"
    manifest = json.loads(read_text(relative_path))
    current_lane = next((lane for lane in manifest.get("lanes", []) if lane.get("id") == "v0.3.x_current_release_gate"), {})
    command = current_lane.get("command", [])
    test_files = set(current_lane.get("test_files", []))
    pass_count = current_lane.get("expected", {}).get("pass_count", 0)
    cases = {
        "manifest_version_is_v0355_or_later": version_at_least(str(manifest.get("version", "")), "v0.3.55"),
        "source_stage_report_is_recorded": bool(manifest.get("source_stage_report")),
        "v0355_test_in_command": "tests/test_v03_55_remove_japanese_learner_customer_group.py" in command,
        "v0355_test_in_test_files": "tests/test_v03_55_remove_japanese_learner_customer_group.py" in test_files,
        "pass_count_not_less_than_v0355_floor": isinstance(pass_count, int) and pass_count >= 319,
    }
    return {"id": "regression_manifest_check", "path": relative_path, "passed": all(cases.values()), "cases": cases, "pass_count": pass_count}


def bug_ledger_evidence() -> dict[str, Any]:
    blocking = [
        item
        for item in BUG_LEDGER
        if item["severity"] in {"P0", "P1"} and item["status"] not in {"fixed", "verified_fixed", "deferred_with_reason"}
    ]
    return {
        "id": "p0_p1_bug_ledger_remove_japanese_learner_customer_group",
        "passed": not blocking,
        "bug_count": len(BUG_LEDGER),
        "blocking_bug_count": len(blocking),
        "bugs": list(BUG_LEDGER),
    }


def build_evidence() -> dict[str, Any]:
    checks = [
        bug_ledger_evidence(),
        customer_group_removal_checks(),
        explicit_japanese_workflow_support_preserved_checks(),
        v0349_supersession_checks(),
        regression_manifest_check(),
        {"id": "safety_no_runtime_mutation", "passed": True, "called_endpoints": [], "model_call_used": False},
    ]
    failed = [check for check in checks if not check.get("passed")]
    return {
        "version": "v0.3.55",
        "stage": "remove_japanese_learner_customer_group",
        "status": "passed" if not failed else "failed",
        "bug_ledger": list(BUG_LEDGER),
        "checks": checks,
        "summary": {
            "failed_check_count": len(failed),
            "open_p0_p1_bug_count": bug_ledger_evidence()["blocking_bug_count"],
            "customer_group_removal": customer_group_removal_checks(),
            "explicit_support_preserved": explicit_japanese_workflow_support_preserved_checks(),
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
