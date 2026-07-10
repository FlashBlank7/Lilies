from __future__ import annotations

from scripts.v02_113_e08_remaining_sidecar_slice_reselection import select_slice


def test_v02_113_selects_scheduler_trigger_worker_offload_handler() -> None:
    result = select_slice()

    assert result["status"] == "completed"
    assert result["decision"] == "select_scheduler_trigger_worker_offload_handler"
    assert result["selected_slice"]["slice_id"] == "scheduler_trigger_worker_offload_handler"
    assert result["next_version"] == "v0.2.114_e08_scheduler_trigger_worker_offload_handler"
    assert result["first_design"] == "docs/current-design/design_v0_2_114_e08_scheduler_trigger_worker_offload_handler.md"


def test_v02_113_excludes_completed_slices() -> None:
    result = select_slice()
    completed_ids = {item["slice_id"] for item in result["completed_slices"]}
    selected_id = result["selected_slice"]["slice_id"]

    assert "distributed_heartbeat_registry" in completed_ids
    assert "complete_handler_catalog" in completed_ids
    assert selected_id not in completed_ids
    assert result["invariants"]["completed_heartbeat_registry_excluded"] is True
    assert result["invariants"]["completed_handler_catalog_excluded"] is True


def test_v02_113_preserves_stage_boundaries() -> None:
    result = select_slice()

    assert result["source_stage_report"] == "docs/stage-reports/v0.2.112_e08_distributed_heartbeat_registry.md"
    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert result["invariants"]["workingon_is_not_task_source"] is True
    assert result["remaining_candidates"][0]["slice_id"] == "scheduler_trigger_worker_offload_handler"
