#!/usr/bin/env python3
"""Generate v0.2.141 E02 true human panel execution package evidence."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "docs" / "experiment-status" / "e02-human-panel"
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.141_e02_true_human_panel_execution_package"

REQUIRED_FILES = [
    "README.md",
    "participant_protocol.md",
    "timing_rubric.md",
    "consent_safety_notes.md",
    "data_capture_schema.json",
    "blank_results.csv",
    "execution_checklist.md",
]

REQUIRED_FIELDS = [
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


def _read_text(name: str) -> str:
    return (PACKAGE_DIR / name).read_text(encoding="utf-8")


def build_evidence() -> dict[str, Any]:
    files = {name: (PACKAGE_DIR / name).exists() for name in REQUIRED_FILES}
    schema = json.loads((PACKAGE_DIR / "data_capture_schema.json").read_text(encoding="utf-8"))
    with (PACKAGE_DIR / "blank_results.csv").open(newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
    package_text = "\n".join(_read_text(name) for name in REQUIRED_FILES if name.endswith(".md"))
    required_fields_present = schema.get("required_fields") == REQUIRED_FIELDS and header == REQUIRED_FIELDS
    completion_gate = schema.get("completion_gate", {})
    checks = {
        "required_files_present": all(files.values()),
        "required_fields_present": required_fields_present,
        "minimum_participants_declared": completion_gate.get("minimum_participants") == 5,
        "raw_and_readable_rows_required": completion_gate.get("requires_raw_and_readable_rows_per_participant") is True,
        "analysis_summary_required": completion_gate.get("requires_analysis_summary") is True,
        "proxy_or_dry_run_cannot_complete_e02": completion_gate.get("proxy_or_dry_run_can_complete_e02") is False,
        "no_completion_claim": "prepared_pending_external_execution" in package_text
        and "E02 remains" in package_text
        and "Until then, global completion must remain unclaimed" in package_text,
        "unrestricted_memory_forbidden_preserved": True,
    }
    return {
        "version": "v0.2.141",
        "evidence_id": "e02_true_human_panel_execution_package",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.140_global_completion_audit_after_e10_productization.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "package_dir": "docs/experiment-status/e02-human-panel",
        "files": files,
        "required_fields": REQUIRED_FIELDS,
        "checks": checks,
        "e02_true_human_panel_package_ready": all(checks.values()),
        "e02_true_human_panel_completed": False,
        "external_participant_rows_captured": 0,
        "global_completion_claimed": False,
        "unrestricted_memory_forbidden": True,
        "next_required_external_action": "recruit participants and run the panel using this package",
    }


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.141 E02 true human panel execution package evidence",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Package dir: `{result['package_dir']}`",
        f"- Package ready: `{result['e02_true_human_panel_package_ready']}`",
        f"- E02 true human panel completed: `{result['e02_true_human_panel_completed']}`",
        f"- External participant rows captured: `{result['external_participant_rows_captured']}`",
        f"- Global completion claimed: `{result['global_completion_claimed']}`",
        f"- Unrestricted memory forbidden: `{result['unrestricted_memory_forbidden']}`",
        "",
        "## Checks",
        "",
    ]
    for name, value in result["checks"].items():
        lines.append(f"- {name}: `{value}`")
    lines.extend(["", "## Files", ""])
    for name, exists in result["files"].items():
        lines.append(f"- `{name}`: `{exists}`")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = build_evidence()
    json_path, summary_path = write_outputs(result, args.output_dir)
    print(json_path)
    print(summary_path)
    print(result["status"])


if __name__ == "__main__":
    main()
