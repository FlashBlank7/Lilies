from __future__ import annotations

from scripts.v02_133_e08_full_sidecar_completion_audit import REQUIRED_SURFACES, audit


def test_v02_133_all_required_e08_sidecar_surfaces_have_evidence() -> None:
    result = audit()
    missing = [surface for surface in result["required_surfaces"] if not surface["exists"]]

    assert result["required_surface_count"] >= 18
    assert missing == []
    assert result["missing_required_gaps"] == []
    assert {surface.surface_id for surface in REQUIRED_SURFACES} == {
        surface["surface_id"] for surface in result["required_surfaces"]
    }


def test_v02_133_claims_full_sidecar_completion_with_boundaries() -> None:
    result = audit()

    assert result["status"] == "completed"
    assert result["decision"] == "claim_e08_full_sidecar_completion"
    assert result["boundaries"]["full_sidecar_completion_claimed"] is True
    assert result["boundaries"]["cloud_provider_deployment_claimed"] is False
    assert result["boundaries"]["cloud_provider_deployment_required_for_completion"] is False
    assert result["boundaries"]["workingon_is_not_task_source"] is True
    assert result["boundaries"]["stage_report_is_next_task_source"] is True


def test_v02_133_optional_followups_do_not_block_completion() -> None:
    result = audit()

    assert result["optional_followups"]
    assert all(item["blocks_full_sidecar_completion"] is False for item in result["optional_followups"])
    assert "cloud_specific_kms_clients" in {item["followup_id"] for item in result["optional_followups"]}
