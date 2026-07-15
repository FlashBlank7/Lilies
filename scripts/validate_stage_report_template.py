#!/usr/bin/env python3
"""Validate legacy and evolution-control Lilies stage reports."""

from __future__ import annotations

import argparse
from pathlib import Path


LEGACY_REQUIRED_SECTIONS = [
    "Stage Identity",
    "Source Task Set",
    "Goal",
    "Completed Work",
    "Verification",
    "Unresolved / Blocked / Deferred",
    "Experiment / Product Status Updates",
    "Historical Designs",
    "Workingon Archive",
    "Next-stage Task Set",
    "Archive Commit",
    "Automatic Evolution Handoff",
]

V2_REQUIRED_SECTIONS = [
    "Stage Identity",
    "Source Task Set",
    "Stage Contract",
    "Stage Objective",
    "Completed Work",
    "Verification",
    "Closure Audit",
    "Deviations",
    "Unresolved / Blocked / Deferred",
    "Intent Coverage",
    "Experiment / Product Status Updates",
    "Historical Designs",
    "Workingon Archive",
    "Next-stage Task Set",
    "Archive Commit",
    "Automatic Evolution Handoff",
]

# Backward-compatible import for the adoption audit. New reports use v2;
# historical reports remain valid under the legacy section contract.
REQUIRED_SECTIONS = V2_REQUIRED_SECTIONS


def headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def report_template_version(text: str) -> int:
    if "| Template version | `2.0` |" in text or "## Stage Contract" in text:
        return 2
    return 1


def validate_stage_report(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    present = headings(text)
    version = report_template_version(text)
    required = V2_REQUIRED_SECTIONS if version == 2 else LEGACY_REQUIRED_SECTIONS
    missing = [section for section in required if section not in present]
    order_errors: list[str] = []
    if not missing:
        positions = [present.index(section) for section in required]
        if positions != sorted(positions):
            order_errors.append("required sections are out of order")
    contract_errors: list[str] = []
    if version == 2:
        required_markers = {
            "stage contract must be locked": "- Contract status: locked",
            "stage contract lock is missing": "- Contract lock:",
            "stage contract fingerprint is missing": "- Contract fingerprint:",
            "stage contract approval is missing": "- Contract approval:",
            "stage contract baseline commit is missing": "- Contract baseline commit:",
            "closure audit verdict is missing": "- Verdict:",
            "intent coverage table must include Source intent ID": "| Source intent ID | Before stage | After stage |",
            "next-stage task table must include Task ID": "| Task ID | Source intent IDs | Task |",
            "handoff must identify Current task ID": "- Current task ID:",
            "handoff must identify First task ID": "- First task ID:",
            "handoff must identify resume stage report": "- Resume from stage report:",
        }
        contract_errors.extend(
            message for message, marker in required_markers.items() if marker not in text
        )
        if "First workingon" in text:
            contract_errors.append("handoff must not use First workingon")
    return [
        *(f"missing section: {section}" for section in missing),
        *order_errors,
        *contract_errors,
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    failures: list[str] = []
    for path in args.paths:
        errors = validate_stage_report(path)
        failures.extend(f"{path}: {error}" for error in errors)
    if failures:
        print("\n".join(failures))
        raise SystemExit(1)
    print("stage report template validation passed")


if __name__ == "__main__":
    main()
