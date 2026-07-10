#!/usr/bin/env python3
"""Generate v0.2.94 productization lane reselection evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.94_productization_lane_reselection"


@dataclass(frozen=True)
class LaneCandidate:
    lane_id: str
    priority: str
    status: str
    product_gap: str
    blocked: bool
    readiness: int
    evidence: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        if self.blocked:
            return -100
        priority_score = {"P1": 40, "P2": 10}.get(self.priority, 0)
        return priority_score + self.readiness


def candidates() -> list[LaneCandidate]:
    return [
        LaneCandidate(
            lane_id="e08_followup_controls",
            priority="P1",
            status="deferred_but_unblocked",
            product_gap="sidecar/passmode follow-up controls for cancellation, budget, worker lease, and UI/API controls",
            blocked=False,
            readiness=35,
            evidence="docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            next_version="v0.2.95_e08_followup_controls_scope",
            first_design="docs/current-design/design_v0_2_95_e08_followup_controls_scope.md",
        ),
        LaneCandidate(
            lane_id="e05_scheduled_monitoring",
            priority="P1",
            status="completed_slice_optional_extension",
            product_gap="scheduled refresh hook is useful but E05 already has API/Studio monitoring surface",
            blocked=False,
            readiness=15,
            evidence="docs/experiment-status/ledgers/E05_template_reuse.md",
            next_version="v0.2.x_e05_scheduled_monitoring",
            first_design="docs/current-design/design_e05_scheduled_monitoring.md",
        ),
        LaneCandidate(
            lane_id="e02_true_human_panel",
            priority="P2",
            status="blocked",
            product_gap="true human timing requires an external human panel",
            blocked=True,
            readiness=0,
            evidence="docs/experiment-status/ledgers/E02_readable_testframe.md",
            next_version="v0.2.x_e02_human_panel",
            first_design="docs/current-design/design_e02_human_panel.md",
        ),
        LaneCandidate(
            lane_id="e10_governed_memory_surface",
            priority="P2",
            status="blocked",
            product_gap="governed memory scope requires accepted permission/audit/revoke/retention boundary",
            blocked=True,
            readiness=0,
            evidence="docs/experiment-status/ledgers/E10_assistant_memory_surface.md",
            next_version="v0.2.x_e10_governed_memory_surface",
            first_design="docs/current-design/design_e10_governed_memory_surface.md",
        ),
    ]


def select_lane(items: list[LaneCandidate] | None = None) -> dict[str, Any]:
    items = items or candidates()
    ranked = sorted(items, key=lambda item: (item.score, item.lane_id), reverse=True)
    winner = ranked[0]
    return {
        "version": "v0.2.94",
        "decision_id": "productization_lane_reselection",
        "source_stage_report": "docs/stage-reports/v0.2.93_complexity_router_guarded_default_rollout.md",
        "status": "completed",
        "e07_invariant": {
            "status": "guarded_default_rollout_implemented",
            "default_guarded_active": True,
            "rollback_disabled_available": True,
            "unknown_bypass": True,
            "evidence": "docs/workingon-archives/v0.2.93/rollout_v0.2.93_complexity_router_guarded_default_summary.md",
        },
        "candidates": [asdict(item) | {"score": item.score} for item in ranked],
        "selected_lane": asdict(winner) | {"score": winner.score},
        "decision": "select_e08_followup_controls",
        "next_version": winner.next_version,
        "first_design": winner.first_design,
        "reason": "E07 is productized; E08 is the highest-priority unblocked remaining productization gap",
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    selected = result["selected_lane"]
    lines = [
        "# v0.2.94 productization lane reselection",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected lane: `{selected['lane_id']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
        f"- Reason: {result['reason']}",
        f"- E07 invariant: `{result['e07_invariant']['status']}`",
        "",
        "## Ranked Candidates",
        "",
    ]
    for item in result["candidates"]:
        lines.append(
            f"- `{item['lane_id']}` score `{item['score']}`; blocked `{item['blocked']}`; status `{item['status']}`"
        )
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = select_lane()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
