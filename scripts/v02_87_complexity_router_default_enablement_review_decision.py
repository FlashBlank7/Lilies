#!/usr/bin/env python3
"""Decide whether to enter complexity-router default enablement review."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from frontend_verification_runner import ROOT, run_frontend_verification


SHADOW_EVIDENCE_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.82" / "rollout_v0.2.82_complexity_router_shadow_only.json"
OPERATOR_OPT_IN_EVIDENCE_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.84" / "rollout_v0.2.84_complexity_router_operator_opt_in.json"
FRONTEND_REPAIR_EVIDENCE_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.86" / "verification_v0.2.86_frontend_environment_repair.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.87_complexity_router_default_enablement_review"


@dataclass(frozen=True)
class DefaultReviewOption:
    option_id: str
    label: str
    safety: int
    productization_value: int
    evidence_readiness: int
    verification_readiness: int
    behavior_change_risk: int
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return (
            self.safety
            + self.productization_value
            + self.evidence_readiness
            + self.verification_readiness
            - self.behavior_change_risk
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "score": self.score,
            "safety": self.safety,
            "productization_value": self.productization_value,
            "evidence_readiness": self.evidence_readiness,
            "verification_readiness": self.verification_readiness,
            "behavior_change_risk": self.behavior_change_risk,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


OPTIONS = [
    DefaultReviewOption(
        option_id="enter_default_enablement_review",
        label="Enter default enablement review",
        safety=5,
        productization_value=5,
        evidence_readiness=5,
        verification_readiness=5,
        behavior_change_risk=1,
        disposition="selected",
        next_version="v0.2.88_complexity_router_limited_default_enablement_plan",
        first_design="docs/current-design/design_complexity_router_limited_default_enablement_plan.md",
    ),
    DefaultReviewOption(
        option_id="continue_operator_opt_in_observation",
        label="Continue operator opt-in observation",
        safety=5,
        productization_value=2,
        evidence_readiness=4,
        verification_readiness=5,
        behavior_change_risk=0,
        disposition="rejected because stage-1 opt-in metrics and frontend verification are already satisfied",
        next_version="v0.2.x_complexity_router_continued_operator_opt_in_observation",
        first_design="docs/current-design/design_complexity_router_continued_operator_opt_in_observation.md",
    ),
    DefaultReviewOption(
        option_id="explicit_default_review_deferral",
        label="Explicitly defer default enablement review",
        safety=5,
        productization_value=1,
        evidence_readiness=3,
        verification_readiness=5,
        behavior_change_risk=0,
        disposition="rejected because no current blocker remains after frontend verification repair",
        next_version="v0.2.x_complexity_router_default_review_deferred",
        first_design="docs/current-design/design_complexity_router_default_review_deferred.md",
    ),
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def decide_default_enablement_review() -> dict[str, Any]:
    shadow = load_json(SHADOW_EVIDENCE_PATH)
    operator = load_json(OPERATOR_OPT_IN_EVIDENCE_PATH)
    repair = load_json(FRONTEND_REPAIR_EVIDENCE_PATH)
    fresh_frontend = run_frontend_verification()
    safety = complexity_router_default_safety_gate()
    gates = {
        "default_safety_allowed": safety["allowed_to_enable_default"] is True,
        "shadow_rollout_passed": shadow["pass_fail"]["passed"] is True,
        "operator_opt_in_passed": operator["pass_fail"]["passed"] is True,
        "frontend_repair_passed": repair["pass_fail"]["passed"] is True,
        "fresh_frontend_verification_passed": fresh_frontend["passed"] is True,
        "no_default_enabled_yet": safety["default_enabled"] is False,
    }
    all_gates_passed = all(gates.values())
    ranked = sorted(OPTIONS, key=lambda option: (-option.score, option.option_id))
    decision = ranked[0] if all_gates_passed else OPTIONS[2]
    return {
        "version": "v0.2.87",
        "decision_id": "complexity_router_default_enablement_review_decision",
        "source_stage_report": "docs/stage-reports/v0.2.86_frontend_verification_environment_repair.md",
        "source_shadow_evidence": SHADOW_EVIDENCE_PATH.relative_to(ROOT).as_posix(),
        "source_operator_opt_in_evidence": OPERATOR_OPT_IN_EVIDENCE_PATH.relative_to(ROOT).as_posix(),
        "source_frontend_repair_evidence": FRONTEND_REPAIR_EVIDENCE_PATH.relative_to(ROOT).as_posix(),
        "gates": gates,
        "all_gates_passed": all_gates_passed,
        "fresh_frontend_verification": fresh_frontend,
        "decision": decision.to_json(),
        "options": [option.to_json() for option in ranked],
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "conclusion": (
            "Enter default enablement review next. Defaults remain disabled in this version."
            if all_gates_passed
            else "Defer default enablement review because one or more gates failed."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(decision: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.87 complexity-router default enablement review decision",
        "",
        f"- Raw decision: `{relative(json_path)}`",
        f"- Decision: `{decision['decision']['option_id']}`",
        f"- All gates passed: `{decision['all_gates_passed']}`",
        f"- Default enabled: `{decision['default_enabled']}`",
        f"- Allowed to enable default: `{decision['allowed_to_enable_default']}`",
        f"- Fresh frontend verification: `{decision['fresh_frontend_verification']['passed']}`",
        f"- Next version: `{decision['decision']['next_version']}`",
        f"- First design: `{decision['decision']['first_design']}`",
        "",
        "| Gate | Passed |",
        "| --- | --- |",
    ]
    for gate, passed in decision["gates"].items():
        lines.append(f"| `{gate}` | `{passed}` |")
    lines.extend(["", "| Option | Score | Disposition |", "| --- | ---: | --- |"])
    for option in decision["options"]:
        lines.append(f"| `{option['option_id']}` | {option['score']} | {option['disposition']} |")
    lines.extend(["", "## Conclusion", "", decision["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    decision = decide_default_enablement_review()
    json_path, summary_path = write_outputs(decision)
    print(json_path)
    print(summary_path)
    print(decision["decision"]["option_id"])


if __name__ == "__main__":
    main()
