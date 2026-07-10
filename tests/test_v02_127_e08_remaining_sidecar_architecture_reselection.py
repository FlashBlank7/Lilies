from __future__ import annotations

from scripts.v02_127_e08_remaining_sidecar_architecture_reselection import (
    ARCHITECTURE_SLICE_IDS,
    COMPLETED_SLICES,
    select_slice,
)


def test_v02_127_selects_distributed_queue_semantics() -> None:
    result = select_slice()

    assert result["decision"] == "select_distributed_queue_semantics"
    assert result["selected_slice"]["slice_id"] == "distributed_queue_semantics"
    assert result["next_version"] == "v0.2.128_e08_distributed_queue_semantics"
    assert result["first_design"] == "docs/current-design/design_v0_2_128_e08_distributed_queue_semantics.md"


def test_v02_127_excludes_completed_supervision_and_worker_coverage() -> None:
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

    assert "production_worker_supervision" in completed_ids
    assert "production_worker_supervision" not in candidate_ids
    assert completed_worker_ids.issubset(completed_ids)
    assert candidate_ids.isdisjoint(completed_worker_ids)
    assert result["selected_slice"]["slice_id"] not in completed_ids
    assert result["invariants"]["completed_production_supervision_excluded"] is True
    assert result["invariants"]["required_worker_task_kind_execution_coverage_preserved"] is True


def test_v02_127_preserves_architecture_and_full_sidecar_boundary() -> None:
    result = select_slice()
    candidate_ids = [item["slice_id"] for item in result["remaining_candidates"]]

    assert set(candidate_ids).issubset(ARCHITECTURE_SLICE_IDS)
    assert candidate_ids == [
        "distributed_queue_semantics",
        "external_process_manager",
        "external_kms_provider_integration",
    ]
    assert result["invariants"]["remaining_candidates_are_architecture_only"] is True
    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert result["invariants"]["workingon_is_not_task_source"] is True
    assert result["invariants"]["stage_report_is_next_task_source"] is True
