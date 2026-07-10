#!/usr/bin/env python3
"""Select the next E08 full-boundary gap slice after the behavior matrix."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "selection_v0.2.67_e08_gap"


@dataclass(frozen=True)
class GapSlice:
    slice_id: str
    label: str
    verification_feasibility: int
    product_value: int
    closure_specificity: int
    dependency_risk: int
    source: str
    disposition: str
    next_version: str | None
    first_design: str | None

    @property
    def score(self) -> int:
        return self.verification_feasibility + self.product_value + self.closure_specificity - self.dependency_risk

    def to_json(self) -> dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "label": self.label,
            "score": self.score,
            "verification_feasibility": self.verification_feasibility,
            "product_value": self.product_value,
            "closure_specificity": self.closure_specificity,
            "dependency_risk": self.dependency_risk,
            "source": self.source,
            "disposition": self.disposition,
            "next_version": self.next_version,
            "first_design": self.first_design,
        }


SLICES = [
    GapSlice(
        slice_id="cancellation_budget_live_behavior",
        label="Cancellation and budget live behavior evidence",
        verification_feasibility=5,
        product_value=5,
        closure_specificity=5,
        dependency_risk=1,
        source="docs/stage-reports/v0.2.66_e08_control_behavior_matrix.md",
        disposition="selected",
        next_version="v0.2.68_e08_cancellation_budget_behavior",
        first_design="docs/current-design/design_e08_cancellation_budget_behavior.md",
    ),
    GapSlice(
        slice_id="editable_policy_controls",
        label="Editable policy controls",
        verification_feasibility=3,
        product_value=5,
        closure_specificity=3,
        dependency_risk=4,
        source="docs/stage-reports/v0.2.65_e08_policy_controls_surface.md",
        disposition="deferred until read-only behavior evidence is stronger",
        next_version="v0.2.x_e08_editable_policy_controls",
        first_design="docs/current-design/design_e08_editable_policy_controls.md",
    ),
    GapSlice(
        slice_id="operator_runbook_lifecycle",
        label="Operator runbook and lifecycle",
        verification_feasibility=4,
        product_value=3,
        closure_specificity=3,
        dependency_risk=2,
        source="docs/stage-reports/v0.2.66_e08_control_behavior_matrix.md",
        disposition="deferred until next behavior slice is implemented",
        next_version="v0.2.x_e08_operator_runbook",
        first_design="docs/current-design/design_e08_operator_runbook.md",
    ),
    GapSlice(
        slice_id="stop_e08_productization",
        label="Stop E08 productization",
        verification_feasibility=5,
        product_value=1,
        closure_specificity=4,
        dependency_risk=0,
        source="docs/stage-reports/v0.2.66_e08_control_behavior_matrix.md",
        disposition="rejected because E08 still has high-value backend-verifiable slices",
        next_version=None,
        first_design=None,
    ),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_gap() -> dict[str, Any]:
    ranked = sorted(SLICES, key=lambda item: (-item.score, item.slice_id))
    winner = ranked[0]
    return {
        "version": "v0.2.67",
        "generated_at": utc_now(),
        "winner": winner.to_json(),
        "slices": [item.to_json() for item in ranked],
        "formula": "verification_feasibility + product_value + closure_specificity - dependency_risk",
        "conclusion": (
            "Select cancellation/budget live behavior because it is backend-verifiable now, carries high E08 "
            "operator value, and can close a concrete gap without Node-dependent frontend verification."
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
        "# v0.2.67 E08 gap selection",
        "",
        f"- Raw selection: `{relative(json_path)}`",
        f"- Winner: `{selection['winner']['slice_id']}`",
        f"- Next version: `{selection['winner']['next_version']}`",
        f"- First design: `{selection['winner']['first_design']}`",
        "",
        "| Slice | Score | Disposition |",
        "| --- | ---: | --- |",
    ]
    for item in selection["slices"]:
        lines.append(f"| `{item['slice_id']}` | {item['score']} | {item['disposition']} |")
    lines.extend(["", "## Conclusion", "", selection["conclusion"], ""])
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    selection = select_gap()
    json_path, summary_path = write_outputs(selection, args.output_dir)
    print(json_path)
    print(summary_path)
    print(selection["winner"]["slice_id"])


if __name__ == "__main__":
    main()
