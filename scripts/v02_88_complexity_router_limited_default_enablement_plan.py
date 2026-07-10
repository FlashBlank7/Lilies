#!/usr/bin/env python3
"""Generate the limited default enablement plan for the complexity router."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from frontend_verification_runner import ROOT, run_frontend_verification


DEFAULT_REVIEW_DECISION_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.87" / "decision_v0.2.87_complexity_router_default_enablement_review.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "plan_v0.2.88_complexity_router_limited_default_enablement"


def load_default_review_decision() -> dict[str, Any]:
    return json.loads(DEFAULT_REVIEW_DECISION_PATH.read_text(encoding="utf-8"))


def build_limited_default_enablement_plan() -> dict[str, Any]:
    decision = load_default_review_decision()
    frontend = run_frontend_verification()
    safety = complexity_router_default_safety_gate()
    verification_gates = {
        "default_review_selected": decision["decision"]["option_id"] == "enter_default_enablement_review",
        "default_safety_allowed": safety["allowed_to_enable_default"] is True,
        "fresh_frontend_verification_passed": frontend["passed"] is True,
        "runtime_default_still_disabled": safety["default_enabled"] is False,
    }
    return {
        "version": "v0.2.88",
        "plan_id": "complexity_router_limited_default_enablement_plan",
        "source_stage_report": "docs/stage-reports/v0.2.87_complexity_router_default_enablement_review_decision.md",
        "source_default_review_decision": DEFAULT_REVIEW_DECISION_PATH.relative_to(ROOT).as_posix(),
        "implementation_in_this_version": False,
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "limited_default_scope": {
            "mode": "limited_default",
            "eligible_requirement_classes": ["simple", "medium", "complex"],
            "unknown_handling": "complex_equivalent_with_conservative_policy",
            "minimum_confidence_by_class": {
                "simple": 0.55,
                "medium": 0.6,
                "complex": 0.65,
            },
            "behavior": "return default builder policy for eligible classifications while preserving operator override controls",
        },
        "config_contract": {
            "settings_fields_to_add": [
                {
                    "name": "complexity_router_default_mode",
                    "type": "literal",
                    "allowed_values": ["disabled", "shadow_only", "operator_opt_in", "limited_default"],
                    "default": "disabled",
                },
                {
                    "name": "complexity_router_limited_default_enabled",
                    "type": "bool",
                    "default": False,
                },
                {
                    "name": "complexity_router_limited_default_min_confidence",
                    "type": "float",
                    "default": 0.55,
                },
            ],
            "runtime_default": "disabled",
            "rollback_value": "disabled",
        },
        "api_contract": {
            "surfaces_to_add_or_extend": [
                "GET /api/v1/platform/complexity-router/default-safety includes configured default mode",
                "GET /api/v1/platform/complexity-router/default-enableable-plan returns current limited default plan",
                "POST /api/v1/platform/complexity-router/classify-requirement returns default policy only when configured mode allows it",
            ],
            "operator_visible_controls": [
                "disable_routing_for_task",
                "force_simple_with_reason",
                "force_medium_with_reason",
                "force_complex_with_reason",
                "rollback_to_disabled_default",
            ],
        },
        "rollback": {
            "immediate_rollback_mode": "disabled",
            "rollback_triggers": [
                "unexpected_classification_rate_above_0.05",
                "override_reason_coverage_below_0.95",
                "frontend_verification_failure",
                "any_accidental_default_enablement_outside_config",
            ],
        },
        "verification_gates": verification_gates,
        "frontend_verification": frontend,
        "next_implementation_target": "v0.2.89_complexity_router_limited_default_enablement_contract",
        "first_design": "docs/current-design/design_complexity_router_limited_default_enablement_contract.md",
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
        "# v0.2.88 complexity-router limited default enablement plan",
        "",
        f"- Raw plan: `{relative(json_path)}`",
        f"- Implementation in this version: `{plan['implementation_in_this_version']}`",
        f"- Default enabled: `{plan['default_enabled']}`",
        f"- Allowed to enable default: `{plan['allowed_to_enable_default']}`",
        f"- Mode: `{plan['limited_default_scope']['mode']}`",
        f"- Runtime default config value: `{plan['config_contract']['runtime_default']}`",
        f"- Rollback value: `{plan['config_contract']['rollback_value']}`",
        f"- Frontend verification passed: `{plan['frontend_verification']['passed']}`",
        f"- Next implementation target: `{plan['next_implementation_target']}`",
        f"- First design: `{plan['first_design']}`",
        "",
        "| Gate | Passed |",
        "| --- | --- |",
    ]
    for gate, passed in plan["verification_gates"].items():
        lines.append(f"| `{gate}` | `{passed}` |")
    lines.extend([
        "",
        "## Config Contract",
        "",
        "| Field | Default |",
        "| --- | --- |",
    ])
    for field in plan["config_contract"]["settings_fields_to_add"]:
        lines.append(f"| `{field['name']}` | `{field['default']}` |")
    lines.extend(["", "## Rollback Triggers", ""])
    for trigger in plan["rollback"]["rollback_triggers"]:
        lines.append(f"- `{trigger}`")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    plan = build_limited_default_enablement_plan()
    json_path, summary_path = write_outputs(plan)
    print(json_path)
    print(summary_path)
    print(plan["next_implementation_target"])


if __name__ == "__main__":
    main()
