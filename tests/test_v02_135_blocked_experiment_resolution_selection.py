from __future__ import annotations

from scripts.v02_135_blocked_experiment_resolution_selection import select_resolution


def test_v02_135_selects_e10_governed_memory_boundary_definition() -> None:
    result = select_resolution()

    assert result["decision"] == "select_e10_governed_memory_boundary_definition"
    assert result["selected"]["experiment_id"] == "E10"
    assert result["next_version"] == "v0.2.136_e10_governed_memory_boundary_definition"
    assert result["first_design"] == "docs/current-design/design_v0_2_136_e10_governed_memory_boundary_definition.md"


def test_v02_135_preserves_e02_external_blocker() -> None:
    result = select_resolution()
    candidates = {item["experiment_id"]: item for item in result["candidates"]}

    assert candidates["E02"]["can_progress_without_external_state"] is False
    assert result["invariants"]["e02_true_human_panel_remains_external_blocker"] is True
    assert result["invariants"]["e02_automated_substitute_claimed"] is False


def test_v02_135_does_not_claim_global_completion() -> None:
    result = select_resolution()

    assert result["invariants"]["global_completion_claimed"] is False
    assert result["invariants"]["e10_governance_boundary_selected"] is True
    assert result["invariants"]["workingon_is_not_task_source"] is True
    assert result["invariants"]["stage_report_is_next_task_source"] is True
