from __future__ import annotations

from scripts.v02_136_e10_governed_memory_boundary_definition import REQUIRED_CONTROLS, boundary


def test_v02_136_boundary_has_all_required_controls() -> None:
    result = boundary()

    assert result["status"] == "completed"
    assert result["missing_controls"] == []
    assert set(result["controls"]) == set(REQUIRED_CONTROLS)
    assert result["accepted_product_scope"] is True


def test_v02_136_boundary_rejects_unrestricted_memory() -> None:
    result = boundary()

    assert result["unrestricted_memory_allowed"] is False
    assert result["filesystem_wrapper_allowed"] is False


def test_v02_136_preserves_e02_and_selects_next_e10_contract() -> None:
    result = boundary()

    assert result["e02_true_human_panel_resolved"] is False
    assert result["next_version"] == "v0.2.137_e10_governed_memory_surface_contract"
    assert result["first_design"] == "docs/current-design/design_v0_2_137_e10_governed_memory_surface_contract.md"
