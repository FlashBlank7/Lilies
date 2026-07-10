#!/usr/bin/env python3
"""Generate v0.2.101 E08 post-runbook disposition evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.101_e08_post_runbook_disposition"


@dataclass(frozen=True)
class DispositionCandidate:
    candidate_id: str
    status: str
    product_value: int
    readiness: int
    verification_readiness: int
    scope_risk: int
    false_completion_risk: int
    evidence: list[str]
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return (
            self.product_value
            + self.readiness
            + self.verification_readiness
            - self.scope_risk
            - self.false_completion_risk
        )


def candidates() -> list[DispositionCandidate]:
    return [
        DispositionCandidate(
            candidate_id="pause_e08_and_reselect_lane",
            status="candidate",
            product_value=35,
            readiness=45,
            verification_readiness=40,
            scope_risk=10,
            false_completion_risk=5,
            evidence=[
                "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
                "docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md",
                "docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md",
            ],
            disposition="select because the current E08 tranche has API, Studio, and runbook productization; full boundary closure should be a separate future lane",
            next_version="v0.2.102_productization_lane_reselection",
            first_design="docs/current-design/design_v0_2_102_productization_lane_reselection.md",
        ),
        DispositionCandidate(
            candidate_id="broader_sidecar_boundary_closure_now",
            status="candidate",
            product_value=50,
            readiness=10,
            verification_readiness=10,
            scope_risk=55,
            false_completion_risk=45,
            evidence=[
                "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            ],
            disposition="defer because it includes several independent hard-boundary areas and risks a false full-completion claim",
            next_version="v0.2.x_e08_broader_sidecar_boundary_closure",
            first_design="docs/current-design/design_e08_broader_sidecar_boundary_closure.md",
        ),
        DispositionCandidate(
            candidate_id="continue_e08_small_followup",
            status="candidate",
            product_value=20,
            readiness=25,
            verification_readiness=25,
            scope_risk=25,
            false_completion_risk=20,
            evidence=[
                "docs/stage-reports/v0.2.100_e08_operator_runbook_lifecycle.md",
            ],
            disposition="reject because small E08 followups would blur the current tranche boundary without addressing full closure",
            next_version="v0.2.x_e08_small_followup",
            first_design="docs/current-design/design_e08_small_followup.md",
        ),
    ]


def select_disposition(items: list[DispositionCandidate] | None = None) -> dict[str, Any]:
    items = items or candidates()
    ranked = sorted(items, key=lambda item: (item.score, item.candidate_id), reverse=True)
    winner = ranked[0]
    return {
        "version": "v0.2.101",
        "decision_id": "e08_post_runbook_disposition",
        "source_stage_report": "docs/stage-reports/v0.2.100_e08_operator_runbook_lifecycle.md",
        "status": "completed",
        "decision": "pause_e08_and_reselect_productization_lane",
        "selected_disposition": asdict(winner) | {"score": winner.score},
        "candidates": [asdict(item) | {"score": item.score} for item in ranked],
        "next_version": winner.next_version,
        "first_design": winner.first_design,
        "e08_current_tranche": {
            "status": "productized_without_full_sidecar_completion",
            "completed_slices": [
                "deterministic sidecar/passmode comparison",
                "editable policy-controls backend API",
                "Studio editable policy-controls",
                "operator runbook lifecycle",
            ],
            "remaining_boundary": "broader sidecar boundary closure remains deferred",
        },
        "e07_invariant": {
            "status": "preserved",
            "no_e07_code_or_default_change": True,
        },
        "reason": "E08 has a coherent productized tranche; the next move should reselect the highest-value remaining lane instead of forcing broad sidecar closure.",
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
    selected = result["selected_disposition"]
    lines = [
        "# v0.2.101 E08 post-runbook disposition",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected disposition: `{selected['candidate_id']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
        f"- E08 tranche status: `{result['e08_current_tranche']['status']}`",
        f"- Remaining boundary: {result['e08_current_tranche']['remaining_boundary']}",
        f"- E07 invariant: `{result['e07_invariant']['status']}`",
        f"- Reason: {result['reason']}",
        "",
        "## Ranked Candidates",
        "",
    ]
    for item in result["candidates"]:
        lines.append(
            f"- `{item['candidate_id']}` score `{item['score']}`; status `{item['status']}`; disposition: {item['disposition']}"
        )
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = select_disposition()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
