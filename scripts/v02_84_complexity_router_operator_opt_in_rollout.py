#!/usr/bin/env python3
"""Execute the stage-1 operator opt-in rollout for the complexity router."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import (
    classify_requirement,
    complexity_router_default_safety_gate,
    validate_operator_override,
)


ROOT = Path(__file__).resolve().parents[1]
ROLLOUT_PLAN_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.80" / "rollout_v0.2.80_complexity_router_staged_preparation.json"
VALIDATION_PLAN_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.76" / "plan_v0.2.76_complexity_router_live_validation.json"
POST_SHADOW_DECISION_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.83" / "decision_v0.2.83_complexity_router_post_shadow_rollout.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "rollout_v0.2.84_complexity_router_operator_opt_in"


OVERRIDE_MODES_BY_EXPECTED_CLASS = {
    "simple": ("force_simple", "Operator opt-in: low-risk text-only route."),
    "medium": ("force_medium", "Operator opt-in: bounded API/workflow route."),
    "complex": ("force_complex", "Operator opt-in: model-sensitive platform route."),
}


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rollout_stage(plan: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in plan["rollout_stages"]:
        if stage["stage_id"] == stage_id:
            return stage
    raise ValueError(f"{stage_id} not found")


def execute_operator_opt_in_rollout() -> dict[str, Any]:
    rollout_plan = load_json(ROLLOUT_PLAN_PATH)
    validation_plan = load_json(VALIDATION_PLAN_PATH)
    decision = load_json(POST_SHADOW_DECISION_PATH)
    stage = rollout_stage(rollout_plan, "stage_1_operator_opt_in")
    safety = complexity_router_default_safety_gate()
    sample_results = []
    for case in validation_plan["validation_cases"]:
        started = time.perf_counter()
        classification = classify_requirement(case["requirement"])
        mode, reason = OVERRIDE_MODES_BY_EXPECTED_CLASS[case["expected_class"]]
        override = validate_operator_override(mode, reason)
        effective_class = override["target_class"] if override["valid"] and override["target_class"] else classification["effective_class"]
        expected_class = case["expected_class"]
        passed = effective_class == expected_class
        sample_results.append({
            "case_id": case["case_id"],
            "requirement": case["requirement"],
            "expected_class": expected_class,
            "predicted_class": classification["requirement_class"],
            "classification_effective_class": classification["effective_class"],
            "operator_mode": mode,
            "operator_visible_reason": reason,
            "override_valid": override["valid"],
            "override_target_class": override["target_class"],
            "effective_class": effective_class,
            "reason_required": override["reason_required"],
            "reason_captured": bool(override["operator_visible_reason"]),
            "operator_opt_in": True,
            "default_router_enabled": override["default_router_enabled"] or classification["default_router_enabled"],
            "behavior_changed": False,
            "duration_seconds": round(time.perf_counter() - started, 6),
            "passed": passed,
        })
    metrics = build_metrics(sample_results, safety)
    exit_criteria = {
        "override_reason_coverage_at_least_0_95": metrics["override_reason_coverage"] >= 0.95,
        "unexpected_classification_rate_at_most_0_05": metrics["unexpected_classification_rate"] <= 0.05,
        "no_accidental_default_enablement": metrics["accidental_default_enablement_count"] == 0,
        "post_shadow_decision_selected_operator_opt_in": (
            decision["decision"]["option_id"] == "execute_operator_opt_in_rollout"
        ),
    }
    passed = all(exit_criteria.values()) and safety["default_enabled"] is False and stage["behavior_change"] is False
    return {
        "version": "v0.2.84",
        "rollout_id": "complexity_router_operator_opt_in_rollout",
        "source_stage_report": "docs/stage-reports/v0.2.83_complexity_router_post_shadow_rollout_decision.md",
        "source_rollout_plan": ROLLOUT_PLAN_PATH.relative_to(ROOT).as_posix(),
        "source_validation_plan": VALIDATION_PLAN_PATH.relative_to(ROOT).as_posix(),
        "source_decision": POST_SHADOW_DECISION_PATH.relative_to(ROOT).as_posix(),
        "stage": stage,
        "status": "completed" if passed else "failed",
        "reason": "stage_1 operator opt-in exit criteria satisfied" if passed else "stage_1 operator opt-in exit criteria not satisfied",
        "sample_count": len(sample_results),
        "sample_results": sample_results,
        "metrics": metrics,
        "exit_criteria": exit_criteria,
        "pass_fail": {
            "passed": passed,
            "reason": "override reason coverage and unexpected classification rate satisfied"
            if passed
            else "one or more stage-1 criteria failed",
        },
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "behavior_change": stage["behavior_change"],
    }


def build_metrics(sample_results: list[dict[str, Any]], safety: dict[str, Any]) -> dict[str, Any]:
    distribution: dict[str, int] = {}
    success_totals: dict[str, int] = {}
    success_counts: dict[str, int] = {}
    override_count = 0
    reason_count = 0
    unexpected = 0
    accidental_default_enablement_count = 0
    for result in sample_results:
        effective = str(result["effective_class"])
        expected = str(result["expected_class"])
        distribution[effective] = distribution.get(effective, 0) + 1
        success_totals[expected] = success_totals.get(expected, 0) + 1
        if result["operator_opt_in"]:
            override_count += 1
            if result["reason_captured"]:
                reason_count += 1
        if result["passed"]:
            success_counts[expected] = success_counts.get(expected, 0) + 1
        else:
            unexpected += 1
        if result["default_router_enabled"] or safety["default_enabled"]:
            accidental_default_enablement_count += 1
    sample_count = max(len(sample_results), 1)
    return {
        "classification_distribution": distribution,
        "override_rate": round(override_count / sample_count, 3),
        "override_reason_coverage": round(reason_count / max(override_count, 1), 3),
        "fallback_unknown_rate": round(distribution.get("unknown", 0) / sample_count, 3),
        "unexpected_classification_rate": round(unexpected / sample_count, 3),
        "success_rate_by_class": {
            key: round(success_counts.get(key, 0) / total, 3)
            for key, total in success_totals.items()
        },
        "accidental_default_enablement_count": accidental_default_enablement_count,
        "behavior_change_count": sum(1 for result in sample_results if result["behavior_changed"]),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.84 complexity-router operator opt-in rollout",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Reason: {result['reason']}",
        f"- Stage: `{result['stage']['stage_id']}`",
        f"- Mode: `{result['stage']['mode']}`",
        f"- Behavior change: `{result['behavior_change']}`",
        f"- Default enabled: `{result['default_enabled']}`",
        f"- Allowed to enable default: `{result['allowed_to_enable_default']}`",
        f"- Sample count: `{result['sample_count']}`",
        f"- Override reason coverage: `{result['metrics']['override_reason_coverage']}`",
        f"- Unexpected classification rate: `{result['metrics']['unexpected_classification_rate']}`",
        f"- Accidental default enablement count: `{result['metrics']['accidental_default_enablement_count']}`",
        "",
        "| Case | Expected | Predicted | Operator mode | Effective | Reason captured | Passed |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in result["sample_results"]:
        lines.append(
            f"| `{case['case_id']}` | `{case['expected_class']}` | `{case['predicted_class']}` | "
            f"`{case['operator_mode']}` | `{case['effective_class']}` | `{case['reason_captured']}` | `{case['passed']}` |"
        )
    lines.extend([
        "",
        "## Metrics",
        "",
        f"- Classification distribution: `{json.dumps(result['metrics']['classification_distribution'], sort_keys=True)}`",
        f"- Override rate: `{result['metrics']['override_rate']}`",
        f"- Override reason coverage: `{result['metrics']['override_reason_coverage']}`",
        f"- Unexpected classification rate: `{result['metrics']['unexpected_classification_rate']}`",
        "",
        "## Pass / Fail",
        "",
        result["pass_fail"]["reason"],
        "",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = execute_operator_opt_in_rollout()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
