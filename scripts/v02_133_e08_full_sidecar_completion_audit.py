#!/usr/bin/env python3
"""Generate v0.2.133 E08 full sidecar completion audit evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "audit_v0.2.133_e08_full_sidecar_completion"


@dataclass(frozen=True)
class RequiredSurface:
    surface_id: str
    label: str
    evidence: str
    required_for_completion: bool = True

    def to_json(self) -> dict[str, Any]:
        path = ROOT / self.evidence
        return asdict(self) | {"exists": path.exists()}


REQUIRED_SURFACES = [
    RequiredSurface(
        "sidecar_passmode_comparison",
        "Sidecar/passmode comparison",
        "docs/experiment-status/evidence/experiment_v0.2.55_e08_sidecar_passmode_2026_07_10_summary.md",
    ),
    RequiredSurface(
        "editable_policy_controls_api",
        "Editable policy-controls API",
        "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
    ),
    RequiredSurface(
        "studio_editable_policy_controls",
        "Studio editable policy controls",
        "docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md",
    ),
    RequiredSurface(
        "operator_runbook_lifecycle",
        "Operator runbook lifecycle",
        "docs/workingon-archives/v0.2.100/evidence_v0.2.100_e08_operator_runbook_lifecycle_summary.md",
    ),
    RequiredSurface(
        "stdio_container_egress_allowlist",
        "Stdio/container egress allowlist",
        "docs/workingon-archives/v0.2.106/evidence_v0.2.106_e08_stdio_container_egress_allowlist_contract_summary.md",
    ),
    RequiredSurface(
        "local_secret_rotation_envelope",
        "Local KMS/rotation-grade secret envelope",
        "docs/workingon-archives/v0.2.108/evidence_v0.2.108_e08_secret_kms_rotation_contract_summary.md",
    ),
    RequiredSurface(
        "complete_handler_catalog",
        "Complete handler catalog",
        "docs/workingon-archives/v0.2.110/evidence_v0.2.110_e08_complete_handler_catalog_summary.md",
    ),
    RequiredSurface(
        "durable_worker_heartbeat_registry",
        "Durable worker heartbeat registry",
        "docs/workingon-archives/v0.2.112/evidence_v0.2.112_e08_distributed_heartbeat_registry_summary.md",
    ),
    RequiredSurface(
        "scheduler_trigger_worker_offload",
        "Scheduler trigger worker offload",
        "docs/workingon-archives/v0.2.114/evidence_v0.2.114_e08_scheduler_trigger_worker_offload_handler_summary.md",
    ),
    RequiredSurface(
        "workflow_run_worker_offload",
        "Workflow run worker offload",
        "docs/workingon-archives/v0.2.116/evidence_v0.2.116_e08_workflow_run_worker_offload_handler_summary.md",
    ),
    RequiredSurface(
        "test_suite_worker_offload",
        "Test suite worker offload",
        "docs/workingon-archives/v0.2.118/evidence_v0.2.118_e08_test_suite_worker_offload_handler_summary.md",
    ),
    RequiredSurface(
        "draft_patch_preview_worker_offload",
        "Draft patch preview worker offload",
        "docs/workingon-archives/v0.2.120/evidence_v0.2.120_e08_draft_patch_preview_worker_offload_handler_summary.md",
    ),
    RequiredSurface(
        "benchmark_worker_offload",
        "Benchmark worker offload",
        "docs/workingon-archives/v0.2.122/evidence_v0.2.122_e08_benchmark_worker_offload_handler_summary.md",
    ),
    RequiredSurface(
        "builder_build_worker_offload",
        "Builder build worker offload",
        "docs/workingon-archives/v0.2.124/evidence_v0.2.124_e08_builder_build_worker_offload_handler_summary.md",
    ),
    RequiredSurface(
        "production_worker_supervision",
        "Production worker supervision",
        "docs/workingon-archives/v0.2.126/evidence_v0.2.126_e08_production_worker_supervision_summary.md",
    ),
    RequiredSurface(
        "distributed_queue_semantics",
        "Distributed queue semantics",
        "docs/workingon-archives/v0.2.128/evidence_v0.2.128_e08_distributed_queue_semantics_summary.md",
    ),
    RequiredSurface(
        "external_process_manager",
        "External worker process manager",
        "docs/workingon-archives/v0.2.130/evidence_v0.2.130_e08_external_process_manager_summary.md",
    ),
    RequiredSurface(
        "external_kms_provider_integration",
        "External KMS provider integration contract",
        "docs/workingon-archives/v0.2.132/evidence_v0.2.132_e08_external_kms_provider_integration_summary.md",
    ),
]


OPTIONAL_FOLLOWUPS = [
    {
        "followup_id": "cloud_specific_kms_clients",
        "reason": "v0.2.132 proves provider contract integration with a deterministic local provider, not AWS/GCP/Azure deployment.",
        "blocks_full_sidecar_completion": False,
    },
    {
        "followup_id": "production_observability_hardening",
        "reason": "Additional deployment dashboards can improve operations but do not invalidate completed sidecar control surfaces.",
        "blocks_full_sidecar_completion": False,
    },
]


def audit() -> dict[str, Any]:
    required = [surface.to_json() for surface in REQUIRED_SURFACES]
    missing_required = [
        surface["surface_id"]
        for surface in required
        if surface["required_for_completion"] and not surface["exists"]
    ]
    full_sidecar_completion_claimed = not missing_required
    return {
        "version": "v0.2.133",
        "audit_id": "e08_full_sidecar_completion_audit",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.132_e08_external_kms_provider_integration.md",
        "status": "completed" if full_sidecar_completion_claimed else "needs_attention",
        "required_surfaces": required,
        "required_surface_count": len(required),
        "missing_required_gaps": missing_required,
        "optional_followups": OPTIONAL_FOLLOWUPS,
        "decision": "claim_e08_full_sidecar_completion" if full_sidecar_completion_claimed else "select_remaining_gap",
        "boundaries": {
            "full_sidecar_completion_claimed": full_sidecar_completion_claimed,
            "cloud_provider_deployment_claimed": False,
            "cloud_provider_deployment_required_for_completion": False,
            "workingon_is_not_task_source": True,
            "stage_report_is_next_task_source": True,
        },
        "reason": (
            "All required E08 Platform Harness sidecar/passmode surfaces have versioned evidence. "
            "Cloud-specific KMS clients remain optional deployment follow-up because v0.2.132 closed "
            "the provider contract boundary without claiming cloud deployment."
        )
        if full_sidecar_completion_claimed
        else "At least one required E08 sidecar evidence surface is missing.",
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
    lines = [
        "# v0.2.133 E08 full sidecar completion audit",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Decision: `{result['decision']}`",
        f"- Required surface count: `{result['required_surface_count']}`",
        f"- Missing required gaps: `{len(result['missing_required_gaps'])}`",
        f"- Full sidecar completion claimed: `{result['boundaries']['full_sidecar_completion_claimed']}`",
        f"- Cloud provider deployment claimed: `{result['boundaries']['cloud_provider_deployment_claimed']}`",
        f"- Reason: {result['reason']}",
        "",
        "## Required Surfaces",
        "",
        "| Surface | Exists | Evidence |",
        "| --- | --- | --- |",
    ]
    for surface in result["required_surfaces"]:
        lines.append(f"| `{surface['surface_id']}` | `{surface['exists']}` | `{surface['evidence']}` |")
    lines.extend(["", "## Optional Followups", ""])
    for item in result["optional_followups"]:
        lines.append(
            f"- `{item['followup_id']}`: blocks full sidecar completion = `{item['blocks_full_sidecar_completion']}`"
        )
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = audit()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["decision"])


if __name__ == "__main__":
    main()
