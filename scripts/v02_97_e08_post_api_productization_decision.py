#!/usr/bin/env python3
"""Generate v0.2.97 E08 post-API productization decision evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.97_e08_post_api_productization"


@dataclass(frozen=True)
class ProductizationPath:
    path_id: str
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


def candidate_paths() -> list[ProductizationPath]:
    return [
        ProductizationPath(
            path_id="studio_editable_policy_controls",
            status="candidate",
            product_value=45,
            readiness=35,
            verification_readiness=30,
            scope_risk=20,
            false_completion_risk=10,
            evidence=[
                "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
                "platform/frontend/app/applications/[id]/page.tsx",
                "platform/frontend/lib/platform.ts",
                "docs/stage-reports/v0.2.86_frontend_verification_environment_repair.md",
            ],
            disposition="select as next product slice because backend PATCH contract exists and operator surface is now valuable",
            next_version="v0.2.98_e08_studio_editable_policy_controls",
            first_design="docs/current-design/design_v0_2_98_e08_studio_editable_policy_controls.md",
        ),
        ProductizationPath(
            path_id="operator_runbook_lifecycle",
            status="candidate",
            product_value=30,
            readiness=25,
            verification_readiness=20,
            scope_risk=15,
            false_completion_risk=15,
            evidence=[
                "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
            ],
            disposition="defer until Studio or equivalent operator surface exists",
            next_version="v0.2.x_e08_operator_runbook_lifecycle",
            first_design="docs/current-design/design_e08_operator_runbook_lifecycle.md",
        ),
        ProductizationPath(
            path_id="broader_sidecar_boundary_closure",
            status="candidate",
            product_value=50,
            readiness=10,
            verification_readiness=10,
            scope_risk=45,
            false_completion_risk=35,
            evidence=[
                "docs/experiment-status/ledgers/E08_harness_sidecar_passmode.md",
                "docs/stage-reports/v0.2.96_e08_editable_policy_controls_api.md",
            ],
            disposition="defer because it is too broad for the immediate post-API slice",
            next_version="v0.2.x_e08_broader_sidecar_boundary_closure",
            first_design="docs/current-design/design_e08_broader_sidecar_boundary_closure.md",
        ),
        ProductizationPath(
            path_id="pause_e08_after_api",
            status="candidate",
            product_value=5,
            readiness=40,
            verification_readiness=40,
            scope_risk=5,
            false_completion_risk=25,
            evidence=[
                "docs/stage-reports/v0.2.96_e08_editable_policy_controls_api.md",
            ],
            disposition="reject for now because the API is useful but not yet operator-accessible",
            next_version="v0.2.x_productization_lane_reselection",
            first_design="docs/current-design/design_productization_lane_reselection.md",
        ),
    ]


def select_path(items: list[ProductizationPath] | None = None) -> dict[str, Any]:
    items = items or candidate_paths()
    ranked = sorted(items, key=lambda item: (item.score, item.path_id), reverse=True)
    winner = ranked[0]
    return {
        "version": "v0.2.97",
        "decision_id": "e08_post_api_productization_decision",
        "source_stage_report": "docs/stage-reports/v0.2.96_e08_editable_policy_controls_api.md",
        "status": "completed",
        "decision": "select_studio_editable_policy_controls",
        "selected_path": asdict(winner) | {"score": winner.score},
        "candidates": [asdict(item) | {"score": item.score} for item in ranked],
        "next_version": winner.next_version,
        "first_design": winner.first_design,
        "e07_invariant": {
            "status": "preserved",
            "no_e07_code_or_default_change": True,
        },
        "v02_98_verification_targets": [
            "frontend type contract for policy-controls PATCH request/response",
            "Studio operator form or control surface for editable policy controls",
            "browser or frontend executable verification",
            "backend policy-controls API regression reuse",
            "E07 guarded default no-change assertion",
        ],
        "reason": "Backend editable policy-controls API exists; the next product value is exposing it safely to operators.",
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
    selected = result["selected_path"]
    lines = [
        "# v0.2.97 E08 post-API productization decision",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected path: `{selected['path_id']}`",
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
            f"- `{item['path_id']}` score `{item['score']}`; status `{item['status']}`; disposition: {item['disposition']}"
        )
    lines.extend(["", "## v0.2.98 Verification Targets", ""])
    for target in result["v02_98_verification_targets"]:
        lines.append(f"- {target}")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = select_path()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
