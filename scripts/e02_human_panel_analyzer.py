#!/usr/bin/env python3
"""Validate and analyze E02 true human panel result CSV files."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "experiment-status" / "e02-human-panel" / "data_capture_schema.json"
DEFAULT_RESULTS = ROOT / "docs" / "experiment-status" / "e02-human-panel" / "blank_results.csv"


class PanelAnalysisError(ValueError):
    pass


@dataclass(frozen=True)
class PanelRow:
    participant_id: str
    group: str
    packet_type: str
    task_id: str
    time_to_actionable_review_seconds: float
    completed: bool
    localization_correct: bool
    recommendation_actionable: bool
    confidence_1_to_5: int
    facilitator_intervention_count: int


def required_fields(schema_path: Path = SCHEMA_PATH) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    fields = schema.get("required_fields")
    if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
        raise PanelAnalysisError("schema required_fields must be a string list")
    return fields


def parse_bool(value: str, *, field: str, row_number: int) -> bool:
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    raise PanelAnalysisError(f"row {row_number}: {field} must be boolean")


def parse_rows(csv_path: Path, schema_path: Path = SCHEMA_PATH) -> list[PanelRow]:
    fields = required_fields(schema_path)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != fields:
            raise PanelAnalysisError("CSV header does not match E02 panel schema")
        rows: list[PanelRow] = []
        for row_number, row in enumerate(reader, start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            try:
                packet_type = row["packet_type"].strip()
                if packet_type not in {"raw_json", "readable_testframe"}:
                    raise PanelAnalysisError(f"row {row_number}: packet_type must be raw_json or readable_testframe")
                confidence = int(row["confidence_1_to_5"])
                if confidence < 1 or confidence > 5:
                    raise PanelAnalysisError(f"row {row_number}: confidence_1_to_5 must be 1..5")
                rows.append(
                    PanelRow(
                        participant_id=row["participant_id"].strip(),
                        group=row["group"].strip(),
                        packet_type=packet_type,
                        task_id=row["task_id"].strip(),
                        time_to_actionable_review_seconds=float(row["time_to_actionable_review_seconds"]),
                        completed=parse_bool(row["completed"], field="completed", row_number=row_number),
                        localization_correct=parse_bool(row["localization_correct"], field="localization_correct", row_number=row_number),
                        recommendation_actionable=parse_bool(row["recommendation_actionable"], field="recommendation_actionable", row_number=row_number),
                        confidence_1_to_5=confidence,
                        facilitator_intervention_count=int(row["facilitator_intervention_count"]),
                    )
                )
            except KeyError as error:
                raise PanelAnalysisError(f"row {row_number}: missing field {error}") from error
            except ValueError as error:
                if isinstance(error, PanelAnalysisError):
                    raise
                raise PanelAnalysisError(f"row {row_number}: invalid numeric value") from error
    return rows


def validate_pairing(rows: list[PanelRow], *, minimum_participants: int = 5) -> dict[str, Any]:
    by_participant: dict[str, list[PanelRow]] = defaultdict(list)
    for row in rows:
        if not row.participant_id:
            raise PanelAnalysisError("participant_id cannot be empty")
        by_participant[row.participant_id].append(row)
    paired = []
    unpaired = []
    for participant_id, items in sorted(by_participant.items()):
        packet_types = {item.packet_type for item in items}
        if {"raw_json", "readable_testframe"}.issubset(packet_types):
            paired.append(participant_id)
        else:
            unpaired.append(participant_id)
    return {
        "participant_count": len(by_participant),
        "paired_participant_count": len(paired),
        "paired_participants": paired,
        "unpaired_participants": unpaired,
        "minimum_participants": minimum_participants,
        "minimum_participants_met": len(paired) >= minimum_participants,
        "pairing_valid": not unpaired and len(paired) == len(by_participant),
    }


def packet_metrics(rows: list[PanelRow], packet_type: str) -> dict[str, Any]:
    completed = [row for row in rows if row.packet_type == packet_type and row.completed]
    if not completed:
        return {
            "completed_rows": 0,
            "median_time_seconds": None,
            "localization_correct_rate": None,
            "recommendation_actionable_rate": None,
            "median_confidence": None,
            "total_facilitator_interventions": 0,
        }
    count = len(completed)
    return {
        "completed_rows": count,
        "median_time_seconds": median(row.time_to_actionable_review_seconds for row in completed),
        "localization_correct_rate": sum(row.localization_correct for row in completed) / count,
        "recommendation_actionable_rate": sum(row.recommendation_actionable for row in completed) / count,
        "median_confidence": median(row.confidence_1_to_5 for row in completed),
        "total_facilitator_interventions": sum(row.facilitator_intervention_count for row in completed),
    }


def analyze(csv_path: Path = DEFAULT_RESULTS, schema_path: Path = SCHEMA_PATH) -> dict[str, Any]:
    rows = parse_rows(csv_path, schema_path)
    pairing = validate_pairing(rows)
    metrics = {
        "raw_json": packet_metrics(rows, "raw_json"),
        "readable_testframe": packet_metrics(rows, "readable_testframe"),
    }
    readable_time = metrics["readable_testframe"]["median_time_seconds"]
    raw_time = metrics["raw_json"]["median_time_seconds"]
    readable_correct = metrics["readable_testframe"]["localization_correct_rate"]
    raw_correct = metrics["raw_json"]["localization_correct_rate"]
    has_real_completion_evidence = (
        pairing["minimum_participants_met"]
        and pairing["pairing_valid"]
        and readable_time is not None
        and raw_time is not None
        and readable_correct is not None
        and raw_correct is not None
    )
    timing_improvement_claim_supported = (
        has_real_completion_evidence
        and readable_time < raw_time
        and readable_correct >= raw_correct
    )
    return {
        "schema": schema_path.relative_to(ROOT).as_posix() if schema_path.is_relative_to(ROOT) else schema_path.as_posix(),
        "results_csv": csv_path.relative_to(ROOT).as_posix() if csv_path.is_relative_to(ROOT) else csv_path.as_posix(),
        "row_count": len(rows),
        "pairing": pairing,
        "metrics": metrics,
        "has_real_completion_evidence": has_real_completion_evidence,
        "timing_improvement_claim_supported": timing_improvement_claim_supported,
        "e02_true_human_panel_completed": timing_improvement_claim_supported,
        "global_completion_claimed": False,
    }


def write_summary(result: dict[str, Any], output_path: Path) -> None:
    raw = result["metrics"]["raw_json"]
    readable = result["metrics"]["readable_testframe"]
    lines = [
        "# E02 true human panel analysis",
        "",
        f"- Results CSV: `{result['results_csv']}`",
        f"- Row count: `{result['row_count']}`",
        f"- Paired participants: `{result['pairing']['paired_participant_count']}`",
        f"- Minimum participants met: `{result['pairing']['minimum_participants_met']}`",
        f"- Pairing valid: `{result['pairing']['pairing_valid']}`",
        f"- Raw median time: `{raw['median_time_seconds']}`",
        f"- Readable median time: `{readable['median_time_seconds']}`",
        f"- Raw localization rate: `{raw['localization_correct_rate']}`",
        f"- Readable localization rate: `{readable['localization_correct_rate']}`",
        f"- Timing improvement claim supported: `{result['timing_improvement_claim_supported']}`",
        f"- E02 true human panel completed: `{result['e02_true_human_panel_completed']}`",
        f"- Global completion claimed: `{result['global_completion_claimed']}`",
        "",
    ]
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", nargs="?", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--json-output", type=Path)
    parser.add_argument("--summary-output", type=Path)
    args = parser.parse_args()
    result = analyze(args.csv_path)
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.summary_output:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        write_summary(result, args.summary_output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
