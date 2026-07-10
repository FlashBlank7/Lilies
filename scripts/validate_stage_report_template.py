#!/usr/bin/env python3
"""Validate mandatory Lilies stage-report sections."""

from __future__ import annotations

import argparse
from pathlib import Path


REQUIRED_SECTIONS = [
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


def headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def validate_stage_report(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    present = headings(text)
    missing = [section for section in REQUIRED_SECTIONS if section not in present]
    order_errors: list[str] = []
    if not missing:
        positions = [present.index(section) for section in REQUIRED_SECTIONS]
        if positions != sorted(positions):
            order_errors.append("required sections are out of order")
    return [*(f"missing section: {section}" for section in missing), *order_errors]


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
