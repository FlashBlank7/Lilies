#!/usr/bin/env python3
"""Generate the v0.2.80 complexity-router staged rollout preparation evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "rollout_v0.2.80_complexity_router_staged_preparation"


ROLLOUT_STAGES = [
    {
        "stage_id": "stage_0_shadow_only",
        "mode": "shadow_only",
        "entry_criteria": ["bounded live validation passed", "metrics schema available"],
        "exit_criteria": ["classification distribution recorded", "no accidental default enablement"],
        "behavior_change": False,
    },
    {
        "stage_id": "stage_1_operator_opt_in",
        "mode": "operator_opt_in",
        "entry_criteria": ["stage_0 exit criteria satisfied", "operator reason capture available"],
        "exit_criteria": ["override reason coverage >= 0.95", "unexpected classification rate <= 0.05"],
        "behavior_change": False,
    },
    {
        "stage_id": "stage_2_limited_default_review",
        "mode": "limited_default_review_ready",
        "entry_criteria": ["stage_1 exit criteria satisfied", "frontend verification restored or explicitly waived"],
        "exit_criteria": ["separate stage report selects or rejects limited default enablement"],
        "behavior_change": False,
    },
]

OPERATOR_CONTROLS = [
    "opt_in_for_task",
    "force_simple_with_reason",
    "force_medium_with_reason",
    "force_complex_with_reason",
    "disable_routing_for_task",
    "rollback_to_shadow_only",
]

ROLLBACK_CRITERIA = [
    "unexpected_classification_rate_above_0.05",
    "missing_required_metrics",
    "override_reason_coverage_below_0.95",
    "any_accidental_default_enablement",
]


def build_rollout_preparation() -> dict[str, Any]:
    safety = complexity_router_default_safety_gate()
    return {
        "version": "v0.2.80",
        "plan_id": "complexity_router_staged_rollout_preparation",
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "rollout_stages": ROLLOUT_STAGES,
        "operator_controls": OPERATOR_CONTROLS,
        "rollback_criteria": ROLLBACK_CRITERIA,
        "next_decision_boundary": (
            "A later stage report must select limited default enablement, continue shadow/opt-in rollout, "
            "or defer enablement."
        ),
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
        "# v0.2.80 complexity-router staged rollout preparation",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Default enabled: `{plan['default_enabled']}`",
        f"- Allowed to enable default: `{plan['allowed_to_enable_default']}`",
        f"- Stage count: `{len(plan['rollout_stages'])}`",
        "",
        "| Stage | Mode | Behavior change |",
        "| --- | --- | --- |",
    ]
    for stage in plan["rollout_stages"]:
        lines.append(f"| `{stage['stage_id']}` | `{stage['mode']}` | `{stage['behavior_change']}` |")
    lines.extend([
        "",
        "## Rollback Criteria",
        "",
        ", ".join(f"`{item}`" for item in plan["rollback_criteria"]),
        "",
        "## Conclusion",
        "",
        "Staged rollout preparation is defined. Defaults remain disabled.",
        "",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    plan = build_rollout_preparation()
    json_path, summary_path = write_outputs(plan)
    print(json_path)
    print(summary_path)
    print(plan["default_enabled"])


if __name__ == "__main__":
    main()
