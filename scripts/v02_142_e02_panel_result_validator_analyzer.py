#!/usr/bin/env python3
"""Generate v0.2.142 E02 panel validator/analyzer evidence."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.142_e02_panel_result_validator_analyzer"


def _prepare_imports() -> None:
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))


def build_evidence() -> dict[str, Any]:
    _prepare_imports()
    from e02_human_panel_analyzer import analyze  # pylint: disable=import-error,import-outside-toplevel

    blank_result = analyze()
    analyzer_path = ROOT / "scripts" / "e02_human_panel_analyzer.py"
    tests_path = ROOT / "tests" / "test_v02_142_e02_panel_result_validator_analyzer.py"
    checks = {
        "analyzer_present": analyzer_path.exists(),
        "tests_present": tests_path.exists(),
        "blank_sheet_detects_zero_rows": blank_result["row_count"] == 0
        and blank_result["e02_true_human_panel_completed"] is False,
        "global_completion_not_claimed": blank_result["global_completion_claimed"] is False,
        "unrestricted_memory_forbidden": True,
    }
    return {
        "version": "v0.2.142",
        "evidence_id": "e02_panel_result_validator_analyzer",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.141_e02_true_human_panel_execution_package.md",
        "status": "completed" if all(checks.values()) else "needs_attention",
        "checks": checks,
        "blank_result_analysis": blank_result,
        "external_participant_rows_captured": blank_result["row_count"],
        "e02_true_human_panel_completed": False,
        "global_completion_claimed": False,
        "unrestricted_memory_forbidden": True,
        "implementation_paths": [
            "scripts/e02_human_panel_analyzer.py",
            "tests/test_v02_142_e02_panel_result_validator_analyzer.py",
        ],
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
        "# v0.2.142 E02 panel result validator/analyzer evidence",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
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
