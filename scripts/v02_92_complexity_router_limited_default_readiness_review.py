#!/usr/bin/env python3
"""Generate v0.2.92 limited-default readiness review evidence."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from frontend_verification_runner import ROOT, run_frontend_verification


METRICS_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.91" / "metrics_v0.2.91_complexity_router_runtime_activation_observability.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.92_complexity_router_limited_default_readiness_review"


def gate(name: str, passed: bool, evidence: str, reason: str) -> dict[str, Any]:
    return {
        "name": name,
        "passed": passed,
        "evidence": evidence,
        "reason": reason,
    }


def evaluate_readiness(metrics_evidence: dict[str, Any], frontend: dict[str, Any] | None = None) -> dict[str, Any]:
    default_metrics = metrics_evidence["default_metrics"]
    enabled_metrics = metrics_evidence["enabled_metrics"]
    frontend = frontend or metrics_evidence.get("frontend_verification", {})
    default_categories = default_metrics["decision_categories"]
    enabled_categories = enabled_metrics["decision_categories"]
    readiness_gates = [
        gate(
            "runtime_activation_evidence",
            enabled_categories["active"] >= 2,
            "docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability_summary.md",
            "explicit limited-default metrics must include at least two active runtime decisions",
        ),
        gate(
            "observability_categories",
            all(
                key in enabled_categories
                for key in ["active", "bypassed", "conservative_unknown", "request_override"]
            ),
            "platform/backend/src/agent_platform/complexity_router.py",
            "metrics must distinguish active, bypassed, unknown, and request override decisions",
        ),
        gate(
            "disabled_default_safety",
            default_categories["active"] == 0 and default_categories["disabled_default"] >= 1,
            "docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability_summary.md",
            "normal default settings must remain inactive",
        ),
        gate(
            "unknown_bypass_safety",
            enabled_categories["conservative_unknown"] >= 1 and enabled_categories["bypassed"] >= 1,
            "docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability_summary.md",
            "unknown requirements must remain bypassed and counted",
        ),
        gate(
            "request_override_visibility",
            enabled_categories["request_override"] >= 1,
            "docs/workingon-archives/v0.2.91/metrics_v0.2.91_complexity_router_runtime_activation_observability_summary.md",
            "request override visibility must be present before broader rollout",
        ),
        gate(
            "rollback_to_disabled",
            default_metrics["rollback_value"] == "disabled" and enabled_metrics["rollback_value"] == "disabled",
            "runtime activation metrics response",
            "rollback value must remain disabled",
        ),
        gate(
            "frontend_verification",
            frontend.get("passed") is True,
            "scripts/frontend_verification_runner.py",
            "frontend verification must pass",
        ),
    ]
    all_passed = all(item["passed"] for item in readiness_gates)
    if all_passed:
        decision = {
            "decision": "enter_guarded_default_rollout",
            "next_version": "v0.2.93_complexity_router_guarded_default_rollout",
            "first_design": "docs/current-design/design_v0_2_93_complexity_router_guarded_default_rollout.md",
            "reason": "runtime activation, observability, rollback, unknown bypass, request override, and frontend gates passed",
        }
    else:
        decision = {
            "decision": "collect_more_runtime_evidence",
            "next_version": "v0.2.x_complexity_router_additional_observation",
            "first_design": "docs/current-design/design_complexity_router_additional_observation.md",
            "reason": "one or more readiness gates failed",
        }
    return {
        "version": "v0.2.92",
        "decision_id": "complexity_router_limited_default_readiness_review",
        "source_stage_report": "docs/stage-reports/v0.2.91_complexity_router_runtime_activation_observability.md",
        "status": "completed",
        "normal_default_settings": "disabled",
        "readiness_gates": readiness_gates,
        "decision": decision,
    }


def build_readiness_review(metrics_path: Path = METRICS_PATH) -> dict[str, Any]:
    metrics_evidence = json.loads(metrics_path.read_text(encoding="utf-8"))
    frontend = run_frontend_verification()
    result = evaluate_readiness(metrics_evidence, frontend)
    result["frontend_verification"] = frontend
    return result


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
    failed = [item for item in result["readiness_gates"] if not item["passed"]]
    lines = [
        "# v0.2.92 complexity-router limited default readiness review",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']['decision']}`",
        f"- Next version: `{result['decision']['next_version']}`",
        f"- Normal default settings: `{result['normal_default_settings']}`",
        f"- Gates passed: `{len(result['readiness_gates']) - len(failed)}/{len(result['readiness_gates'])}`",
        f"- Frontend verification passed: `{result['frontend_verification']['passed']}`",
        "",
        "## Gate Results",
        "",
    ]
    for item in result["readiness_gates"]:
        lines.append(f"- `{item['name']}`: `{item['passed']}` - {item['reason']}")
    lines.extend([
        "",
        "## Decision Boundary",
        "",
        "- This readiness review does not change normal default settings.",
        "- A guarded default rollout must preserve rollback value `disabled` and conservative unknown bypass behavior.",
        "",
    ])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = build_readiness_review()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["decision"]["decision"])


if __name__ == "__main__":
    main()
