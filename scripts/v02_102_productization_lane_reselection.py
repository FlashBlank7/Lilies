#!/usr/bin/env python3
"""Generate v0.2.102 productization lane reselection evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.102_productization_lane_reselection"


@dataclass(frozen=True)
class LaneCandidate:
    lane_id: str
    priority: str
    status: str
    product_gap: str
    blocked: bool
    blocker_reason: str
    product_value: int
    readiness: int
    evidence_maturity: int
    next_slice_specificity: int
    scope_risk: int
    evidence: str
    next_version: str | None
    first_design: str | None

    @property
    def score(self) -> int:
        if self.blocked:
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
        return asdict(self) | {"score": self.score}


def candidates() -> list[LaneCandidate]:
    return [
        LaneCandidate(
            lane_id="e05_scheduled_monitoring_hook",
            priority="P1",
            status="unblocked_product_extension",
            product_gap=(
                "adaptive reuse policy already has API/Studio/manual refresh/history; "
                "scheduled drift checks remain the next concrete monitoring product slice"
            ),
            blocked=False,
            blocker_reason="none",
            product_value=32,
            readiness=30,
            evidence_maturity=30,
            next_slice_specificity=28,
            scope_risk=8,
            evidence="docs/experiment-status/ledgers/E05_template_reuse.md",
            next_version="v0.2.103_e05_scheduled_monitoring_hook",
            first_design="docs/current-design/design_v0_2_103_e05_scheduled_monitoring_hook.md",
        ),
        LaneCandidate(
            lane_id="e08_broader_sidecar_boundary_closure",
            priority="P1",
            status="deferred_broad_boundary",
            product_gap=(
                "current E08 tranche is productized, but full sidecar completion remains broad "
                "and should not be claimed from API/Studio/runbook slices"
            ),
            blocked=False,
            blocker_reason="scope too broad for the next focused productization slice",
            product_value=35,
            readiness=14,
            evidence_maturity=22,
            next_slice_specificity=6,
            scope_risk=35,
            evidence="docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            next_version="v0.2.x_e08_broader_sidecar_boundary_closure",
            first_design="docs/current-design/design_e08_broader_sidecar_boundary_closure.md",
        ),
        LaneCandidate(
            lane_id="e07_continuous_monitoring",
            priority="P2",
            status="completed_product_surface_monitoring_only",
            product_gap="guarded default rollout is already implemented; only monitoring and rollback watch remain",
            blocked=False,
            blocker_reason="none",
            product_value=12,
            readiness=20,
            evidence_maturity=28,
            next_slice_specificity=10,
            scope_risk=8,
            evidence="docs/experiment-status/ledgers/E07_complexity_router.md",
            next_version="v0.2.x_e07_continuous_monitoring",
            first_design="docs/current-design/design_e07_continuous_monitoring.md",
        ),
        LaneCandidate(
            lane_id="e02_true_human_panel",
            priority="P2",
            status="blocked_external_panel",
            product_gap="true human timing requires recruited participants and timing protocol",
            blocked=True,
            blocker_reason="external human panel is not available in the current local evolution loop",
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
            status="blocked_governance_boundary",
            product_gap="memory surface requires accepted permission/audit/revoke/retention/source boundary",
            blocked=True,
            blocker_reason="governed memory product scope is not accepted yet",
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
    winner = next(item for item in ranked if not item.blocked)
    blocked = [item for item in ranked if item.blocked]
    return {
        "version": "v0.2.102",
        "decision_id": "productization_lane_reselection",
        "source_stage_report": "docs/stage-reports/v0.2.101_e08_post_runbook_disposition.md",
        "status": "completed",
        "selection_rule": (
            "choose the highest-scoring unblocked lane; blocked lanes are retained in evidence "
            "but cannot win; broad E08 sidecar closure is penalized for scope risk"
        ),
        "candidates": [item.to_json() for item in ranked],
        "blocked_lanes": [item.to_json() for item in blocked],
        "selected_lane": winner.to_json(),
        "decision": f"select_{winner.lane_id}",
        "next_version": winner.next_version,
        "first_design": winner.first_design,
        "invariants": {
            "task_source": "stage_report_next_stage_task_set",
            "workingon_is_not_task_source": True,
            "e07_guarded_default_preserved": True,
            "e08_current_tranche_productized": True,
            "e08_full_sidecar_completion_claimed": False,
            "e02_true_human_panel_blocked": True,
            "e10_governed_memory_blocked": True,
        },
        "reason": (
            "E05 scheduled monitoring is the highest-value unblocked concrete product slice after "
            "E08 current tranche pause; E08 full sidecar remains a future broad boundary, while "
            "E02 and E10 stay blocked by external/governance prerequisites."
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
        "# v0.2.102 productization lane reselection",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected lane: `{selected['lane_id']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
        f"- Reason: {result['reason']}",
        f"- Task source: `{result['invariants']['task_source']}`",
        f"- Workingon is not task source: `{result['invariants']['workingon_is_not_task_source']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        "",
        "## Ranked Candidates",
        "",
        "| Lane | Score | Blocked | Status | Evidence |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for item in result["candidates"]:
        lines.append(
            f"| `{item['lane_id']}` | {item['score']} | `{item['blocked']}` | `{item['status']}` | `{item['evidence']}` |"
        )
    lines.extend(
        [
            "",
            "## Blocked / Deferred Boundaries",
            "",
            "- E02 true human panel remains blocked by external panel availability.",
            "- E10 governed memory remains blocked by governance boundary acceptance.",
            "- E08 broader sidecar boundary remains deferred and is not full-sidecar-complete.",
            "",
        ]
    )
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
