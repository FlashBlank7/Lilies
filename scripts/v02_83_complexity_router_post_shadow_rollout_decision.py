#!/usr/bin/env python3
"""Decide the post-shadow rollout path for the complexity router."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
ROLLOUT_PLAN_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.80" / "rollout_v0.2.80_complexity_router_staged_preparation.json"
SHADOW_EVIDENCE_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.82" / "rollout_v0.2.82_complexity_router_shadow_only.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.83_complexity_router_post_shadow_rollout"


@dataclass(frozen=True)
class PostShadowOption:
    option_id: str
    label: str
    safety: int
    productization_value: int
    evidence_readiness: int
    behavior_change_risk: int
    blocker_penalty: int
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return (
            self.safety
            + self.productization_value
            + self.evidence_readiness
            - self.behavior_change_risk
            - self.blocker_penalty
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "score": self.score,
            "safety": self.safety,
            "productization_value": self.productization_value,
            "evidence_readiness": self.evidence_readiness,
            "behavior_change_risk": self.behavior_change_risk,
            "blocker_penalty": self.blocker_penalty,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


OPTIONS = [
    PostShadowOption(
        option_id="execute_operator_opt_in_rollout",
        label="Execute stage_1 operator opt-in rollout",
        safety=5,
        productization_value=5,
        evidence_readiness=5,
        behavior_change_risk=0,
        blocker_penalty=0,
        disposition="selected",
        next_version="v0.2.84_complexity_router_operator_opt_in_rollout",
        first_design="docs/current-design/design_complexity_router_operator_opt_in_rollout.md",
    ),
    PostShadowOption(
        option_id="continue_shadow_only_observation",
        label="Continue shadow-only observation",
        safety=5,
        productization_value=2,
        evidence_readiness=4,
        behavior_change_risk=0,
        blocker_penalty=0,
        disposition="rejected because stage_0 exit criteria are already satisfied and productization needs operator opt-in evidence",
        next_version="v0.2.x_complexity_router_continued_shadow_observation",
        first_design="docs/current-design/design_complexity_router_continued_shadow_observation.md",
    ),
    PostShadowOption(
        option_id="begin_default_enablement_review",
        label="Begin default enablement review",
        safety=2,
        productization_value=5,
        evidence_readiness=2,
        behavior_change_risk=3,
        blocker_penalty=2,
        disposition="rejected because operator opt-in evidence and frontend verification are still missing",
        next_version="v0.2.x_complexity_router_default_enablement_review",
        first_design="docs/current-design/design_complexity_router_default_enablement_review.md",
    ),
]


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def rollout_stage(plan: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for stage in plan["rollout_stages"]:
        if stage["stage_id"] == stage_id:
            return stage
    raise ValueError(f"{stage_id} not found")


def decide_post_shadow_rollout() -> dict[str, Any]:
    rollout_plan = load_json(ROLLOUT_PLAN_PATH)
    shadow = load_json(SHADOW_EVIDENCE_PATH)
    stage_0 = rollout_stage(rollout_plan, "stage_0_shadow_only")
    stage_1 = rollout_stage(rollout_plan, "stage_1_operator_opt_in")
    safety = complexity_router_default_safety_gate()
    stage_0_passed = bool(shadow["pass_fail"]["passed"]) and shadow["default_enabled"] is False
    ranked = sorted(OPTIONS, key=lambda option: (-option.score, option.option_id))
    decision = ranked[0]
    return {
        "version": "v0.2.83",
        "decision_id": "complexity_router_post_shadow_rollout_decision",
        "source_stage_report": "docs/stage-reports/v0.2.82_complexity_router_shadow_only_rollout.md",
        "source_rollout_plan": ROLLOUT_PLAN_PATH.relative_to(ROOT).as_posix(),
        "source_shadow_evidence": SHADOW_EVIDENCE_PATH.relative_to(ROOT).as_posix(),
        "stage_0": stage_0,
        "stage_1": stage_1,
        "stage_0_passed": stage_0_passed,
        "shadow_metrics": shadow["metrics"],
        "decision": decision.to_json(),
        "options": [option.to_json() for option in ranked],
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "frontend_verification_required_before_default_review": True,
        "conclusion": "Execute operator opt-in rollout next. Defaults remain disabled.",
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
        "# v0.2.83 complexity-router post-shadow rollout decision",
        "",
        f"- Raw decision: `{relative(json_path)}`",
        f"- Decision: `{decision['decision']['option_id']}`",
        f"- Stage 0 passed: `{decision['stage_0_passed']}`",
        f"- Next stage: `{decision['stage_1']['stage_id']}`",
        f"- Next stage behavior change: `{decision['stage_1']['behavior_change']}`",
        f"- Default enabled: `{decision['default_enabled']}`",
        f"- Allowed to enable default: `{decision['allowed_to_enable_default']}`",
        f"- Next version: `{decision['decision']['next_version']}`",
        f"- First design: `{decision['decision']['first_design']}`",
        "",
        "| Option | Score | Disposition |",
        "| --- | ---: | --- |",
    ]
    for option in decision["options"]:
        lines.append(f"| `{option['option_id']}` | {option['score']} | {option['disposition']} |")
    lines.extend(["", "## Conclusion", "", decision["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    decision = decide_post_shadow_rollout()
    json_path, summary_path = write_outputs(decision)
    print(json_path)
    print(summary_path)
    print(decision["decision"]["option_id"])


if __name__ == "__main__":
    main()
