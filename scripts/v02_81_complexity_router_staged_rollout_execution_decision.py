#!/usr/bin/env python3
"""Decide the staged rollout execution path for the complexity router."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
ROLLOUT_PLAN_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.80" / "rollout_v0.2.80_complexity_router_staged_preparation.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.81_complexity_router_staged_rollout_execution"


@dataclass(frozen=True)
class RolloutExecutionOption:
    option_id: str
    label: str
    safety: int
    readiness: int
    evidence_value: int
    behavior_change_risk: int
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return self.safety + self.readiness + self.evidence_value - self.behavior_change_risk

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "score": self.score,
            "safety": self.safety,
            "readiness": self.readiness,
            "evidence_value": self.evidence_value,
            "behavior_change_risk": self.behavior_change_risk,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


OPTIONS = [
    RolloutExecutionOption(
        option_id="execute_shadow_only_rollout",
        label="Execute stage_0 shadow-only rollout",
        safety=5,
        readiness=5,
        evidence_value=5,
        behavior_change_risk=0,
        disposition="selected",
        next_version="v0.2.82_complexity_router_shadow_only_rollout",
        first_design="docs/current-design/design_complexity_router_shadow_only_rollout.md",
    ),
    RolloutExecutionOption(
        option_id="prepare_more_rollout_docs",
        label="Prepare more rollout docs before execution",
        safety=4,
        readiness=3,
        evidence_value=2,
        behavior_change_risk=0,
        disposition="rejected because v0.2.80 already defines stages, controls, and rollback criteria",
        next_version="v0.2.x_complexity_router_more_rollout_docs",
        first_design="docs/current-design/design_complexity_router_more_rollout_docs.md",
    ),
    RolloutExecutionOption(
        option_id="defer_rollout_execution",
        label="Defer rollout execution",
        safety=5,
        readiness=2,
        evidence_value=1,
        behavior_change_risk=0,
        disposition="rejected because shadow-only execution has no behavior-change risk and produces evidence",
        next_version="v0.2.x_complexity_router_rollout_deferred",
        first_design="docs/current-design/design_complexity_router_rollout_deferred.md",
    ),
]


def load_rollout_plan() -> dict[str, Any]:
    return json.loads(ROLLOUT_PLAN_PATH.read_text(encoding="utf-8"))


def decide_rollout_execution() -> dict[str, Any]:
    plan = load_rollout_plan()
    safety = complexity_router_default_safety_gate()
    ranked = sorted(OPTIONS, key=lambda option: (-option.score, option.option_id))
    decision = ranked[0]
    return {
        "version": "v0.2.81",
        "source_rollout_plan": ROLLOUT_PLAN_PATH.relative_to(ROOT).as_posix(),
        "decision": decision.to_json(),
        "options": [option.to_json() for option in ranked],
        "stage_count": len(plan["rollout_stages"]),
        "first_stage": plan["rollout_stages"][0],
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "conclusion": "Execute the shadow-only rollout next. Defaults remain disabled.",
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
        "# v0.2.81 complexity-router staged rollout execution decision",
        "",
        f"- Raw decision: `{relative(json_path)}`",
        f"- Decision: `{decision['decision']['option_id']}`",
        f"- First stage: `{decision['first_stage']['stage_id']}`",
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
    decision = decide_rollout_execution()
    json_path, summary_path = write_outputs(decision)
    print(json_path)
    print(summary_path)
    print(decision["decision"]["option_id"])


if __name__ == "__main__":
    main()
