#!/usr/bin/env python3
"""Generate v0.2.115 E08 remaining sidecar slice reselection evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "decision_v0.2.115_e08_remaining_sidecar_slice_reselection"


COMPLETED_SLICES = [
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
            slice_id="workflow_run_worker_offload_handler",
            label="Worker-owned workflow_run handler",
            sidecar_criticality=34,
            readiness=23,
            product_value=31,
            testability=25,
            scope_risk=23,
            existing_evidence="docs/stage-reports/v0.2.114_e08_scheduler_trigger_worker_offload_handler.md; platform/backend/src/agent_platform/workflow_runtime.py",
            next_version="v0.2.116_e08_workflow_run_worker_offload_handler",
            first_design="docs/current-design/design_v0_2_116_e08_workflow_run_worker_offload_handler.md",
        ),
        E08RemainingSlice(
            slice_id="test_suite_worker_offload_handler",
            label="Worker-owned test_suite handler",
            sidecar_criticality=25,
            readiness=21,
            product_value=23,
            testability=24,
            scope_risk=19,
            existing_evidence="platform/backend/src/agent_platform/workflow_runtime.py; platform/backend/src/agent_platform/workflow_storage.py",
            next_version="v0.2.116_e08_test_suite_worker_offload_handler",
            first_design="docs/current-design/design_v0_2_116_e08_test_suite_worker_offload_handler.md",
        ),
        E08RemainingSlice(
            slice_id="builder_build_worker_offload_handler",
            label="Worker-owned builder_build handler",
            sidecar_criticality=29,
            readiness=15,
            product_value=30,
            testability=18,
            scope_risk=32,
            existing_evidence="platform/backend/src/agent_platform/builder.py; docs/experiment-status/ledgers/builder_benchmark_foundation.md",
            next_version="v0.2.116_e08_builder_build_worker_offload_handler",
            first_design="docs/current-design/design_v0_2_116_e08_builder_build_worker_offload_handler.md",
        ),
        E08RemainingSlice(
            slice_id="draft_patch_preview_worker_offload_handler",
            label="Worker-owned draft_patch_preview handler",
            sidecar_criticality=18,
            readiness=22,
            product_value=18,
            testability=22,
            scope_risk=15,
            existing_evidence="platform/backend/src/agent_platform/draft_patch_preview.py",
            next_version="v0.2.116_e08_draft_patch_preview_worker_offload_handler",
            first_design="docs/current-design/design_v0_2_116_e08_draft_patch_preview_worker_offload_handler.md",
        ),
        E08RemainingSlice(
            slice_id="benchmark_worker_offload_handler",
            label="Worker-owned benchmark handler",
            sidecar_criticality=20,
            readiness=17,
            product_value=22,
            testability=19,
            scope_risk=21,
            existing_evidence="platform/backend/src/agent_platform/builder_benchmark.py",
            next_version="v0.2.116_e08_benchmark_worker_offload_handler",
            first_design="docs/current-design/design_v0_2_116_e08_benchmark_worker_offload_handler.md",
        ),
        E08RemainingSlice(
            slice_id="production_worker_supervision",
            label="Production worker supervision",
            sidecar_criticality=31,
            readiness=13,
            product_value=29,
            testability=15,
            scope_risk=33,
            existing_evidence="docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md; docs/stage-reports/v0.2.114_e08_scheduler_trigger_worker_offload_handler.md",
            next_version="v0.2.116_e08_production_worker_supervision",
            first_design="docs/current-design/design_v0_2_116_e08_production_worker_supervision.md",
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
            next_version="v0.2.116_e08_distributed_queue_semantics",
            first_design="docs/current-design/design_v0_2_116_e08_distributed_queue_semantics.md",
        ),
    ]


def select_slice(items: list[E08RemainingSlice] | None = None) -> dict[str, Any]:
    items = items or candidates()
    completed_ids = {item["slice_id"] for item in COMPLETED_SLICES}
    open_items = [item for item in items if item.slice_id not in completed_ids]
    ranked = sorted(open_items, key=lambda item: (item.score, item.slice_id), reverse=True)
    selected = ranked[0]
    return {
        "version": "v0.2.115",
        "decision_id": "e08_remaining_sidecar_slice_reselection_after_scheduler_trigger_offload",
        "source_stage_report": "docs/stage-reports/v0.2.114_e08_scheduler_trigger_worker_offload_handler.md",
        "status": "completed",
        "completed_slices": COMPLETED_SLICES,
        "remaining_candidates": [item.to_json() for item in ranked],
        "selected_slice": selected.to_json(),
        "decision": f"select_{selected.slice_id}",
        "next_version": selected.next_version,
        "first_design": selected.first_design,
        "invariants": {
            "completed_scheduler_trigger_excluded": "scheduler_trigger_worker_offload_handler" in completed_ids
            and selected.slice_id != "scheduler_trigger_worker_offload_handler",
            "completed_heartbeat_registry_excluded": "distributed_heartbeat_registry" in completed_ids
            and selected.slice_id != "distributed_heartbeat_registry",
            "completed_handler_catalog_excluded": "complete_handler_catalog" in completed_ids
            and selected.slice_id != "complete_handler_catalog",
            "e08_full_sidecar_completion_claimed": False,
            "workingon_is_not_task_source": True,
        },
        "reason": (
            "v0.2.114 closed automatic scheduler offload. The next highest-value concrete sidecar slice is a "
            "worker-owned workflow_run handler because workflow execution is the core runtime path behind broader "
            "worker ownership, has existing runtime/API semantics to reuse, and is more central than benchmark, "
            "draft preview, or process-supervision work before more real handlers exist."
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
        "# v0.2.115 E08 remaining sidecar slice reselection",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Selected slice: `{selected['slice_id']}`",
        f"- Next version: `{result['next_version']}`",
        f"- First design: `{result['first_design']}`",
        f"- Completed scheduler_trigger excluded: `{result['invariants']['completed_scheduler_trigger_excluded']}`",
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
