#!/usr/bin/env python3
"""Re-select between preserved E08 and complexity-router productization lanes."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "selection_v0.2.64_productization_lane_reselection"


@dataclass(frozen=True)
class Lane:
    lane_id: str
    label: str
    evidence: int
    readiness: int
    product_value: int
    next_slice_specificity: int
    default_risk_blocker: int
    governance_blocker: int
    source: str
    source_detail: str
    conclusion: str
    next_version: str | None
    first_design: str | None

    @property
    def score(self) -> int:
        return (
            self.evidence
            + self.readiness
            + self.product_value
            + self.next_slice_specificity
            - self.default_risk_blocker
            - self.governance_blocker
        )

    @property
    def blocked(self) -> bool:
        return self.default_risk_blocker >= 4 or self.governance_blocker >= 4

    def to_json(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "label": self.label,
            "score": self.score,
            "blocked": self.blocked,
            "evidence": self.evidence,
            "readiness": self.readiness,
            "product_value": self.product_value,
            "next_slice_specificity": self.next_slice_specificity,
            "default_risk_blocker": self.default_risk_blocker,
            "governance_blocker": self.governance_blocker,
            "source": self.source,
            "source_detail": self.source_detail,
            "conclusion": self.conclusion,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


LANES = [
    Lane(
        lane_id="e08_extended_controls",
        label="E08 sidecar/passmode extended controls",
        evidence=4,
        readiness=4,
        product_value=5,
        next_slice_specificity=4,
        default_risk_blocker=0,
        governance_blocker=1,
        source="docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
        source_detail=(
            "v0.2.55 completed a deterministic comparison: workflow-internal passmode can pause/pass by "
            "configuration, while Platform Harness sidecar policy hard-fails before the external action."
        ),
        conclusion=(
            "Select next because the first comparison is closed, evidence is runnable, and a bounded controls "
            "surface can improve operator governance without enabling risky defaults."
        ),
        next_version="v0.2.65_e08_policy_controls_surface",
        first_design="docs/current-design/design_e08_policy_controls_surface.md",
    ),
    Lane(
        lane_id="complexity_router_rollout",
        label="E07 complexity router rollout",
        evidence=3,
        readiness=2,
        product_value=4,
        next_slice_specificity=2,
        default_risk_blocker=3,
        governance_blocker=2,
        source="docs/experiment-status/ledgers/E07_complexity_router.md",
        source_detail=(
            "v0.2.57 made simple/medium/complex routing hypotheses explicit, but the ledger states "
            "router_ready_for_default=false and requires guardrails plus rollout design."
        ),
        conclusion=(
            "Defer until a stage report selects guardrails, overrides, rollout metrics, and default-safety design."
        ),
        next_version="v0.2.x_complexity_router_guardrails",
        first_design="docs/current-design/design_complexity_router_guardrails.md",
    ),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ranked_lanes() -> list[Lane]:
    return sorted(LANES, key=lambda lane: (lane.blocked, -lane.score, lane.lane_id))


def select_lane() -> dict[str, Any]:
    ranked = ranked_lanes()
    winner = next(lane for lane in ranked if not lane.blocked)
    deferred = [lane for lane in ranked if lane.lane_id != winner.lane_id]
    return {
        "version": "v0.2.64",
        "status": "completed",
        "generated_at": utc_now(),
        "winner": winner.to_json(),
        "deferred": [lane.to_json() for lane in deferred],
        "lanes": [lane.to_json() for lane in ranked],
        "next_version": winner.next_version,
        "first_design": winner.first_design,
        "formula": (
            "evidence + readiness + product_value + next_slice_specificity "
            "- default_risk_blocker - governance_blocker"
        ),
        "conclusion": (
            "Select E08 extended controls as the next productization lane. Complexity-router remains useful, "
            "but its own ledger says it is not default-ready until guardrails and rollout design exist."
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
        "# v0.2.64 productization lane reselection",
        "",
        "## Summary",
        "",
        f"- Raw selection: `{relative(json_path)}`",
        f"- Winner: `{selection['winner']['lane_id']}`",
        f"- Next version: `{selection['next_version']}`",
        f"- First design: `{selection['first_design']}`",
        "",
        "## Scores",
        "",
        "| Lane | Score | Blocked | Source |",
        "| --- | ---: | --- | --- |",
    ]
    for lane in selection["lanes"]:
        lines.append(f"| {lane['lane_id']} | {lane['score']} | {lane['blocked']} | `{lane['source']}` |")
    lines.extend(["", "## Conclusion", "", selection["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    selection = select_lane()
    json_path, summary_path = write_outputs(selection, args.output_dir)
    print(json_path)
    print(summary_path)
    print(selection["winner"]["lane_id"])


if __name__ == "__main__":
    main()
