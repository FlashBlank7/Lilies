#!/usr/bin/env python3
"""Select the next productization lane after v0.2 backlog closure."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "selection_v0.2.59_productization_lane"


@dataclass(frozen=True)
class Lane:
    lane_id: str
    label: str
    evidence: int
    readiness: int
    product_value: int
    next_slice_specificity: int
    external_blocker: int
    safety_governance_blocker: int
    source: str
    conclusion: str
    next_stage: str | None

    @property
    def score(self) -> int:
        return (
            self.evidence
            + self.readiness
            + self.product_value
            + self.next_slice_specificity
            - self.external_blocker
            - self.safety_governance_blocker
        )

    @property
    def blocked(self) -> bool:
        return self.external_blocker >= 4 or self.safety_governance_blocker >= 4

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
            "external_blocker": self.external_blocker,
            "safety_governance_blocker": self.safety_governance_blocker,
            "source": self.source,
            "conclusion": self.conclusion,
            "next_stage": self.next_stage,
        }


LANES = [
    Lane(
        lane_id="adaptive_monitoring_product_surface",
        label="E05 adaptive monitoring surface",
        evidence=5,
        readiness=5,
        product_value=5,
        next_slice_specificity=5,
        external_blocker=0,
        safety_governance_blocker=0,
        source="docs/experiment-status/ledgers/E05_template_reuse.md",
        conclusion=(
            "Default adaptive behavior, policy-default live reliability, and a zero-critical-alert monitoring "
            "snapshot already exist; the next product slice can expose this as Studio/API or scheduled drift checks."
        ),
        next_stage="v0.2.60_adaptive_monitoring_product_surface",
    ),
    Lane(
        lane_id="e08_extended_controls",
        label="E08 sidecar/passmode extended controls",
        evidence=4,
        readiness=3,
        product_value=5,
        next_slice_specificity=3,
        external_blocker=0,
        safety_governance_blocker=1,
        source="docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
        conclusion=(
            "First deterministic comparison is closed, but extending cancellation, budget, worker lease, and UI/API "
            "controls is broader than the smallest next product step."
        ),
        next_stage="v0.2.60_e08_extended_controls",
    ),
    Lane(
        lane_id="complexity_router_rollout",
        label="E07 complexity router rollout",
        evidence=3,
        readiness=2,
        product_value=4,
        next_slice_specificity=3,
        external_blocker=0,
        safety_governance_blocker=2,
        source="docs/experiment-status/ledgers/E07_complexity_router.md",
        conclusion=(
            "Router hypothesis exists, but the ledger explicitly says it is not ready for default without guardrails "
            "and rollout design."
        ),
        next_stage="v0.2.60_complexity_router_guardrails",
    ),
    Lane(
        lane_id="human_panel",
        label="E02 true human timing panel",
        evidence=2,
        readiness=1,
        product_value=3,
        next_slice_specificity=2,
        external_blocker=5,
        safety_governance_blocker=0,
        source="docs/experiment-status/ledgers/E02_readable_testframe.md",
        conclusion="True human timing remains externally blocked by reviewer recruitment and timing protocol needs.",
        next_stage=None,
    ),
    Lane(
        lane_id="governed_memory_surface",
        label="E10 governed memory surface",
        evidence=2,
        readiness=1,
        product_value=4,
        next_slice_specificity=2,
        external_blocker=0,
        safety_governance_blocker=5,
        source="docs/experiment-status/ledgers/E10_assistant_memory_surface.md",
        conclusion=(
            "Memory remains blocked until permission, audit, revoke, retention, and source boundaries are accepted."
        ),
        next_stage=None,
    ),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ranked_lanes() -> list[Lane]:
    return sorted(LANES, key=lambda lane: (lane.blocked, -lane.score, lane.lane_id))


def select_lane() -> dict[str, Any]:
    ranked = ranked_lanes()
    winner = next(lane for lane in ranked if not lane.blocked)
    return {
        "version": "v0.2.59",
        "status": "completed",
        "generated_at": utc_now(),
        "winner": winner.to_json(),
        "next_version": winner.next_stage,
        "first_workingon": "docs/workingon/work_v0.2.60_adaptive_monitoring_product_surface.md",
        "lanes": [lane.to_json() for lane in ranked],
        "formula": "evidence + readiness + product_value + next_slice_specificity - external_blocker - safety_governance_blocker",
        "conclusion": (
            "Select E05 adaptive monitoring product surface as the next productization lane because it has the "
            "strongest evidence/readiness combination and no external or governance blocker."
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
        "# v0.2.59 productization lane selection",
        "",
        "## Summary",
        "",
        f"- Raw selection: `{relative(json_path)}`",
        f"- Winner: `{selection['winner']['lane_id']}`",
        f"- Next version: `{selection['next_version']}`",
        f"- First workingon: `{selection['first_workingon']}`",
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
