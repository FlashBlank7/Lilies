#!/usr/bin/env python3
"""Generate v0.2.104 productization lane reselection evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.104_productization_lane_reselection"
CompletionState = Literal["open", "blocked", "completed_productized"]


@dataclass(frozen=True)
class LaneCandidate:
    lane_id: str
    priority: str
    completion_state: CompletionState
    status: str
    product_gap: str
    product_value: int
    readiness: int
    evidence_maturity: int
    next_slice_specificity: int
    scope_risk: int
    evidence: str
    next_version: str | None
    first_design: str | None

    @property
    def selectable(self) -> bool:
        return self.completion_state == "open"

    @property
    def score(self) -> int:
        if not self.selectable:
            return -1000
        priority_score = {"P1": 50, "P2": 20, "P3": 5}.get(self.priority, 0)
        return (
            priority_score
            + self.product_value
            + self.readiness
            + self.evidence_maturity
            + self.next_slice_specificity
            - self.scope_risk
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self) | {"selectable": self.selectable, "score": self.score}


def candidates() -> list[LaneCandidate]:
    return [
        LaneCandidate(
            lane_id="e08_broader_sidecar_scope_decomposition",
            priority="P1",
            completion_state="open",
            status="open_broad_boundary_needs_scope",
            product_gap="full Platform Harness sidecar completion remains incomplete beyond the current E08 tranche",
            product_value=36,
            readiness=18,
            evidence_maturity=28,
            next_slice_specificity=22,
            scope_risk=22,
            evidence="docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            next_version="v0.2.105_e08_broader_sidecar_scope_decomposition",
            first_design="docs/current-design/design_v0_2_105_e08_broader_sidecar_scope_decomposition.md",
        ),
        LaneCandidate(
            lane_id="e09_live_ui_usability_study",
            priority="P2",
            completion_state="open",
            status="optional_product_study",
            product_gap="natural-language editing has patch-scope fixture closure; live UI usability can be studied later",
            product_value=14,
            readiness=8,
            evidence_maturity=16,
            next_slice_specificity=8,
            scope_risk=12,
            evidence="docs/experiment-status/ledgers/E09_natural_language_editing.md",
            next_version="v0.2.x_e09_live_ui_usability_study",
            first_design="docs/current-design/design_e09_live_ui_usability_study.md",
        ),
        LaneCandidate(
            lane_id="e05_scheduled_monitoring_hook",
            priority="P1",
            completion_state="completed_productized",
            status="verified_existing_product_capability",
            product_gap="already implemented in v0.2.63 and verified in v0.2.103",
            product_value=30,
            readiness=35,
            evidence_maturity=35,
            next_slice_specificity=30,
            scope_risk=4,
            evidence="docs/experiment-status/ledgers/E05_template_reuse.md",
            next_version=None,
            first_design=None,
        ),
        LaneCandidate(
            lane_id="e07_continuous_monitoring",
            priority="P2",
            completion_state="completed_productized",
            status="guarded_default_rollout_implemented",
            product_gap="E07 guarded default rollout is already productized; only routine monitoring remains",
            product_value=12,
            readiness=24,
            evidence_maturity=30,
            next_slice_specificity=8,
            scope_risk=6,
            evidence="docs/experiment-status/ledgers/E07_complexity_router.md",
            next_version=None,
            first_design=None,
        ),
        LaneCandidate(
            lane_id="e02_true_human_panel",
            priority="P2",
            completion_state="blocked",
            status="blocked_external_panel",
            product_gap="true human timing requires recruited participants and timing protocol",
            product_value=26,
            readiness=2,
            evidence_maturity=18,
            next_slice_specificity=4,
            scope_risk=20,
            evidence="docs/experiment-status/ledgers/E02_readable_testframe.md",
            next_version=None,
            first_design=None,
        ),
        LaneCandidate(
            lane_id="e10_governed_memory_surface",
            priority="P2",
            completion_state="blocked",
            status="blocked_governance_boundary",
            product_gap="memory surface requires accepted permission/audit/revoke/retention/source boundary",
            product_value=30,
            readiness=2,
            evidence_maturity=12,
            next_slice_specificity=3,
            scope_risk=28,
            evidence="docs/experiment-status/ledgers/E10_assistant_memory_surface.md",
            next_version=None,
            first_design=None,
        ),
    ]


def select_lane(items: list[LaneCandidate] | None = None) -> dict[str, Any]:
    items = items or candidates()
    ranked = sorted(items, key=lambda item: (item.score, item.lane_id), reverse=True)
    winner = next(item for item in ranked if item.selectable)
    return {
        "version": "v0.2.104",
        "decision_id": "productization_lane_reselection",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.103_e05_scheduled_monitoring_hook.md",
        "status": "completed",
        "selection_rule": "only completion_state=open candidates can win; completed and blocked lanes stay visible but non-selectable",
        "candidates": [item.to_json() for item in ranked],
        "selected_lane": winner.to_json(),
        "decision": f"select_{winner.lane_id}",
        "next_version": winner.next_version,
        "first_design": winner.first_design,
        "invariants": {
            "completed_lanes_excluded": True,
            "blocked_lanes_excluded": True,
            "e05_scheduled_hook_productized": True,
            "e07_guarded_default_preserved": True,
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
        },
        "reason": (
            "E05 scheduled monitoring and E07 guarded rollout are already productized, while E02/E10 "
            "remain blocked. The highest-value open lane is E08 broader sidecar scope decomposition; "
            "the next stage must scope a concrete slice rather than claim full sidecar completion."
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
    selected = result["selected_lane"]
    lines = [
        "# v0.2.104 productization lane reselection",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected lane: `{selected['lane_id']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Reason: {result['reason']}",
        "",
        "## Ranked Candidates",
        "",
        "| Lane | Score | Completion state | Selectable | Status |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for item in result["candidates"]:
        lines.append(
            f"| `{item['lane_id']}` | {item['score']} | `{item['completion_state']}` | `{item['selectable']}` | `{item['status']}` |"
        )
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = select_lane()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
