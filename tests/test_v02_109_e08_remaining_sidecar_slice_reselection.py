from __future__ import annotations

from scripts.v02_109_e08_remaining_sidecar_slice_reselection import select_slice


def test_v02_109_selects_complete_handler_catalog() -> None:
    result = select_slice()

    assert result["status"] == "completed"
    assert result["decision"] == "select_complete_handler_catalog"
    assert result["selected_slice"]["slice_id"] == "complete_handler_catalog"
    assert result["next_version"] == "v0.2.110_e08_complete_handler_catalog"
    assert result["first_design"] == "docs/current-design/design_v0_2_110_e08_complete_handler_catalog.md"


def test_v02_109_excludes_completed_slices() -> None:
    result = select_slice()
    completed_ids = {item["slice_id"] for item in result["completed_slices"]}
    selected_id = result["selected_slice"]["slice_id"]

    assert "stdio_container_egress_allowlist_contract" in completed_ids
    assert "secret_kms_rotation_contract" in completed_ids
    assert selected_id not in completed_ids
    assert result["invariants"]["completed_stdio_slice_excluded"] is True
    assert result["invariants"]["completed_secret_slice_excluded"] is True


def test_v02_109_preserves_stage_boundaries() -> None:
    result = select_slice()

    assert result["source_stage_report"] == "docs/stage-report-archives/v0.2.x/v0.2.108_e08_secret_kms_rotation_contract.md"
    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert result["invariants"]["workingon_is_not_task_source"] is True
    assert result["remaining_candidates"][0]["slice_id"] == "complete_handler_catalog"
