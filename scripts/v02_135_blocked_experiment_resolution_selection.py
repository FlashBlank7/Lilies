#!/usr/bin/env python3
"""Generate v0.2.135 blocked experiment resolution selection evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.135_blocked_experiment_resolution_selection"


@dataclass(frozen=True)
class BlockedResolutionCandidate:
    experiment_id: str
    blocker: str
    resolution_path: str
    can_progress_without_external_state: bool
    risk: int
    product_value: int
    next_version: str
    first_design: str
    evidence: str

    @property
    def score(self) -> int:
        progress_score = 50 if self.can_progress_without_external_state else 0
        return progress_score + self.product_value - self.risk

    def to_json(self) -> dict[str, Any]:
        return asdict(self) | {"score": self.score}


def candidates() -> list[BlockedResolutionCandidate]:
    return [
        BlockedResolutionCandidate(
            experiment_id="E02",
            blocker="blocked_external_panel",
            resolution_path="recruited_human_timing_panel",
            can_progress_without_external_state=False,
            risk=35,
            product_value=20,
            next_version="blocked_until_external_panel_exists",
            first_design="none",
            evidence="docs/experiment-status/ledgers/E02_readable_testframe.md",
        ),
        BlockedResolutionCandidate(
            experiment_id="E10",
            blocker="blocked_governance_boundary",
            resolution_path="governed_memory_boundary_definition",
            can_progress_without_external_state=True,
            risk=25,
            product_value=35,
            next_version="v0.2.136_e10_governed_memory_boundary_definition",
            first_design="docs/current-design/design_v0_2_136_e10_governed_memory_boundary_definition.md",
            evidence="docs/experiment-status/ledgers/E10_assistant_memory_surface.md",
        ),
    ]


def select_resolution(items: list[BlockedResolutionCandidate] | None = None) -> dict[str, Any]:
    items = items or candidates()
    ranked = sorted(items, key=lambda item: (item.score, item.experiment_id), reverse=True)
    selected = ranked[0]
    return {
        "version": "v0.2.135",
        "decision_id": "blocked_experiment_resolution_selection_after_global_audit",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.134_global_experiment_productization_completion_audit.md",
        "status": "completed",
        "candidates": [item.to_json() for item in ranked],
        "selected": selected.to_json(),
        "decision": f"select_{selected.experiment_id.lower()}_{selected.resolution_path}",
        "next_version": selected.next_version,
        "first_design": selected.first_design,
        "invariants": {
            "e02_true_human_panel_remains_external_blocker": True,
            "e02_automated_substitute_claimed": False,
            "e10_governance_boundary_selected": selected.experiment_id == "E10",
            "global_completion_claimed": False,
            "workingon_is_not_task_source": True,
            "stage_report_is_next_task_source": True,
        },
        "reason": (
            "E02 requires external recruited participants and cannot progress through automation. "
            "E10 can progress by defining the governed memory product boundary that the ledger requires."
        ),
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.135 Blocked experiment resolution selection",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
        f"- E02 true human panel remains external blocker: `{result['invariants']['e02_true_human_panel_remains_external_blocker']}`",
        f"- E02 automated substitute claimed: `{result['invariants']['e02_automated_substitute_claimed']}`",
        f"- E10 governance boundary selected: `{result['invariants']['e10_governance_boundary_selected']}`",
        f"- Global completion claimed: `{result['invariants']['global_completion_claimed']}`",
        f"- Reason: {result['reason']}",
        "",
        "## Candidates",
        "",
        "| Experiment | Resolution | Score | External state needed |",
        "| --- | --- | ---: | --- |",
    ]
    for item in result["candidates"]:
        lines.append(
            f"| `{item['experiment_id']}` | `{item['resolution_path']}` | {item['score']} | `{not item['can_progress_without_external_state']}` |"
        )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = select_resolution()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
