#!/usr/bin/env python3
"""Generate the v0.2.76 complexity-router live validation plan."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "plan_v0.2.76_complexity_router_live_validation"


VALIDATION_CASES = [
    {
        "case_id": "simple_text_edit",
        "requirement": "Fix a typo in a settings label",
        "expected_class": "simple",
    },
    {
        "case_id": "medium_api_workflow",
        "requirement": "Add an API endpoint with tests for a reporting workflow",
        "expected_class": "medium",
    },
    {
        "case_id": "complex_platform_guardrail",
        "requirement": "Design a platform guardrail rollout for a model-sensitive agent router",
        "expected_class": "complex",
    },
]

REQUIRED_METRICS = [
    "classification_distribution",
    "override_rate",
    "fallback_unknown_rate",
    "success_rate_by_class",
    "cost_latency_by_class",
]


def build_plan() -> dict[str, Any]:
    safety = complexity_router_default_safety_gate()
    return {
        "version": "v0.2.76",
        "plan_id": "complexity_router_live_validation_plan",
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "execution_in_this_stage": False,
        "validation_cases": VALIDATION_CASES,
        "metrics_capture": REQUIRED_METRICS,
        "budget_boundary": {
            "max_live_cases": 3,
            "default_enablement_allowed": False,
            "record_provider_model_command_and_skip_reason": True,
        },
        "pass_criteria": [
            "each case classified as expected",
            "required metrics captured",
            "no default router behavior enabled",
            "no unresolved critical error",
        ],
        "fail_criteria": [
            "unexpected class",
            "missing metrics",
            "budget breach",
            "accidental default enablement",
        ],
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(plan: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.76 complexity-router live validation plan",
        "",
        f"- Raw plan: `{relative(json_path)}`",
        f"- Execution in this stage: `{plan['execution_in_this_stage']}`",
        f"- Default enabled: `{plan['default_enabled']}`",
        f"- Allowed to enable default: `{plan['allowed_to_enable_default']}`",
        f"- Max live cases: `{plan['budget_boundary']['max_live_cases']}`",
        "",
        "| Case | Expected class | Requirement |",
        "| --- | --- | --- |",
    ]
    for case in plan["validation_cases"]:
        lines.append(f"| `{case['case_id']}` | `{case['expected_class']}` | {case['requirement']} |")
    lines.extend([
        "",
        "## Conclusion",
        "",
        "Live validation is planned but not executed in this stage. Defaults remain disabled.",
        "",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    plan = build_plan()
    json_path, summary_path = write_outputs(plan)
    print(json_path)
    print(summary_path)
    print(plan["execution_in_this_stage"])


if __name__ == "__main__":
    main()
