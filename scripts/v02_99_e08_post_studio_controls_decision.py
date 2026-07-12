#!/usr/bin/env python3
"""Generate v0.2.99 E08 post-Studio controls decision evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.99_e08_post_studio_controls"


@dataclass(frozen=True)
class PostStudioCandidate:
    candidate_id: str
    status: str
    product_value: int
    readiness: int
    verification_readiness: int
    scope_risk: int
    evidence: list[str]
    disposition: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return self.product_value + self.readiness + self.verification_readiness - self.scope_risk


def candidates() -> list[PostStudioCandidate]:
    return [
        PostStudioCandidate(
            candidate_id="operator_runbook_lifecycle",
            status="candidate",
            product_value=40,
            readiness=35,
            verification_readiness=35,
            scope_risk=15,
            evidence=[
                "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
                "docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md",
            ],
            disposition="select because backend API and Studio surface now need operational procedure, rollback, and escalation guidance",
            next_version="v0.2.100_e08_operator_runbook_lifecycle",
            first_design="docs/current-design/design_v0_2_100_e08_operator_runbook_lifecycle.md",
        ),
        PostStudioCandidate(
            candidate_id="broader_sidecar_boundary_closure",
            status="candidate",
            product_value=50,
            readiness=15,
            verification_readiness=15,
            scope_risk=45,
            evidence=[
                "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
            ],
            disposition="defer because full boundary closure remains broader than the immediate post-Studio slice",
            next_version="v0.2.x_e08_broader_sidecar_boundary_closure",
            first_design="docs/current-design/design_e08_broader_sidecar_boundary_closure.md",
        ),
        PostStudioCandidate(
            candidate_id="pause_e08_after_studio_controls",
            status="candidate",
            product_value=10,
            readiness=40,
            verification_readiness=40,
            scope_risk=5,
            evidence=[
                "docs/stage-report-archives/v0.2.x/v0.2.98_e08_studio_editable_policy_controls.md",
            ],
            disposition="reject for now because operator runbook is the natural closure after an operator surface",
            next_version="v0.2.x_productization_lane_reselection",
            first_design="docs/current-design/design_productization_lane_reselection.md",
        ),
    ]


def select_candidate(items: list[PostStudioCandidate] | None = None) -> dict[str, Any]:
    items = items or candidates()
    ranked = sorted(items, key=lambda item: (item.score, item.candidate_id), reverse=True)
    winner = ranked[0]
    return {
        "version": "v0.2.99",
        "decision_id": "e08_post_studio_controls_decision",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.98_e08_studio_editable_policy_controls.md",
        "status": "completed",
        "decision": "select_operator_runbook_lifecycle",
        "selected_candidate": asdict(winner) | {"score": winner.score},
        "candidates": [asdict(item) | {"score": item.score} for item in ranked],
        "next_version": winner.next_version,
        "first_design": winner.first_design,
        "e07_invariant": {
            "status": "preserved",
            "no_e07_code_or_default_change": True,
        },
        "v02_100_verification_targets": [
            "operator runbook document or product surface under docs/current-design then archived",
            "runbook checklist covering before-change, apply-change, rollback, and incident escalation",
            "linkage to backend PATCH API and Studio editable controls evidence",
            "stage report template validation and active directory cleanup",
        ],
        "reason": "After API and Studio controls, the next highest-value bounded step is an operator runbook lifecycle.",
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
    selected = result["selected_candidate"]
    lines = [
        "# v0.2.99 E08 post-Studio controls decision",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected candidate: `{selected['candidate_id']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
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
    lines.extend(["", "## v0.2.100 Verification Targets", ""])
    for target in result["v02_100_verification_targets"]:
        lines.append(f"- {target}")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = select_candidate()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
