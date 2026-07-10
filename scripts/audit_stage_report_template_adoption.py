#!/usr/bin/env python3
"""Audit recent Lilies stage reports against the mandatory template."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from validate_stage_report_template import REQUIRED_SECTIONS, validate_stage_report


VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class StageReportAudit:
    path: Path
    version: str
    errors: list[str]

    @property
    def conforms(self) -> bool:
        return not self.errors


def version_key(path: Path) -> tuple[int, int, int]:
    match = VERSION_RE.search(path.name)
    if not match:
        return (-1, -1, -1)
    return tuple(int(part) for part in match.groups())


def stage_report_paths(stage_reports_dir: Path) -> list[Path]:
    paths = [
        path
        for path in stage_reports_dir.glob("v*.md")
        if path.name != "STAGE_REPORT_TEMPLATE.md" and VERSION_RE.search(path.name)
    ]
    return sorted(paths, key=version_key)


def audit_stage_reports(stage_reports_dir: Path, limit: int = 8) -> list[StageReportAudit]:
    paths = stage_report_paths(stage_reports_dir)[-limit:]
    audits: list[StageReportAudit] = []
    for path in paths:
        match = VERSION_RE.search(path.name)
        version = match.group(0) if match else path.stem
        audits.append(StageReportAudit(path=path, version=version, errors=validate_stage_report(path)))
    return audits


def migration_recommendation(audits: Iterable[StageReportAudit]) -> str:
    audits = list(audits)
    if not audits:
        return "no_stage_reports_found"
    latest = audits[-1]
    if not latest.conforms:
        return "create_migration_plan_for_recent_reports"
    if any(not audit.conforms for audit in audits[:-1]):
        return "forward_only_keep_historical_reports_as_is"
    return "all_recent_reports_conform"


def format_audit(audits: list[StageReportAudit]) -> str:
    lines = ["| Report | Conforms | Errors |", "| --- | --- | --- |"]
    for audit in audits:
        errors = "; ".join(audit.errors) if audit.errors else "none"
        lines.append(f"| `{audit.path.as_posix()}` | {str(audit.conforms).lower()} | {errors} |")
    lines.append("")
    lines.append(f"Recommendation: `{migration_recommendation(audits)}`")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage-reports-dir", type=Path, default=Path("docs/stage-reports"))
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    audits = audit_stage_reports(args.stage_reports_dir, limit=args.limit)
    print(format_audit(audits))


if __name__ == "__main__":
    main()
