from __future__ import annotations

import pytest

from scripts.v02_131_e08_remaining_sidecar_architecture_reselection import (
    ARCHITECTURE_SLICE_IDS,
    COMPLETED_SLICES,
    E08RemainingArchitectureSlice,
    select_slice,
)


def test_v02_131_selects_external_kms_provider_integration() -> None:
    result = select_slice()

    assert result["decision"] == "select_external_kms_provider_integration"
    assert result["selected_slice"]["slice_id"] == "external_kms_provider_integration"
    assert result["next_version"] == "v0.2.132_e08_external_kms_provider_integration"
    assert result["first_design"] == "docs/current-design/design_v0_2_132_e08_external_kms_provider_integration.md"


def test_v02_131_excludes_completed_architecture_and_worker_coverage() -> None:
    result = select_slice()
    completed_ids = {item["slice_id"] for item in COMPLETED_SLICES}
    candidate_ids = {item["slice_id"] for item in result["remaining_candidates"]}

    completed_worker_ids = {
        "builder_build_worker_offload_handler",
        "benchmark_worker_offload_handler",
        "draft_patch_preview_worker_offload_handler",
        "test_suite_worker_offload_handler",
        "workflow_run_worker_offload_handler",
        "scheduler_trigger_worker_offload_handler",
    }

    assert "external_process_manager" in completed_ids
    assert "distributed_queue_semantics" in completed_ids
    assert "production_worker_supervision" in completed_ids
    assert "external_process_manager" not in candidate_ids
    assert "distributed_queue_semantics" not in candidate_ids
    assert "production_worker_supervision" not in candidate_ids
    assert completed_worker_ids.issubset(completed_ids)
    assert candidate_ids.isdisjoint(completed_worker_ids)
    assert result["selected_slice"]["slice_id"] not in completed_ids
    assert result["invariants"]["completed_external_process_manager_excluded"] is True
    assert result["invariants"]["completed_distributed_queue_excluded"] is True
    assert result["invariants"]["completed_production_supervision_excluded"] is True
    assert result["invariants"]["required_worker_task_kind_execution_coverage_preserved"] is True


def test_v02_131_preserves_external_kms_and_full_sidecar_boundary() -> None:
    result = select_slice()
    candidate_ids = [item["slice_id"] for item in result["remaining_candidates"]]

    assert set(candidate_ids).issubset(ARCHITECTURE_SLICE_IDS)
    assert candidate_ids == ["external_kms_provider_integration"]
    assert result["invariants"]["remaining_candidates_are_architecture_only"] is True
    assert result["invariants"]["external_kms_provider_integration_selected"] is True
    assert result["invariants"]["external_kms_provider_integration_implemented"] is False
    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert result["invariants"]["workingon_is_not_task_source"] is True
    assert result["invariants"]["stage_report_is_next_task_source"] is True


def test_v02_131_fails_if_all_candidates_are_completed() -> None:
    completed_candidate = E08RemainingArchitectureSlice(
        slice_id="external_process_manager",
        label="Completed external process manager",
        sidecar_criticality=1,
        readiness=1,
        product_value=1,
        testability=1,
        scope_risk=1,
        existing_evidence="docs/workingon-archives/v0.2.130/evidence_v0.2.130_e08_external_process_manager_summary.md",
        next_version="never",
        first_design="never",
    )

    with pytest.raises(ValueError, match="no remaining E08 architecture slice candidates"):
        select_slice([completed_candidate])
