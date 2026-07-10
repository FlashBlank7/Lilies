#!/usr/bin/env python3
"""Generate v0.2.113 E08 remaining sidecar slice reselection evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.113_e08_remaining_sidecar_slice_reselection"


COMPLETED_SLICES = [
    {
        "slice_id": "distributed_heartbeat_registry",
        "evidence": "docs/workingon-archives/v0.2.112/evidence_v0.2.112_e08_distributed_heartbeat_registry_summary.md",
    },
    {
        "slice_id": "complete_handler_catalog",
        "evidence": "docs/workingon-archives/v0.2.110/evidence_v0.2.110_e08_complete_handler_catalog_summary.md",
    },
    {
        "slice_id": "stdio_container_egress_allowlist_contract",
        "evidence": "docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md",
    },
    {
        "slice_id": "secret_kms_rotation_contract",
        "evidence": "docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md",
    },
    {
        "slice_id": "editable_policy_controls_api",
        "evidence": "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
    },
    {
        "slice_id": "studio_editable_policy_controls",
        "evidence": "docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md",
    },
    {
        "slice_id": "operator_runbook_lifecycle",
        "evidence": "docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md",
    },
]


@dataclass(frozen=True)
class E08RemainingSlice:
    slice_id: str
    label: str
    sidecar_criticality: int
    readiness: int
    product_value: int
    testability: int
    scope_risk: int
    existing_evidence: str
    next_version: str
    first_design: str

    @property
    def score(self) -> int:
        return self.sidecar_criticality + self.readiness + self.product_value + self.testability - self.scope_risk

    def to_json(self) -> dict[str, Any]:
        return asdict(self) | {"score": self.score}


def candidates() -> list[E08RemainingSlice]:
    return [
        E08RemainingSlice(
            slice_id="scheduler_trigger_worker_offload_handler",
            label="Worker-owned scheduler_trigger handler",
            sidecar_criticality=29,
            readiness=24,
            product_value=27,
            testability=25,
            scope_risk=18,
            existing_evidence="docs/stage-reports/v0.2.110_e08_complete_handler_catalog.md; docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md",
            next_version="v0.2.114_e08_scheduler_trigger_worker_offload_handler",
            first_design="docs/current-design/design_v0_2_114_e08_scheduler_trigger_worker_offload_handler.md",
        ),
        E08RemainingSlice(
            slice_id="operational_alerting_for_sidecar_liveness",
            label="Operational alerting for sidecar liveness",
            sidecar_criticality=23,
            readiness=18,
            product_value=22,
            testability=19,
            scope_risk=19,
            existing_evidence="docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md",
            next_version="v0.2.114_e08_operational_alerting_for_sidecar_liveness",
            first_design="docs/current-design/design_v0_2_114_e08_operational_alerting_for_sidecar_liveness.md",
        ),
        E08RemainingSlice(
            slice_id="external_kms_provider_integration",
            label="External KMS provider integration",
            sidecar_criticality=24,
            readiness=11,
            product_value=24,
            testability=14,
            scope_risk=30,
            existing_evidence="docs/stage-reports/v0.2.108_e08_secret_kms_rotation_contract.md",
            next_version="v0.2.114_e08_external_kms_provider_integration",
            first_design="docs/current-design/design_v0_2_114_e08_external_kms_provider_integration.md",
        ),
        E08RemainingSlice(
            slice_id="distributed_queue_semantics",
            label="Distributed queue semantics",
            sidecar_criticality=32,
            readiness=12,
            product_value=28,
            testability=14,
            scope_risk=34,
            existing_evidence="docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md",
            next_version="v0.2.114_e08_distributed_queue_semantics",
            first_design="docs/current-design/design_v0_2_114_e08_distributed_queue_semantics.md",
        ),
    ]


def select_slice(items: list[E08RemainingSlice] | None = None) -> dict[str, Any]:
    items = items or candidates()
    completed_ids = {item["slice_id"] for item in COMPLETED_SLICES}
    open_items = [item for item in items if item.slice_id not in completed_ids]
    ranked = sorted(open_items, key=lambda item: (item.score, item.slice_id), reverse=True)
    selected = ranked[0]
    return {
        "version": "v0.2.113",
        "decision_id": "e08_remaining_sidecar_slice_reselection_after_heartbeat_registry",
        "source_stage_report": "docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md",
        "status": "completed",
        "completed_slices": COMPLETED_SLICES,
        "remaining_candidates": [item.to_json() for item in ranked],
        "selected_slice": selected.to_json(),
        "decision": f"select_{selected.slice_id}",
        "next_version": selected.next_version,
        "first_design": selected.first_design,
        "invariants": {
            "completed_heartbeat_registry_excluded": "distributed_heartbeat_registry" in completed_ids
            and selected.slice_id != "distributed_heartbeat_registry",
            "completed_handler_catalog_excluded": "complete_handler_catalog" in completed_ids
            and selected.slice_id != "complete_handler_catalog",
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
        },
        "reason": (
            "v0.2.112 closed durable worker heartbeat/liveness registry. The next highest-value concrete slice is "
            "a worker-owned scheduler_trigger handler because v0.2.110 exposed scheduler_trigger as an unavailable "
            "catalog entry and v0.2.112 now gives worker liveness enough product visibility for a real offload slice."
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
    selected = result["selected_slice"]
    lines = [
        "# v0.2.113 E08 remaining sidecar slice reselection",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected slice: `{selected['slice_id']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
        f"- Completed heartbeat registry excluded: `{result['invariants']['completed_heartbeat_registry_excluded']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Reason: {result['reason']}",
        "",
        "## Completed Slices",
        "",
    ]
    for item in result["completed_slices"]:
        lines.append(f"- `{item['slice_id']}` via `{item['evidence']}`")
    lines.extend(["", "## Remaining Candidates", "", "| Slice | Score | Evidence |", "| --- | ---: | --- |"])
    for item in result["remaining_candidates"]:
        lines.append(f"| `{item['slice_id']}` | {item['score']} | `{item['existing_evidence']}` |")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = select_slice()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
