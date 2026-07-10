#!/usr/bin/env python3
"""Generate v0.2.129 E08 remaining sidecar architecture reselection evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.129_e08_remaining_sidecar_architecture_reselection"


COMPLETED_SLICES = [
    {
        "slice_id": "distributed_queue_semantics",
        "evidence": "docs/workingon-archives/v0.2.128/evidence_v0.2.128_e08_distributed_queue_semantics_summary.md",
    },
    {
        "slice_id": "production_worker_supervision",
        "evidence": "docs/workingon-archives/v0.2.126/evidence_v0.2.126_e08_production_worker_supervision_summary.md",
    },
    {
        "slice_id": "builder_build_worker_offload_handler",
        "evidence": "docs/workingon-archives/v0.2.124/evidence_v0.2.124_e08_builder_build_worker_offload_handler_summary.md",
    },
    {
        "slice_id": "benchmark_worker_offload_handler",
        "evidence": "docs/workingon-archives/v0.2.122/evidence_v0.2.122_e08_benchmark_worker_offload_handler_summary.md",
    },
    {
        "slice_id": "draft_patch_preview_worker_offload_handler",
        "evidence": "docs/workingon-archives/v0.2.120/evidence_v0.2.120_e08_draft_patch_preview_worker_offload_handler_summary.md",
    },
    {
        "slice_id": "test_suite_worker_offload_handler",
        "evidence": "docs/workingon-archives/v0.2.118/evidence_v0.2.118_e08_test_suite_worker_offload_handler_summary.md",
    },
    {
        "slice_id": "workflow_run_worker_offload_handler",
        "evidence": "docs/workingon-archives/v0.2.116/evidence_v0.2.116_e08_workflow_run_worker_offload_handler_summary.md",
    },
    {
        "slice_id": "scheduler_trigger_worker_offload_handler",
        "evidence": "docs/workingon-archives/v0.2.114/evidence_v0.2.114_e08_scheduler_trigger_worker_offload_handler_summary.md",
    },
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

ARCHITECTURE_SLICE_IDS = {
    "external_process_manager",
    "external_kms_provider_integration",
}


@dataclass(frozen=True)
class E08RemainingArchitectureSlice:
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


def candidates() -> list[E08RemainingArchitectureSlice]:
    return [
        E08RemainingArchitectureSlice(
            slice_id="external_process_manager",
            label="External worker process manager",
            sidecar_criticality=36,
            readiness=20,
            product_value=32,
            testability=20,
            scope_risk=34,
            existing_evidence="docs/stage-reports/v0.2.126_e08_production_worker_supervision.md; docs/stage-reports/v0.2.128_e08_distributed_queue_semantics.md; platform/backend/src/agent_platform/worker_runner.py",
            next_version="v0.2.130_e08_external_process_manager",
            first_design="docs/current-design/design_v0_2_130_e08_external_process_manager.md",
        ),
        E08RemainingArchitectureSlice(
            slice_id="external_kms_provider_integration",
            label="External KMS provider integration",
            sidecar_criticality=28,
            readiness=14,
            product_value=25,
            testability=16,
            scope_risk=31,
            existing_evidence="docs/stage-reports/v0.2.108_e08_secret_kms_rotation_contract.md; platform/backend/src/agent_platform/platform_harness.py",
            next_version="v0.2.130_e08_external_kms_provider_integration",
            first_design="docs/current-design/design_v0_2_130_e08_external_kms_provider_integration.md",
        ),
    ]


def select_slice(items: list[E08RemainingArchitectureSlice] | None = None) -> dict[str, Any]:
    items = items or candidates()
    completed_ids = {item["slice_id"] for item in COMPLETED_SLICES}
    open_items = [item for item in items if item.slice_id not in completed_ids]
    ranked = sorted(open_items, key=lambda item: (item.score, item.slice_id), reverse=True)
    selected = ranked[0]
    remaining_ids = {item.slice_id for item in ranked}
    completed_worker_ids = {
        "builder_build_worker_offload_handler",
        "benchmark_worker_offload_handler",
        "draft_patch_preview_worker_offload_handler",
        "test_suite_worker_offload_handler",
        "workflow_run_worker_offload_handler",
        "scheduler_trigger_worker_offload_handler",
    }
    return {
        "version": "v0.2.129",
        "decision_id": "e08_remaining_sidecar_architecture_reselection_after_distributed_queue_semantics",
        "source_stage_report": "docs/stage-reports/v0.2.128_e08_distributed_queue_semantics.md",
        "status": "completed",
        "completed_slices": COMPLETED_SLICES,
        "remaining_candidates": [item.to_json() for item in ranked],
        "selected_slice": selected.to_json(),
        "decision": f"select_{selected.slice_id}",
        "next_version": selected.next_version,
        "first_design": selected.first_design,
        "invariants": {
            "completed_distributed_queue_excluded": "distributed_queue_semantics" in completed_ids
            and selected.slice_id != "distributed_queue_semantics",
            "completed_production_supervision_excluded": "production_worker_supervision" in completed_ids
            and selected.slice_id != "production_worker_supervision",
            "required_worker_task_kind_execution_coverage_preserved": completed_worker_ids.issubset(completed_ids)
            and remaining_ids.isdisjoint(completed_worker_ids),
            "remaining_candidates_are_architecture_only": remaining_ids.issubset(ARCHITECTURE_SLICE_IDS),
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
            "stage_report_is_next_task_source": True,
        },
        "reason": (
            "v0.2.128 closed storage-backed distributed queue semantics. The next highest-value remaining "
            "architecture slice is an external worker process manager because supervision and queue ownership "
            "now exist inside the app, but process-level spawn/observe/stop/restart semantics are still missing. "
            "External KMS provider integration remains an important security slice but is less coupled to the "
            "current worker-sidecar execution path."
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
        "# v0.2.129 E08 remaining sidecar architecture reselection",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected slice: `{selected['slice_id']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
        f"- Completed distributed queue excluded: `{result['invariants']['completed_distributed_queue_excluded']}`",
        f"- Completed production supervision excluded: `{result['invariants']['completed_production_supervision_excluded']}`",
        f"- Worker task-kind execution coverage preserved: `{result['invariants']['required_worker_task_kind_execution_coverage_preserved']}`",
        f"- Remaining candidates are architecture-only: `{result['invariants']['remaining_candidates_are_architecture_only']}`",
        f"- E08 full sidecar completion claimed: `{result['invariants']['e08_full_sidecar_completion_claimed']}`",
        f"- Reason: {result['reason']}",
        "",
        "## Completed Slices",
        "",
    ]
    for item in result["completed_slices"]:
        lines.append(f"- `{item['slice_id']}` via `{item['evidence']}`")
    lines.extend(["", "## Remaining Architecture Candidates", "", "| Slice | Score | Evidence |", "| --- | ---: | --- |"])
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
