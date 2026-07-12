#!/usr/bin/env python3
"""Decide whether to continue E08 productization or move to a preserved lane."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.69_e08_continuation"


@dataclass(frozen=True)
class ContinuationOption:
    option_id: str
    label: str
    product_value: int
    evidence_saturation: int
    verification_feasibility: int
    dependency_risk: int
    novelty: int
    source: str
    disposition: str
    next_version: str | None
    first_design: str | None

    @property
    def score(self) -> int:
        return (
            self.product_value
            + self.evidence_saturation
            + self.verification_feasibility
            + self.novelty
            - self.dependency_risk
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "option_id": self.option_id,
            "label": self.label,
            "score": self.score,
            "product_value": self.product_value,
            "evidence_saturation": self.evidence_saturation,
            "verification_feasibility": self.verification_feasibility,
            "dependency_risk": self.dependency_risk,
            "novelty": self.novelty,
            "source": self.source,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


OPTIONS = [
    ContinuationOption(
        option_id="pause_e08_move_complexity_router",
        label="Pause E08 tranche and move to complexity-router guardrails",
        product_value=5,
        evidence_saturation=5,
        verification_feasibility=4,
        dependency_risk=1,
        novelty=5,
        source="docs/stage-report-archives/v0.2.x/v0.2.68_e08_cancellation_budget_behavior.md",
        disposition="selected",
        next_version="v0.2.70_complexity_router_guardrail_selection",
        first_design="docs/current-design/design_complexity_router_guardrail_selection.md",
    ),
    ContinuationOption(
        option_id="continue_e08_editable_policy_controls",
        label="Continue E08 with editable policy controls",
        product_value=5,
        evidence_saturation=2,
        verification_feasibility=2,
        dependency_risk=5,
        novelty=3,
        source="docs/stage-report-archives/v0.2.x/v0.2.68_e08_cancellation_budget_behavior.md",
        disposition="deferred because Node/frontend verification is still blocked and read-only evidence is sufficient for now",
        next_version="v0.2.x_e08_editable_policy_controls",
        first_design="docs/current-design/design_e08_editable_policy_controls.md",
    ),
    ContinuationOption(
        option_id="continue_e08_operator_runbook",
        label="Continue E08 with operator runbook lifecycle",
        product_value=3,
        evidence_saturation=3,
        verification_feasibility=4,
        dependency_risk=2,
        novelty=2,
        source="docs/stage-report-archives/v0.2.x/v0.2.68_e08_cancellation_budget_behavior.md",
        disposition="deferred because it is lower value after current backend evidence and before editable controls",
        next_version="v0.2.x_e08_operator_runbook",
        first_design="docs/current-design/design_e08_operator_runbook.md",
    ),
    ContinuationOption(
        option_id="declare_full_sidecar_complete",
        label="Declare full sidecar complete",
        product_value=1,
        evidence_saturation=0,
        verification_feasibility=1,
        dependency_risk=5,
        novelty=0,
        source="docs/stage-report-archives/v0.2.x/v0.2.68_e08_cancellation_budget_behavior.md",
        disposition="rejected because v0.2.65-v0.2.68 explicitly do not close full sidecar completion",
        next_version=None,
        first_design=None,
    ),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decide() -> dict[str, Any]:
    ranked = sorted(OPTIONS, key=lambda item: (-item.score, item.option_id))
    winner = ranked[0]
    return {
        "version": "v0.2.69",
        "generated_at": utc_now(),
        "decision": winner.to_json(),
        "options": [option.to_json() for option in ranked],
        "formula": "product_value + evidence_saturation + verification_feasibility + novelty - dependency_risk",
        "conclusion": (
            "Pause the current E08 productization tranche after surface, matrix, and cancellation/budget evidence. "
            "Move next to complexity-router guardrail selection while keeping E08 editable controls and runbook deferred."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(decision: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(decision, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.69 E08 continuation decision",
        "",
        f"- Raw decision: `{relative(json_path)}`",
        f"- Decision: `{decision['decision']['option_id']}`",
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
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    decision = decide()
    json_path, summary_path = write_outputs(decision, args.output_dir)
    print(json_path)
    print(summary_path)
    print(decision["decision"]["option_id"])


if __name__ == "__main__":
    main()
