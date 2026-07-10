from __future__ import annotations

from scripts.v02_121_e08_remaining_sidecar_slice_reselection import COMPLETED_SLICES, select_slice


def test_v02_121_selects_benchmark_worker_offload_handler() -> None:
    result = select_slice()

    assert result["decision"] == "select_benchmark_worker_offload_handler"
    assert result["selected_slice"]["slice_id"] == "benchmark_worker_offload_handler"
    assert result["next_version"] == "v0.2.122_e08_benchmark_worker_offload_handler"
    assert result["first_design"] == "docs/current-design/design_v0_2_122_e08_benchmark_worker_offload_handler.md"


def test_v02_121_excludes_completed_draft_preview_and_prior_slices() -> None:
    result = select_slice()
    completed_ids = {item["slice_id"] for item in COMPLETED_SLICES}

    assert "draft_patch_preview_worker_offload_handler" in completed_ids
    assert "test_suite_worker_offload_handler" in completed_ids
    assert "workflow_run_worker_offload_handler" in completed_ids
    assert "scheduler_trigger_worker_offload_handler" in completed_ids
    assert result["selected_slice"]["slice_id"] not in completed_ids
    assert result["invariants"]["completed_draft_patch_preview_excluded"] is True
    assert result["invariants"]["completed_test_suite_excluded"] is True
    assert result["invariants"]["completed_workflow_run_excluded"] is True
    assert result["invariants"]["completed_scheduler_trigger_excluded"] is True


def test_v02_121_preserves_full_sidecar_boundary() -> None:
    result = select_slice()

    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert result["invariants"]["workingon_is_not_task_source"] is True
    assert result["invariants"]["stage_report_is_next_task_source"] is True
    candidate_ids = [item["slice_id"] for item in result["remaining_candidates"]]
    assert candidate_ids[0] == "benchmark_worker_offload_handler"
    assert "builder_build_worker_offload_handler" in candidate_ids
    assert "production_worker_supervision" in candidate_ids
    assert "distributed_queue_semantics" in candidate_ids
