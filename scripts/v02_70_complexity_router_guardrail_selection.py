#!/usr/bin/env python3
"""Select the first complexity-router guardrail scope before default enablement."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "selection_v0.2.70_complexity_router_guardrail"


@dataclass(frozen=True)
class GuardrailCandidate:
    candidate_id: str
    label: str
    default_safety_value: int
    verification_feasibility: int
    product_value: int
    source_readiness: int
    dependency_risk: int
    source: str
    disposition: str
    next_version: str | None
    first_design: str | None

    @property
    def score(self) -> int:
        return (
            self.default_safety_value
            + self.verification_feasibility
            + self.product_value
            + self.source_readiness
            - self.dependency_risk
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "label": self.label,
            "score": self.score,
            "default_safety_value": self.default_safety_value,
            "verification_feasibility": self.verification_feasibility,
            "product_value": self.product_value,
            "source_readiness": self.source_readiness,
            "dependency_risk": self.dependency_risk,
            "source": self.source,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


CANDIDATES = [
    GuardrailCandidate(
        candidate_id="default_safety_gate",
        label="Default-safety gate",
        default_safety_value=5,
        verification_feasibility=5,
        product_value=5,
        source_readiness=5,
        dependency_risk=1,
        source="docs/experiment-status/ledgers/E07_complexity_router.md",
        disposition="selected",
        next_version="v0.2.71_complexity_router_default_safety_gate",
        first_design="docs/current-design/design_complexity_router_default_safety_gate.md",
    ),
    GuardrailCandidate(
        candidate_id="requirement_classification_contract",
        label="Requirement classification contract",
        default_safety_value=4,
        verification_feasibility=4,
        product_value=4,
        source_readiness=4,
        dependency_risk=2,
        source="docs/experiment-status/ledgers/E07_complexity_router.md",
        disposition="deferred as supporting input after default-safety gate contract",
        next_version="v0.2.x_complexity_router_requirement_classification",
        first_design="docs/current-design/design_complexity_router_requirement_classification.md",
    ),
    GuardrailCandidate(
        candidate_id="override_controls",
        label="Operator override controls",
        default_safety_value=4,
        verification_feasibility=3,
        product_value=5,
        source_readiness=3,
        dependency_risk=4,
        source="docs/experiment-status/ledgers/E07_complexity_router.md",
        disposition="deferred because editable/operator controls should follow the default-safety contract",
        next_version="v0.2.x_complexity_router_override_controls",
        first_design="docs/current-design/design_complexity_router_override_controls.md",
    ),
    GuardrailCandidate(
        candidate_id="rollout_metrics",
        label="Rollout metrics",
        default_safety_value=3,
        verification_feasibility=4,
        product_value=4,
        source_readiness=3,
        dependency_risk=2,
        source="docs/experiment-status/ledgers/E07_complexity_router.md",
        disposition="deferred until the default-safety and classification contracts define measurable states",
        next_version="v0.2.x_complexity_router_rollout_metrics",
        first_design="docs/current-design/design_complexity_router_rollout_metrics.md",
    ),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_guardrail() -> dict[str, Any]:
    ranked = sorted(CANDIDATES, key=lambda item: (-item.score, item.candidate_id))
    winner = ranked[0]
    return {
        "version": "v0.2.70",
        "generated_at": utc_now(),
        "winner": winner.to_json(),
        "candidates": [candidate.to_json() for candidate in ranked],
        "router_ready_for_default": False,
        "formula": "default_safety_value + verification_feasibility + product_value + source_readiness - dependency_risk",
        "conclusion": (
            "Select the default-safety gate first. E07 remains not ready for default routing; classification, "
            "override controls, and rollout metrics are deferred supporting guardrails."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(selection: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.70 complexity-router guardrail selection",
        "",
        f"- Raw selection: `{relative(json_path)}`",
        f"- Winner: `{selection['winner']['candidate_id']}`",
        f"- Router ready for default: `{selection['router_ready_for_default']}`",
        f"- Next version: `{selection['winner']['next_version']}`",
        f"- First design: `{selection['winner']['first_design']}`",
        "",
        "| Candidate | Score | Disposition |",
        "| --- | ---: | --- |",
    ]
    for candidate in selection["candidates"]:
        lines.append(f"| `{candidate['candidate_id']}` | {candidate['score']} | {candidate['disposition']} |")
    lines.extend(["", "## Conclusion", "", selection["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    selection = select_guardrail()
    json_path, summary_path = write_outputs(selection, args.output_dir)
    print(json_path)
    print(summary_path)
    print(selection["winner"]["candidate_id"])


if __name__ == "__main__":
    main()
