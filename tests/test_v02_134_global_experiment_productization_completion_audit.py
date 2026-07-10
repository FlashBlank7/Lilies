from __future__ import annotations

from scripts.v02_134_global_experiment_productization_completion_audit import audit


def test_v02_134_covers_e01_to_e10_once() -> None:
    result = audit()
    experiment_ids = [item["experiment_id"] for item in result["experiments"]]

    assert experiment_ids == [f"E{index:02d}" for index in range(1, 11)]
    assert len(set(experiment_ids)) == 10
    assert result["experiment_count"] == 10


def test_v02_134_has_no_open_unblocked_gaps_and_preserves_blockers() -> None:
    result = audit()

    assert result["status"] == "completed"
    assert result["open_unblocked_gaps"] == []
    assert result["blocked_experiments"] == ["E02", "E10"]
    assert result["blocked_count"] == 2
    blockers = {item["experiment_id"]: item["blocker"] for item in result["experiments"]}
    assert blockers["E02"] == "blocked_external_panel"
    assert blockers["E10"] == "blocked_governance_boundary"
    assert result["global_completion_claimed"] is False


def test_v02_134_productized_lanes_are_current() -> None:
    result = audit()
    productized = {item["experiment_id"] for item in result["experiments"] if item["productized"]}

    assert productized == {"E05", "E07", "E08"}
    assert result["productized_count"] == 3
    assert result["completed_or_productized_count"] == 8
    assert result["missing_ledgers"] == []
    assert result["missing_evidence"] == []
