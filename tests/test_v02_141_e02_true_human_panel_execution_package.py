from __future__ import annotations

from scripts.v02_141_e02_true_human_panel_execution_package import build_evidence


def test_v02_141_required_panel_package_files_and_fields_present() -> None:
    evidence = build_evidence()

    assert evidence["status"] == "completed"
    assert evidence["checks"]["required_files_present"] is True
    assert evidence["checks"]["required_fields_present"] is True
    assert set(evidence["files"]) == {
        "README.md",
        "participant_protocol.md",
        "timing_rubric.md",
        "consent_safety_notes.md",
        "data_capture_schema.json",
        "blank_results.csv",
        "execution_checklist.md",
    }


def test_v02_141_panel_package_does_not_claim_e02_completion() -> None:
    evidence = build_evidence()

    assert evidence["checks"]["no_completion_claim"] is True
    assert evidence["checks"]["proxy_or_dry_run_cannot_complete_e02"] is True
    assert evidence["e02_true_human_panel_package_ready"] is True
    assert evidence["e02_true_human_panel_completed"] is False
    assert evidence["external_participant_rows_captured"] == 0
    assert evidence["global_completion_claimed"] is False


def test_v02_141_preserves_unrestricted_memory_boundary() -> None:
    evidence = build_evidence()

    assert evidence["checks"]["unrestricted_memory_forbidden_preserved"] is True
    assert evidence["unrestricted_memory_forbidden"] is True
