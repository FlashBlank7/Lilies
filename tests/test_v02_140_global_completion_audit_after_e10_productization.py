from __future__ import annotations

from scripts.v02_140_global_completion_audit_after_e10_productization import audit


def test_v02_140_covers_e01_to_e10_once() -> None:
    result = audit()
    experiment_ids = [item["experiment_id"] for item in result["experiments"]]

    assert experiment_ids == [f"E{index:02d}" for index in range(1, 11)]
    assert len(set(experiment_ids)) == 10
    assert result["experiment_count"] == 10


def test_v02_140_all_non_external_productization_complete_but_no_global_completion_claim() -> None:
    result = audit()

    assert result["status"] == "completed"
    assert result["open_unblocked_gaps"] == []
    assert result["external_blockers"] == ["E02"]
    assert result["external_blocker_count"] == 1
    assert result["all_non_external_productization_complete"] is True
    assert result["global_completion_claimed"] is False
    assert result["e02_true_human_panel_resolved"] is False


def test_v02_140_e10_is_productized_and_unrestricted_memory_remains_forbidden() -> None:
    result = audit()
    by_id = {item["experiment_id"]: item for item in result["experiments"]}
    productized = {item["experiment_id"] for item in result["experiments"] if item["productized"]}

    assert by_id["E10"]["status"] == "productized"
    assert "governed boundary" in by_id["E10"]["productization_scope"]
    assert productized == {"E05", "E07", "E08", "E10"}
    assert result["productized_count"] == 4
    assert result["completed_or_productized_count"] == 9
    assert result["unrestricted_memory_forbidden"] is True
    assert result["missing_ledgers"] == []
    assert result["missing_evidence"] == []
    assert result["missing_ledger_status"] == []
