#!/usr/bin/env python3
"""Shared, dependency-free helpers for Lilies evolution lifecycle hooks."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from validate_evolution_control import validate_stage_report


VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def repository_root(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return current


def version_key(path: Path) -> tuple[int, int, int]:
    match = VERSION_RE.search(path.name)
    if not match:
        return (-1, -1, -1)
    return tuple(int(part) for part in match.groups())


def is_v2_stage_report(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return "| Template version | `2.0` |" in text and "## Stage Contract" in text


def v2_stage_reports(root: Path) -> list[Path]:
    return sorted(
        [path for path in (root / "docs/stage-reports").glob("v*.md") if is_v2_stage_report(path)],
        key=version_key,
    )


def stage_report_errors(root: Path, path: Path) -> list[str]:
    registry = root / "docs/evolution-control/report_intents.json"
    try:
        return validate_stage_report(path, registry)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as error:
        return [f"validator error: {error}"]


def campaign_state(root: Path) -> dict[str, str]:
    registry_path = root / "docs/evolution-control/report_intents.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"campaign_objective": "missing", "campaign_priority_rule": "missing"}
    return {
        "campaign_objective": str(registry.get("campaign_objective", "missing")),
        "campaign_priority_rule": str(registry.get("priority_rule", "missing")),
    }


def latest_v2_stage_report(root: Path) -> Path | None:
    valid = [path for path in v2_stage_reports(root) if not stage_report_errors(root, path)]
    return max(valid, key=version_key) if valid else None


def section_text(text: str, heading: str, next_heading: str | None = None) -> str:
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = text.find(f"## {next_heading}", start) if next_heading else -1
    return text[start:] if end < 0 else text[start:end]


def bullet_value(block: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}:[ \t]*(.*)$", block, flags=re.MULTILINE)
    return match.group(1).strip().strip("`") if match else ""


def active_stage_state(root: Path) -> dict[str, str]:
    campaign = campaign_state(root)
    candidates = v2_stage_reports(root)
    report = latest_v2_stage_report(root)
    invalid = [path for path in candidates if stage_report_errors(root, path)]
    invalid_newer = [
        path.relative_to(root).as_posix()
        for path in invalid
        if report is None or version_key(path) > version_key(report)
    ]
    if report is None:
        return {
            **campaign,
            "stage_report": "none",
            "current_task_id": "none",
            "closure_verdict": "none",
            "contract_status": "none",
            "validation_status": "invalid" if invalid else "none",
            "invalid_newer_reports": ", ".join(invalid_newer) or "none",
        }
    text = report.read_text(encoding="utf-8")
    handoff = section_text(text, "Automatic Evolution Handoff")
    closure = section_text(text, "Closure Audit", "Deviations")
    contract = section_text(text, "Stage Contract", "Stage Objective")
    return {
        **campaign,
        "stage_report": report.relative_to(root).as_posix(),
        "current_task_id": bullet_value(handoff, "Current task ID") or "none",
        "closure_verdict": bullet_value(closure, "Verdict") or "missing",
        "contract_status": bullet_value(contract, "Contract status") or "missing",
        "validation_status": "valid",
        "invalid_newer_reports": ", ".join(invalid_newer) or "none",
    }


def git_snapshot(root: Path) -> dict[str, Any]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return {"head": "unknown", "status": []}
    return {"head": head, "status": status}


def checkpoint_payload(root: Path, hook_input: dict[str, Any] | None = None) -> dict[str, Any]:
    state = active_stage_state(root)
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "intermediate resume evidence only; not a next-stage task source",
        "hook_event": (hook_input or {}).get("hook_event_name", "manual"),
        **state,
        "git": git_snapshot(root),
    }


def read_hook_input() -> dict[str, Any]:
    try:
        import sys

        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return {}
