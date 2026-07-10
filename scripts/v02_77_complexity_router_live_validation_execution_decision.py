#!/usr/bin/env python3
"""Decide whether to execute the complexity-router live validation plan."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_platform.complexity_router import complexity_router_default_safety_gate


ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "docs" / "workingon-archives" / "v0.2.76" / "plan_v0.2.76_complexity_router_live_validation.json"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.77_complexity_router_live_validation_execution"


@dataclass(frozen=True)
class ExecutionOption:
    option_id: str
    label: str
    readiness: int
    safety: int
    product_value: int
    budget_risk: int
    default_change_risk: int
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return self.readiness + self.safety + self.product_value - self.budget_risk - self.default_change_risk

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "score": self.score,
            "readiness": self.readiness,
            "safety": self.safety,
            "product_value": self.product_value,
            "budget_risk": self.budget_risk,
            "default_change_risk": self.default_change_risk,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


OPTIONS = [
    ExecutionOption(
        option_id="execute_bounded_live_validation",
        label="Execute bounded live validation",
        readiness=5,
        safety=5,
        product_value=5,
        budget_risk=2,
        default_change_risk=0,
        disposition="selected",
        next_version="v0.2.78_complexity_router_bounded_live_validation",
        first_design="docs/current-design/design_complexity_router_bounded_live_validation.md",
    ),
    ExecutionOption(
        option_id="prepare_additional_dry_run",
        label="Prepare another dry-run before live validation",
        readiness=3,
        safety=4,
        product_value=2,
        budget_risk=0,
        default_change_risk=0,
        disposition="rejected because v0.2.76 already defines cases, metrics, budget, and pass/fail criteria",
        next_version="v0.2.x_complexity_router_additional_dry_run",
        first_design="docs/current-design/design_complexity_router_additional_dry_run.md",
    ),
    ExecutionOption(
        option_id="defer_live_validation",
        label="Defer live validation",
        readiness=2,
        safety=5,
        product_value=1,
        budget_risk=0,
        default_change_risk=0,
        disposition="rejected because it prevents evidence needed before any default review",
        next_version="v0.2.x_complexity_router_live_validation_deferred",
        first_design="docs/current-design/design_complexity_router_live_validation_deferred.md",
    ),
]


def load_plan() -> dict[str, Any]:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def decide_live_validation_execution() -> dict[str, Any]:
    plan = load_plan()
    safety = complexity_router_default_safety_gate()
    ranked = sorted(OPTIONS, key=lambda option: (-option.score, option.option_id))
    decision = ranked[0]
    return {
        "version": "v0.2.77",
        "source_plan": PLAN_PATH.relative_to(ROOT).as_posix(),
        "decision": decision.to_json(),
        "options": [option.to_json() for option in ranked],
        "plan_summary": {
            "execution_in_v0_2_76": plan["execution_in_this_stage"],
            "case_count": len(plan["validation_cases"]),
            "max_live_cases": plan["budget_boundary"]["max_live_cases"],
            "metrics_capture": plan["metrics_capture"],
        },
        "default_safety": safety,
        "default_enabled": safety["default_enabled"],
        "allowed_to_enable_default": safety["allowed_to_enable_default"],
        "conclusion": (
            "Select bounded live validation execution next. This decision does not execute live validation "
            "and does not enable complexity-router defaults."
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
        "# v0.2.77 complexity-router live validation execution decision",
        "",
        f"- Raw decision: `{relative(json_path)}`",
        f"- Decision: `{decision['decision']['option_id']}`",
        f"- Default enabled: `{decision['default_enabled']}`",
        f"- Allowed to enable default: `{decision['allowed_to_enable_default']}`",
        f"- Source plan: `{decision['source_plan']}`",
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
    decision = decide_live_validation_execution()
    json_path, summary_path = write_outputs(decision)
    print(json_path)
    print(summary_path)
    print(decision["decision"]["option_id"])


if __name__ == "__main__":
    main()
