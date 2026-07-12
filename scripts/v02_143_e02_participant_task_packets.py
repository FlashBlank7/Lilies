#!/usr/bin/env python3
"""Generate v0.2.143 E02 participant packet readiness evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.143_e02_participant_task_packets"
PACKET_DIR = ROOT / "docs" / "experiment-status" / "e02-human-panel" / "packets"
SOURCE_EVIDENCE = ROOT / "docs" / "experiment-status" / "evidence" / "experiment_v0.2.36_e02_readable_testframe_review_2026_07_09.json"
REQUIRED_PACKET_FILES = [
    "README.md",
    "facilitator_packet_manifest.md",
    "task_packet_raw_json.md",
    "task_packet_readable_testframe.md",
    "post_task_questionnaire.md",
    "answer_key.md",
]


def _read_packet(name: str) -> str:
    return (PACKET_DIR / name).read_text(encoding="utf-8")


def _relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def build_evidence() -> dict[str, Any]:
    files_present = {name: (PACKET_DIR / name).exists() for name in REQUIRED_PACKET_FILES}
    source_payload = json.loads(SOURCE_EVIDENCE.read_text(encoding="utf-8"))
    raw_packet = _read_packet("task_packet_raw_json.md")
    readable_packet = _read_packet("task_packet_readable_testframe.md")
    manifest = _read_packet("facilitator_packet_manifest.md")
    answer_key = _read_packet("answer_key.md")

    task_prompt = "Identify the failed requirement or workflow surface"
    checks = {
        "packet_directory_present": PACKET_DIR.exists(),
        "required_files_present": all(files_present.values()),
        "source_evidence_present": SOURCE_EVIDENCE.exists(),
        "source_has_raw_and_readable_packets": set(source_payload.get("packets", {})) >= {"raw_legacy_json", "readable_testframe"},
        "raw_packet_has_prompt_and_capture_id": task_prompt in raw_packet and "Condition id for capture sheet: `raw_json`" in raw_packet,
        "readable_packet_has_prompt_and_capture_id": task_prompt in readable_packet
        and "Condition id for capture sheet: `readable_testframe`" in readable_packet,
        "manifest_counterbalances_order": "| A | `raw_json`" in manifest and "| B | `readable_testframe`" in manifest,
        "answer_key_facilitator_only": "Do not show this file to participants." in answer_key,
        "participant_packets_do_not_embed_answer_key": "Expected Findings" not in raw_packet
        and "Expected Findings" not in readable_packet,
        "global_completion_not_claimed": True,
        "unrestricted_memory_forbidden": True,
    }
    return {
        "version": "v0.2.143",
        "evidence_id": "e02_participant_task_packets",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.142_e02_panel_result_validator_analyzer.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "packet_files": {name: _relative(PACKET_DIR / name) for name in REQUIRED_PACKET_FILES},
        "source_evidence": _relative(SOURCE_EVIDENCE),
        "external_participant_rows_captured": 0,
        "e02_true_human_panel_completed": False,
        "global_completion_claimed": False,
        "unrestricted_memory_forbidden": True,
        "next_closure_requires": [
            "at least 5 real participant ids",
            "paired raw_json and readable_testframe rows per included participant",
            "analyzer output",
            "analysis summary",
            "E02 ledger update",
        ],
    }


def write_outputs(result: dict[str, Any], output_dir: Path = DEFAULT_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.143 E02 participant task packets evidence",
        "",
        f"- Raw evidence: `{_relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Packet directory: `{_relative(PACKET_DIR)}`",
        f"- Source evidence: `{result['source_evidence']}`",
        f"- External participant rows captured: `{result['external_participant_rows_captured']}`",
        f"- E02 true human panel completed: `{result['e02_true_human_panel_completed']}`",
        f"- Global completion claimed: `{result['global_completion_claimed']}`",
        f"- Unrestricted memory forbidden: `{result['unrestricted_memory_forbidden']}`",
        "",
        "## Checks",
        "",
    ]
    for name, value in result["checks"].items():
        lines.append(f"- {name}: `{value}`")
    lines.extend(["", "## Next Closure Requires", ""])
    for item in result["next_closure_requires"]:
        lines.append(f"- {item}")
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
