from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

from scripts.e02_human_panel_analyzer import PanelAnalysisError, analyze, parse_rows


FIELDS = [
    "participant_id",
    "group",
    "packet_type",
    "task_id",
    "started_at",
    "ended_at",
    "time_to_actionable_review_seconds",
    "completed",
    "localization_correct",
    "recommendation_actionable",
    "confidence_1_to_5",
    "facilitator_intervention_count",
    "preference",
    "notes",
]


def write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def row(participant: int, packet_type: str, seconds: int) -> dict[str, object]:
    return {
        "participant_id": f"P{participant:03d}",
        "group": "A" if participant % 2 else "B",
        "packet_type": packet_type,
        "task_id": f"task-{packet_type}",
        "started_at": "2026-07-10T10:00:00Z",
        "ended_at": "2026-07-10T10:05:00Z",
        "time_to_actionable_review_seconds": seconds,
        "completed": "true",
        "localization_correct": "true",
        "recommendation_actionable": "true",
        "confidence_1_to_5": 4,
        "facilitator_intervention_count": 0,
        "preference": "readable_testframe",
        "notes": "",
    }


def load_evidence_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_142_e02_panel_result_validator_analyzer.py"
    spec = importlib.util.spec_from_file_location("v02_142_e02_panel_analyzer_evidence_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_142_analyzer_supports_valid_paired_rows(tmp_path: Path) -> None:
    csv_path = tmp_path / "valid.csv"
    rows = []
    for participant in range(1, 6):
        rows.append(row(participant, "raw_json", 300 + participant))
        rows.append(row(participant, "readable_testframe", 180 + participant))
    write_rows(csv_path, rows)

    result = analyze(csv_path)

    assert result["row_count"] == 10
    assert result["pairing"]["minimum_participants_met"] is True
    assert result["pairing"]["pairing_valid"] is True
    assert result["timing_improvement_claim_supported"] is True
    assert result["e02_true_human_panel_completed"] is True
    assert result["global_completion_claimed"] is False


def test_v02_142_analyzer_rejects_unpaired_or_bad_header(tmp_path: Path) -> None:
    unpaired = tmp_path / "unpaired.csv"
    write_rows(unpaired, [row(1, "raw_json", 300)])
    unpaired_result = analyze(unpaired)
    assert unpaired_result["pairing"]["pairing_valid"] is False
    assert unpaired_result["e02_true_human_panel_completed"] is False

    bad = tmp_path / "bad.csv"
    bad.write_text("participant_id,packet_type\nP001,raw_json\n", encoding="utf-8")
    with pytest.raises(PanelAnalysisError, match="header"):
        parse_rows(bad)


def test_v02_142_default_blank_sheet_and_evidence_do_not_claim_completion() -> None:
    module = load_evidence_module()
    evidence = module.build_evidence()

    assert evidence["status"] == "completed"
    assert evidence["checks"]["blank_sheet_detects_zero_rows"] is True
    assert evidence["external_participant_rows_captured"] == 0
    assert evidence["e02_true_human_panel_completed"] is False
    assert evidence["global_completion_claimed"] is False
    assert evidence["unrestricted_memory_forbidden"] is True
