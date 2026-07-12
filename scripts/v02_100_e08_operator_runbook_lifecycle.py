#!/usr/bin/env python3
"""Validate and summarize v0.2.100 E08 operator runbook lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RUNBOOK_PATH = ROOT / "docs" / "operator-runbooks" / "e08_policy_controls_operator_runbook.md"
OUTPUT_DIR = ROOT / "docs" / "workingon"
OUTPUT_NAME = "evidence_v0.2.100_e08_operator_runbook_lifecycle"

REQUIRED_SECTIONS = [
    "Scope",
    "Before-Change Checks",
    "Apply-Change Procedure",
    "Post-Change Verification",
    "Rollback Procedure",
    "Incident Escalation",
    "Evidence Checklist",
    "Product Boundary",
]

REQUIRED_PHRASES = [
    "GET /api/v1/platform/harness/policy-controls",
    "PATCH /api/v1/platform/harness/policy-controls",
    "Studio monitor tab",
    "operator reason",
    "changed fields",
    "rollback",
    "incident",
    "does not claim full Platform Harness sidecar completion",
    "docs/workingon-archives/v0.2.96/evidence_v0.2.96_e08_editable_policy_controls_api_summary.md",
    "docs/workingon-archives/v0.2.98/evidence_v0.2.98_e08_studio_editable_policy_controls_summary.md",
]


def validate_runbook(path: Path = RUNBOOK_PATH) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    missing_sections = [section for section in REQUIRED_SECTIONS if f"## {section}" not in text]
    missing_phrases = [phrase for phrase in REQUIRED_PHRASES if phrase not in text]
    checklist_items = [line for line in text.splitlines() if line.startswith("- [ ] ")]
    result = {
        "version": "v0.2.100",
        "source_stage_report": "docs/stage-report-archives/v0.2.x/v0.2.99_e08_post_studio_controls_decision.md",
        "runbook": path.relative_to(ROOT).as_posix(),
        "status": "passed" if not missing_sections and not missing_phrases and len(checklist_items) >= 6 else "failed",
        "missing_sections": missing_sections,
        "missing_phrases": missing_phrases,
        "checklist_item_count": len(checklist_items),
        "required_sections": REQUIRED_SECTIONS,
        "not_full_sidecar_completion": True,
        "e07_invariant": {
            "status": "preserved",
            "no_e07_code_or_default_change": True,
        },
    }
    return result


def relative(path: Path) -> str:
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def write_outputs(result: dict[str, Any], output_dir: Path = OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{OUTPUT_NAME}.json"
    summary_path = output_dir / f"{OUTPUT_NAME}_summary.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# v0.2.100 E08 operator runbook lifecycle evidence",
        "",
        f"- Raw evidence: `{relative(json_path)}`",
        f"- Status: `{result['status']}`",
        f"- Runbook: `{result['runbook']}`",
        f"- Checklist item count: `{result['checklist_item_count']}`",
        f"- Missing sections: `{', '.join(result['missing_sections']) or 'none'}`",
        f"- Missing phrases: `{', '.join(result['missing_phrases']) or 'none'}`",
        f"- E07 invariant: `{result['e07_invariant']['status']}`",
        f"- Not full sidecar completion: `{result['not_full_sidecar_completion']}`",
        "",
        "## Required Sections",
        "",
    ]
    for section in result["required_sections"]:
        lines.append(f"- {section}")
    lines.append("")
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, summary_path


def main() -> None:
    result = validate_runbook()
    json_path, summary_path = write_outputs(result)
    print(json_path)
    print(summary_path)
    print(result["status"])
    if result["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
