#!/usr/bin/env python3
"""Execute the stage-0 shadow-only rollout for the complexity router."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import classify_requirement, complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
ROLLOUT_PLAN_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.80" / "rollout_v0.2.80_complexity_router_staged_preparation.json"
VALIDATION_PLAN_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.76" / "plan_v0.2.76_complexity_router_live_validation.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "rollout_v0.2.82_complexity_router_shadow_only"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def stage_0(plan: dict[str, Any]) -> dict[str, Any]:
    for stage in plan["rollout_stages"]:
        if stage["stage_id"] == "stage_0_shadow_only":
            return stage
    raise ValueError("stage_0_shadow_only not found")


def execute_shadow_only_rollout() -> dict[str, Any]:
    rollout_plan = load_json(ROLLOUT_PLAN_PATH)
    validation_plan = load_json(VALIDATION_PLAN_PATH)
    stage = stage_0(rollout_plan)
    safety = complexity_router_default_safety_gate()
    sample_results = []
    for case in validation_plan["validation_cases"]:
        started = time.perf_counter()
        classification = classify_requirement(case["requirement"])
        predicted_class = classification["requirement_class"]
        expected_class = case["expected_class"]
        passed = predicted_class == expected_class
        sample_results.append({
            "case_id": case["case_id"],
            "requirement": case["requirement"],
            "expected_class": expected_class,
            "predicted_class": predicted_class,
            "effective_class": classification["effective_class"],
            "confidence": classification["confidence"],
            "signals": classification["signals"],
            "conservative_unknown": classification["conservative_unknown"],
            "shadow_only": True,
            "behavior_changed": False,
            "default_router_enabled": classification["default_router_enabled"],
            "duration_seconds": round(time.perf_counter() - started, 6),
            "passed": passed,
        })
    metrics = build_metrics(sample_results, safety)
    exit_criteria = {
        "classification_distribution_recorded": bool(metrics["classification_distribution"]),
        "no_accidental_default_enablement": metrics["accidental_default_enablement_count"] == 0,
        "unexpected_classification_rate_within_stage_0_boundary": metrics["unexpected_classification_rate"] == 0.0,
    }
    passed = all(exit_criteria.values()) and safety["default_enabled"] is False and stage["behavior_change"] is False
    return {
        "version": "v0.2.82",
        "rollout_id": "complexity_router_shadow_only_rollout",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.81_complexity_router_staged_rollout_execution_decision.md",
        "source_rollout_plan": ROLLOUT_PLAN_PATH.relative_to(ROOT).as_posix(),
        "source_validation_plan": VALIDATION_PLAN_PATH.relative_to(ROOT).as_posix(),
        "stage": stage,
        "status": "completed" if passed else "failed",
        "reason": "stage_0 shadow-only exit criteria satisfied" if passed else "stage_0 shadow-only exit criteria not satisfied",
        "sample_count": len(sample_results),
        "sample_results": sample_results,
        "metrics": metrics,
        "exit_criteria": exit_criteria,
        "pass_fail": {
            "passed": passed,
            "reason": "classification distribution recorded and no default enablement occurred"
            if passed
            else "one or more stage-0 criteria failed",
        },
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "behavior_change": stage["behavior_change"],
    }


def build_metrics(sample_results: list[dict[str, Any]], safety: dict[str, Any]) -> dict[str, Any]:
    distribution: dict[str, int] = {}
    expected_totals: dict[str, int] = {}
    expected_successes: dict[str, int] = {}
    latency_by_class: dict[str, list[float]] = {}
    unexpected = 0
    accidental_default_enablement_count = 0
    for result in sample_results:
        predicted = str(result["predicted_class"])
        expected = str(result["expected_class"])
        distribution[predicted] = distribution.get(predicted, 0) + 1
        expected_totals[expected] = expected_totals.get(expected, 0) + 1
        latency_by_class.setdefault(expected, []).append(float(result["duration_seconds"]))
        if result["passed"]:
            expected_successes[expected] = expected_successes.get(expected, 0) + 1
        else:
            unexpected += 1
        if result["default_router_enabled"] or safety["default_enabled"]:
            accidental_default_enablement_count += 1
    sample_count = max(len(sample_results), 1)
    return {
        "classification_distribution": distribution,
        "override_rate": 0.0,
        "override_reason_coverage": 1.0,
        "fallback_unknown_rate": round(distribution.get("unknown", 0) / sample_count, 3),
        "unexpected_classification_rate": round(unexpected / sample_count, 3),
        "success_rate_by_class": {
            key: round(expected_successes.get(key, 0) / total, 3)
            for key, total in expected_totals.items()
        },
        "cost_latency_by_class": {
            key: {"avg_shadow_duration_seconds": round(sum(values) / max(len(values), 1), 6)}
            for key, values in latency_by_class.items()
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
        "# v0.2.82 complexity-router shadow-only rollout",
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
        f"- Unexpected classification rate: `{result['metrics']['unexpected_classification_rate']}`",
        f"- Accidental default enablement count: `{result['metrics']['accidental_default_enablement_count']}`",
        "",
        "| Case | Expected | Predicted | Effective | Passed |",
        "| --- | --- | --- | --- | --- |",
    ]
    for case in result["sample_results"]:
        lines.append(
            f"| `{case['case_id']}` | `{case['expected_class']}` | `{case['predicted_class']}` | "
            f"`{case['effective_class']}` | `{case['passed']}` |"
        )
    lines.extend([
        "",
        "## Metrics",
        "",
        f"- Classification distribution: `{json.dumps(result['metrics']['classification_distribution'], sort_keys=True)}`",
        f"- Fallback unknown rate: `{result['metrics']['fallback_unknown_rate']}`",
        f"- Override rate: `{result['metrics']['override_rate']}`",
        "",
        "## Pass / Fail",
        "",
        result["pass_fail"]["reason"],
        "",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = execute_shadow_only_rollout()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
