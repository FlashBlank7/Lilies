#!/usr/bin/env python3
"""Generate v0.2.95 E08 follow-up controls scope evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "scope_v0.2.95_e08_followup_controls"


@dataclass(frozen=True)
class ControlSlice:
    slice_id: str
    title: str
    status: str
    product_gap: str
    already_closed: bool
    blocked: bool
    readiness: int
    product_value: int
    risk: int
    evidence: list[str]
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        if self.blocked:
            return -100
        if self.already_closed:
            return -50
        return self.readiness + self.product_value - self.risk


def candidate_slices() -> list[ControlSlice]:
    return [
        ControlSlice(
            slice_id="editable_policy_controls_api",
            title="Editable policy-controls API",
            status="selected_candidate",
            product_gap=(
                "read-only policy-controls exists, but operators cannot safely mutate cancellation, budget, "
                "worker lease, or network policy settings through an audited backend contract"
            ),
            already_closed=False,
            blocked=False,
            readiness=35,
            product_value=45,
            risk=15,
            evidence=[
                "docs/stage-reports/v0.2.65_e08_policy_controls_surface.md",
                "docs/stage-reports/v0.2.66_e08_control_behavior_matrix.md",
                "platform/backend/src/agent_platform/platform_harness.py",
                "tests/test_workflow.py",
            ],
            next_version="v0.2.96_e08_editable_policy_controls_api",
            first_design="docs/current-design/design_v0_2_96_e08_editable_policy_controls_api.md",
        ),
        ControlSlice(
            slice_id="cancellation_budget_behavior_repeat",
            title="Cancellation and budget behavior evidence",
            status="already_closed_slice",
            product_gap="behavior evidence exists; repeating it would not advance product controls",
            already_closed=True,
            blocked=False,
            readiness=30,
            product_value=10,
            risk=5,
            evidence=[
                "docs/stage-reports/v0.2.68_e08_cancellation_budget_behavior.md",
                "tests/test_v02_68_e08_cancellation_budget_behavior.py",
            ],
            next_version="v0.2.x_e08_cancellation_budget_repeat",
            first_design="docs/current-design/design_e08_cancellation_budget_repeat.md",
        ),
        ControlSlice(
            slice_id="worker_lease_behavior_repeat",
            title="Worker lease backend behavior",
            status="already_closed_slice",
            product_gap="lease claim, renew, release, reconciliation, runner, and heartbeat evidence already exist",
            already_closed=True,
            blocked=False,
            readiness=30,
            product_value=10,
            risk=5,
            evidence=[
                "docs/stage-reports/v0.2.20_platform_harness_worker_lease.md",
                "docs/stage-reports/v0.2.28_worker_heartbeat_and_renewal.md",
                "tests/test_workflow.py",
            ],
            next_version="v0.2.x_e08_worker_lease_repeat",
            first_design="docs/current-design/design_e08_worker_lease_repeat.md",
        ),
        ControlSlice(
            slice_id="studio_editable_controls",
            title="Studio editable controls UI",
            status="defer_until_backend_contract",
            product_gap="operator UI needs an audited backend mutation contract first",
            already_closed=False,
            blocked=False,
            readiness=15,
            product_value=35,
            risk=35,
            evidence=[
                "platform/frontend/app/applications/[id]/page.tsx",
                "platform/frontend/lib/platform.ts",
            ],
            next_version="v0.2.x_e08_studio_editable_controls",
            first_design="docs/current-design/design_e08_studio_editable_controls.md",
        ),
        ControlSlice(
            slice_id="full_sidecar_completion_claim",
            title="Full sidecar completion claim",
            status="blocked_by_scope",
            product_gap="full sidecar completion still requires broader boundary closure and long-running operations evidence",
            already_closed=False,
            blocked=True,
            readiness=0,
            product_value=50,
            risk=80,
            evidence=[
                "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
                "docs/stage-reports/v0.2.69_e08_continuation_decision.md",
            ],
            next_version="v0.2.x_e08_full_sidecar_completion",
            first_design="docs/current-design/design_e08_full_sidecar_completion.md",
        ),
    ]


def select_scope(items: list[ControlSlice] | None = None) -> dict[str, Any]:
    items = items or candidate_slices()
    ranked = sorted(items, key=lambda item: (item.score, item.slice_id), reverse=True)
    winner = ranked[0]
    return {
        "version": "v0.2.95",
        "decision_id": "e08_followup_controls_scope",
        "source_stage_report": "docs/stage-reports/v0.2.94_productization_lane_reselection.md",
        "status": "completed",
        "selected_slice": asdict(winner) | {"score": winner.score},
        "candidates": [asdict(item) | {"score": item.score} for item in ranked],
        "decision": "select_editable_policy_controls_api",
        "next_version": winner.next_version,
        "first_design": winner.first_design,
        "e07_invariant": {
            "status": "preserved",
            "no_e07_code_or_default_change": True,
            "evidence": "docs/workingon-archives/v0.2.93/rollout_v0.2.93_complexity_router_guarded_default_summary.md",
        },
        "v02_96_verification_targets": [
            "backend API mutation tests for editable policy-controls",
            "invalid or unsafe policy change rejection tests",
            "before/after policy-controls evidence artifact",
            "E07 guarded default no-change assertion",
        ],
        "reason": (
            "Existing E08 read-only, matrix, cancellation/budget, and worker lease evidence should not be repeated; "
            "the next product gap is an audited backend mutation contract for policy controls."
        ),
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
    selected = result["selected_slice"]
    lines = [
        "# v0.2.95 E08 follow-up controls scope",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected slice: `{selected['slice_id']}`",
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
            f"- `{item['slice_id']}` score `{item['score']}`; "
            f"already_closed `{item['already_closed']}`; blocked `{item['blocked']}`; status `{item['status']}`"
        )
    lines.extend([
        "",
        "## v0.2.96 Verification Targets",
        "",
    ])
    for target in result["v02_96_verification_targets"]:
        lines.append(f"- {target}")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = select_scope()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
