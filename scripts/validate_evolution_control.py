#!/usr/bin/env python3
"""Validate Lilies report-intent coverage and v2 stage closure contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = ROOT / "docs/evolution-control/report_intents.json"
INTENT_ID_RE = re.compile(r"\b(?:PRODUCT|ARCH|EVAL|GOV|SCENARIO|EVOL)-\d{3}\b")
TASK_ID_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)+$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
FORBIDDEN_WORKINGON_HEADINGS = re.compile(
    r"^#{1,6}\s+(?:Next-stage Task Set|Automatic Evolution Handoff|Roadmap Authority|Next Version)\b",
    flags=re.IGNORECASE | re.MULTILINE,
)
FORBIDDEN_WORKINGON_KEYS = {
    "next_stage",
    "next_stage_task_set",
    "next_task",
    "next_task_id",
    "next_version",
    "first_task_id",
}


@dataclass(frozen=True)
class MarkdownTable:
    headers: list[str]
    rows: list[dict[str, str]]


def load_registry(path: Path = DEFAULT_REGISTRY) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def repository_root_for_registry(path: Path) -> Path:
    resolved = path.resolve()
    if len(resolved.parents) >= 3:
        return resolved.parents[2]
    return ROOT


def validate_registry(path: Path = DEFAULT_REGISTRY, *, require_terminal: bool = False) -> list[str]:
    data = load_registry(path)
    root = repository_root_for_registry(path)
    errors: list[str] = []
    if data.get("task_authority") != "stage_report_only":
        errors.append("registry task_authority must be stage_report_only")
    terminal = set(data.get("terminal_statuses", []))
    non_terminal = set(data.get("non_terminal_statuses", []))
    allowed_statuses = terminal | non_terminal
    if terminal & non_terminal:
        errors.append("terminal and non-terminal statuses overlap")
    intents = data.get("intents")
    if not isinstance(intents, list) or not intents:
        return [*errors, "registry must contain intents"]
    seen: set[str] = set()
    for index, intent in enumerate(intents):
        prefix = f"intent[{index}]"
        intent_id = intent.get("id", "")
        if not INTENT_ID_RE.fullmatch(intent_id):
            errors.append(f"{prefix} has invalid id: {intent_id}")
        if intent_id in seen:
            errors.append(f"duplicate intent id: {intent_id}")
        seen.add(intent_id)
        if not str(intent.get("statement", "")).strip():
            errors.append(f"{intent_id} is missing statement")
        if not str(intent.get("acceptance", "")).strip():
            errors.append(f"{intent_id} is missing acceptance")
        status = intent.get("status")
        if status not in allowed_statuses:
            errors.append(f"{intent_id} has unknown status: {status}")
        if require_terminal and status not in terminal:
            errors.append(f"{intent_id} is not terminal: {status}")
        evidence_items = intent.get("evidence", [])
        if status in terminal and not evidence_items:
            errors.append(f"{intent_id} is terminal without evidence")
        for evidence in evidence_items:
            evidence_path = root / evidence
            if "::" not in evidence and not evidence_path.exists():
                errors.append(f"{intent_id} evidence path does not exist: {evidence}")
    source_report = data.get("source_report", "")
    if not source_report or not (root / source_report).exists():
        errors.append(f"source report does not exist: {source_report}")
    errors.extend(validate_program_charter_lock(root, data, require_git_baseline=require_terminal))
    return errors


def section_text(text: str, heading: str, next_headings: Iterable[str] = ()) -> str:
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return ""
    start += len(marker)
    end = len(text)
    for next_heading in next_headings:
        position = text.find(f"## {next_heading}", start)
        if position >= 0:
            end = min(end, position)
    return text[start:end]


def parse_first_table(block: str) -> MarkdownTable | None:
    lines = [line.strip() for line in block.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("|") or line.count("|") < 2:
            continue
        if index + 1 >= len(lines) or not re.fullmatch(r"\|[\s:|-]+\|", lines[index + 1]):
            continue
        headers = [cell.strip() for cell in line.strip("|").split("|")]
        rows: list[dict[str, str]] = []
        for row_line in lines[index + 2 :]:
            if not row_line.startswith("|"):
                break
            values = [cell.strip() for cell in row_line.strip("|").split("|")]
            if len(values) != len(headers):
                continue
            rows.append(dict(zip(headers, values)))
        return MarkdownTable(headers=headers, rows=rows)
    return None


def clean_code(value: str) -> str:
    cleaned = value.strip()
    if re.fullmatch(r"`[^`]+`", cleaned):
        return cleaned[1:-1].strip()
    return cleaned


def split_ids(value: str, pattern: re.Pattern[str]) -> list[str]:
    return pattern.findall(value)


def field_value(block: str, field: str) -> str:
    table = parse_first_table(block)
    if not table:
        return ""
    for row in table.rows:
        if clean_code(row.get("Field", "")) == field:
            return clean_code(row.get("Value", ""))
    return ""


def bullet_value(block: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}:[ \t]*(.*)$", block, flags=re.MULTILINE)
    return clean_code(match.group(1)) if match else ""


def contract_tables(stage_contract: str) -> tuple[MarkdownTable | None, MarkdownTable | None]:
    mandatory_marker = "### Mandatory Tasks"
    optional_marker = "### Optional Tasks"
    mandatory_start = stage_contract.find(mandatory_marker)
    optional_start = stage_contract.find(optional_marker)
    mandatory = None
    optional = None
    if mandatory_start >= 0:
        mandatory_end = optional_start if optional_start >= 0 else len(stage_contract)
        mandatory = parse_first_table(stage_contract[mandatory_start:mandatory_end])
    if optional_start >= 0:
        optional = parse_first_table(stage_contract[optional_start:])
    return mandatory, optional


def non_none_rows(table: MarkdownTable | None, key: str) -> list[dict[str, str]]:
    if table is None:
        return []
    return [row for row in table.rows if clean_code(row.get(key, "")).lower() not in {"", "none"}]


def normalized_task_row(row: dict[str, str], *, source: bool = False) -> dict[str, object]:
    normalized: dict[str, object] = {
        "task_id": clean_code(row.get("Task ID", "")),
        "source_intent_ids": sorted(split_ids(row.get("Source intent IDs", ""), INTENT_ID_RE)),
    }
    if source:
        normalized.update(
            {
                "source_task": clean_code(row.get("Source task from previous stage report", "")),
                "disposition": clean_code(row.get("Disposition in this stage", "")).lower(),
                "authority": clean_code(row.get("Authority / reason", "")),
            }
        )
    else:
        normalized.update(
            {
                "surface": clean_code(row.get("Surface / role", "")),
                "acceptance": clean_code(row.get("Acceptance criteria", "")),
                "required_evidence": clean_code(row.get("Required evidence", "")),
            }
        )
    return normalized


def contract_lock_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_repo_path(root: Path, value: str) -> Path | None:
    if not value or value.lower() in {"none", "pending"}:
        return None
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def git_file_at_commit(root: Path, commit: str, relative_path: str) -> bytes | None:
    try:
        return subprocess.run(
            ["git", "show", f"{commit}:{relative_path}"],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def git_first_path_commit(root: Path, relative_path: str) -> str | None:
    try:
        commits = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H", "--reverse", "--", relative_path],
            cwd=root,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return None
    return commits[0] if commits else None


def validate_program_charter_lock(
    root: Path,
    registry: dict,
    *,
    require_git_baseline: bool,
) -> list[str]:
    errors: list[str] = []
    lock_value = str(registry.get("program_charter_lock", ""))
    lock_path = safe_repo_path(root, lock_value)
    if lock_path is None or not lock_path.exists():
        return [f"program charter lock does not exist: {lock_value or 'missing'}"]
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except ValueError:
        return ["program charter lock is not valid JSON"]
    charter_value = str(lock.get("charter_path", ""))
    charter_path = safe_repo_path(root, charter_value)
    if charter_path is None or not charter_path.exists():
        return [f"program charter does not exist: {charter_value or 'missing'}"]
    digest = hashlib.sha256(charter_path.read_bytes()).hexdigest()
    if lock.get("charter_sha256") != digest:
        errors.append("Program Charter differs from its frozen SHA-256 lock")
    charter_text = charter_path.read_text(encoding="utf-8")
    version_match = re.search(r"^\| Charter version \| `([^`]+)` \|$", charter_text, flags=re.MULTILINE)
    charter_version = version_match.group(1) if version_match else ""
    if lock.get("charter_version") != charter_version:
        errors.append("Program Charter version does not match its lock")
    if not str(lock.get("approval_ref", "")).strip():
        errors.append("Program Charter lock has no approval reference")
    known_intents = {intent.get("id") for intent in registry.get("intents", [])}
    charter_intents = set(INTENT_ID_RE.findall(charter_text))
    for intent_id in sorted(charter_intents - known_intents):
        errors.append(f"Program Charter references unknown intent id: {intent_id}")
    if require_git_baseline:
        relative_lock = lock_path.relative_to(root).as_posix()
        baseline_commit = git_first_path_commit(root, relative_lock)
        if baseline_commit is None:
            errors.append("Program Charter lock has no Git baseline commit")
        else:
            frozen_lock = git_file_at_commit(root, baseline_commit, relative_lock)
            if frozen_lock != lock_path.read_bytes():
                errors.append("Program Charter lock differs from its first Git commit")
            frozen_charter = git_file_at_commit(root, baseline_commit, charter_path.relative_to(root).as_posix())
            if frozen_charter != charter_path.read_bytes():
                errors.append("Program Charter differs from the Charter-lock baseline commit")
    return errors


def json_keys(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key)
            yield from json_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from json_keys(nested)


def validate_workingon_authority(root: Path) -> list[str]:
    errors: list[str] = []
    workingon = root / "docs/workingon"
    if not workingon.exists():
        return errors
    for path in sorted(item for item in workingon.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if path.name == "README.md":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.suffix.lower() == ".json":
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            forbidden = sorted(set(json_keys(payload)) & FORBIDDEN_WORKINGON_KEYS)
            for key in forbidden:
                errors.append(f"workingon contains authoritative next-task key {key}: {relative}")
            for key, value in (payload.items() if isinstance(payload, dict) else []):
                if key == "task_authority" and value != "stage_report_only":
                    errors.append(f"workingon claims task authority: {relative}")
        if FORBIDDEN_WORKINGON_HEADINGS.search(text):
            errors.append(f"workingon contains authoritative next-task heading: {relative}")
        if re.search(r"^- (?:First task ID|Next version):", text, flags=re.MULTILINE | re.IGNORECASE):
            errors.append(f"workingon contains authoritative handoff field: {relative}")
    return errors


def validate_prior_major_archives(root: Path, current_version: str) -> list[str]:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", current_version)
    if match is None:
        return []
    major, current_minor, _patch = (int(part) for part in match.groups())
    errors: list[str] = []
    active_dir = root / "docs/stage-reports"
    for path in sorted(active_dir.glob(f"v{major}.*.*.md")):
        path_match = re.match(rf"v{major}\.(\d+)\.(\d+)", path.name)
        if path_match and int(path_match.group(1)) < current_minor:
            errors.append(f"completed prior-major stage report remains active: {path.relative_to(root)}")
    archive_index = root / "docs/stage-report-archives/README.md"
    archive_index_text = archive_index.read_text(encoding="utf-8") if archive_index.exists() else ""
    for minor in range(2, current_minor):
        archive_dir = root / f"docs/stage-report-archives/v{major}.{minor}.x"
        reports = sorted(archive_dir.glob(f"v{major}.{minor}.*.md")) if archive_dir.exists() else []
        if not reports:
            errors.append(f"prior-major archive is missing or empty: v{major}.{minor}.x")
            continue
        if not (archive_dir / "README.md").exists():
            errors.append(f"prior-major archive README is missing: v{major}.{minor}.x")
        phase_reports = list((root / "docs/phase-reports").glob(f"v{major}.{minor}.0_*.md"))
        if not phase_reports:
            errors.append(f"prior-major phase report is missing: v{major}.{minor}.x")
        if f"v{major}.{minor}.x/" not in archive_index_text:
            errors.append(f"prior-major archive index entry is missing: v{major}.{minor}.x")
    return errors


def validate_contract_lock(
    *,
    root: Path,
    report_path: Path,
    version: str,
    contract: str,
    source_rows: list[dict[str, str]],
    mandatory_rows: list[dict[str, str]],
    optional_rows: list[dict[str, str]],
    require_git_baseline: bool,
) -> list[str]:
    errors: list[str] = []
    lock_value = bullet_value(contract, "Contract lock")
    fingerprint = bullet_value(contract, "Contract fingerprint")
    revision = bullet_value(contract, "Contract revision")
    approval = bullet_value(contract, "Contract approval")
    baseline_commit = bullet_value(contract, "Contract baseline commit")
    lock_path = safe_repo_path(root, lock_value)
    if lock_path is None or not lock_path.exists():
        return [f"stage contract lock does not exist: {lock_value or 'missing'}"]
    expected_fingerprint = f"sha256:{contract_lock_digest(lock_path)}"
    if fingerprint != expected_fingerprint:
        errors.append("stage contract fingerprint does not match the lock file")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except ValueError:
        return [*errors, "stage contract lock is not valid JSON"]
    try:
        relative_report = report_path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        relative_report = "outside-repository"
    if lock.get("stage_report") != relative_report:
        errors.append("stage contract lock points to a different stage report")
    if lock.get("stage_version") != version:
        errors.append("stage contract lock version does not match the report")
    if str(lock.get("contract_revision", "")) != revision:
        errors.append("stage contract revision does not match the lock")
    if lock.get("approval_ref") != approval or not approval or approval.lower() in {"none", "pending"}:
        errors.append("stage contract approval record is missing or does not match the lock")
    report_source = [normalized_task_row(row, source=True) for row in source_rows]
    report_mandatory = [normalized_task_row(row) for row in mandatory_rows]
    report_optional = [normalized_task_row(row) for row in optional_rows]
    if lock.get("source_tasks") != report_source:
        errors.append("Source Task Set differs from the frozen contract lock")
    if lock.get("mandatory_tasks") != report_mandatory:
        errors.append("mandatory Stage Contract differs from the frozen contract lock")
    if lock.get("optional_tasks", []) != report_optional:
        errors.append("optional Stage Contract differs from the frozen contract lock")
    if require_git_baseline:
        if not GIT_COMMIT_RE.fullmatch(baseline_commit):
            errors.append("closure pass requires a 40-character Contract baseline commit")
        else:
            relative_lock = lock_path.relative_to(root).as_posix()
            first_commit = git_first_path_commit(root, relative_lock)
            if first_commit != baseline_commit:
                errors.append("Contract baseline commit is not the lock file's first Git commit")
            frozen = git_file_at_commit(root, baseline_commit, relative_lock)
            if frozen is None:
                errors.append("contract lock is absent from the baseline commit")
            elif frozen != lock_path.read_bytes():
                errors.append("contract lock differs from the Git baseline commit")
    return errors


def validate_stage_report(path: Path, registry_path: Path = DEFAULT_REGISTRY) -> list[str]:
    text = path.read_text(encoding="utf-8")
    root = repository_root_for_registry(registry_path)
    errors: list[str] = validate_workingon_authority(root)
    registry = load_registry(registry_path)
    known_intents = {intent["id"] for intent in registry["intents"]}
    intent_statuses = {intent["id"]: intent["status"] for intent in registry["intents"]}
    terminal_statuses = set(registry.get("terminal_statuses", []))
    unknown_intents = sorted(set(INTENT_ID_RE.findall(text)) - known_intents)
    errors.extend(f"unknown source intent id: {intent_id}" for intent_id in unknown_intents)

    identity = section_text(text, "Stage Identity", ["Source Task Set"])
    if field_value(identity, "Template version") != "2.0":
        return [*errors, "evolution-controlled stage report must use template version 2.0"]
    if field_value(identity, "Program charter") != "docs/evolution-control/PROGRAM_CHARTER.md":
        errors.append("stage report must reference the program charter")
    if not field_value(identity, "Stage scope justification") or "Explain why" in identity:
        errors.append("stage scope justification is missing or placeholder")
    version = field_value(identity, "Version")
    errors.extend(validate_prior_major_archives(root, version))

    source_block = section_text(text, "Source Task Set", ["Stage Contract"])
    source_table = parse_first_table(source_block)
    source_rows = non_none_rows(source_table, "Task ID")
    source_task_ids: set[str] = set()
    source_intents: set[str] = set()
    accepted_mandatory_intents: set[str] = set()
    carried_source_rows: list[dict[str, str]] = []
    for row in source_rows:
        task_id = clean_code(row.get("Task ID", ""))
        if not TASK_ID_RE.fullmatch(task_id):
            errors.append(f"invalid source task id: {task_id}")
        if task_id in source_task_ids:
            errors.append(f"duplicate source task id: {task_id}")
        source_task_ids.add(task_id)
        intent_ids = set(split_ids(row.get("Source intent IDs", ""), INTENT_ID_RE))
        if not intent_ids:
            errors.append(f"source task {task_id} has no source intent ids")
        source_intents.update(intent_ids)
        disposition = clean_code(row.get("Disposition in this stage", "")).lower()
        if "accepted" in disposition and "mandatory" in disposition:
            accepted_mandatory_intents.update(intent_ids)
        if any(marker in disposition for marker in ("carried", "deferred", "blocked", "superseded")):
            carried_source_rows.append(row)

    contract = section_text(text, "Stage Contract", ["Stage Objective"])
    if bullet_value(contract, "Contract status") != "locked":
        errors.append("stage contract must be locked")
    if "user" not in bullet_value(contract, "Contract change authority").lower():
        errors.append("mandatory contract change authority must require user approval")
    mandatory_table, optional_table = contract_tables(contract)
    mandatory_rows = non_none_rows(mandatory_table, "Task ID")
    optional_rows = non_none_rows(optional_table, "Task ID")
    if not mandatory_rows:
        errors.append("stage contract must contain at least one mandatory task")
    mandatory_ids: set[str] = set()
    contract_intents: set[str] = set()
    surfaces: set[str] = set()
    for row in mandatory_rows:
        task_id = clean_code(row.get("Task ID", ""))
        if not TASK_ID_RE.fullmatch(task_id):
            errors.append(f"invalid mandatory task id: {task_id}")
        if task_id in mandatory_ids:
            errors.append(f"duplicate mandatory task id: {task_id}")
        mandatory_ids.add(task_id)
        intent_ids = split_ids(row.get("Source intent IDs", ""), INTENT_ID_RE)
        if not intent_ids:
            errors.append(f"mandatory task {task_id} has no source intent ids")
        contract_intents.update(intent_ids)
        surface = clean_code(row.get("Surface / role", "")).lower()
        if surface and surface != "none":
            surfaces.update(item.strip() for item in re.split(r"[/,]", surface) if item.strip())
        if not clean_code(row.get("Acceptance criteria", "")):
            errors.append(f"mandatory task {task_id} has no acceptance criteria")
        if not clean_code(row.get("Required evidence", "")):
            errors.append(f"mandatory task {task_id} has no required evidence")
        status = clean_code(row.get("Status", "")).lower()
        if status not in {"accepted", "in_progress", "completed", "implemented_verified", "blocked"}:
            errors.append(f"mandatory task {task_id} has invalid status: {status or 'missing'}")

    optional_ids: set[str] = set()
    for row in optional_rows:
        task_id = clean_code(row.get("Task ID", ""))
        if not TASK_ID_RE.fullmatch(task_id):
            errors.append(f"invalid optional task id: {task_id}")
        if task_id in optional_ids or task_id in mandatory_ids:
            errors.append(f"duplicate contract task id: {task_id}")
        optional_ids.add(task_id)
        if not split_ids(row.get("Source intent IDs", ""), INTENT_ID_RE):
            errors.append(f"optional task {task_id} has no source intent ids")
    missing_accepted_intents = sorted(accepted_mandatory_intents - contract_intents)
    errors.extend(
        f"accepted mandatory source intent missing from Stage Contract: {intent_id}"
        for intent_id in missing_accepted_intents
    )

    completed_block = section_text(text, "Completed Work", ["Verification"])
    completed_table = parse_first_table(completed_block)
    completed_rows = non_none_rows(completed_table, "Task ID")
    completed = {clean_code(row.get("Task ID", "")): row for row in completed_rows}

    verification_block = section_text(text, "Verification", ["Closure Audit"])
    verification_table = parse_first_table(verification_block)
    verification_rows = non_none_rows(verification_table, "Task ID")
    verification_by_task: dict[str, list[dict[str, str]]] = {}
    for row in verification_rows:
        verification_by_task.setdefault(clean_code(row.get("Task ID", "")), []).append(row)

    closure_block = section_text(text, "Closure Audit", ["Deviations"])
    verdict = bullet_value(closure_block, "Verdict").lower()
    version_gate = bullet_value(closure_block, "Version-size gate").lower()
    closure_table = parse_first_table(closure_block)
    closure_rows = non_none_rows(closure_table, "Mandatory task ID")
    closure = {clean_code(row.get("Mandatory task ID", "")): row for row in closure_rows}

    unresolved_block = section_text(text, "Unresolved / Blocked / Deferred", ["Intent Coverage"])
    unresolved_table = parse_first_table(unresolved_block)
    unresolved_rows = non_none_rows(unresolved_table, "Task ID")
    mandatory_unresolved = [
        row
        for row in unresolved_rows
        if clean_code(row.get("Mandatory / optional", "")).lower() == "mandatory"
    ]

    coverage_block = section_text(text, "Intent Coverage", ["Experiment / Product Status Updates"])
    coverage_table = parse_first_table(coverage_block)
    coverage_rows = non_none_rows(coverage_table, "Source intent ID")
    covered_intents = {
        intent_id
        for row in coverage_rows
        for intent_id in split_ids(row.get("Source intent ID", ""), INTENT_ID_RE)
    }
    missing_coverage = sorted((source_intents | contract_intents) - covered_intents)
    errors.extend(f"source or contract intent missing from Intent Coverage: {item}" for item in missing_coverage)

    next_block = section_text(text, "Next-stage Task Set", ["Archive Commit"])
    next_table = parse_first_table(next_block)
    next_rows = non_none_rows(next_table, "Task ID")
    next_task_ids: set[str] = set()
    next_intents: set[str] = set()
    for row in next_rows:
        task_id = clean_code(row.get("Task ID", ""))
        if not TASK_ID_RE.fullmatch(task_id):
            errors.append(f"invalid next-stage task id: {task_id}")
        if task_id in next_task_ids:
            errors.append(f"duplicate next-stage task id: {task_id}")
        next_task_ids.add(task_id)
        intent_ids = split_ids(row.get("Source intent IDs", ""), INTENT_ID_RE)
        if not intent_ids:
            errors.append(f"next-stage task {task_id} has no source intent ids")
        next_intents.update(intent_ids)

    handoff = section_text(text, "Automatic Evolution Handoff")
    current_task = bullet_value(handoff, "Current task ID")
    first_task = bullet_value(handoff, "First task ID")
    if verdict != "pass" and current_task and current_task.lower() != "none" and current_task not in mandatory_ids:
        errors.append(f"handoff Current task ID is not in the Stage Contract: {current_task}")
    if first_task and first_task.lower() != "none" and first_task not in next_task_ids:
        errors.append(f"handoff First task ID is not in Next-stage Task Set: {first_task}")
    if "First workingon" in handoff:
        errors.append("workingon cannot be used as handoff task authority")

    errors.extend(
        validate_contract_lock(
            root=root,
            report_path=path,
            version=version,
            contract=contract,
            source_rows=source_rows,
            mandatory_rows=mandatory_rows,
            optional_rows=optional_rows,
            require_git_baseline=verdict == "pass",
        )
    )

    if verdict == "pass":
        errors.extend(validate_program_charter_lock(root, registry, require_git_baseline=True))
        if current_task and current_task.lower() != "none":
            errors.append("closure pass requires Current task ID to be none")
        if version_gate != "pass":
            errors.append("closure pass requires version-size gate pass")
        if len(mandatory_rows) < 3 or len(surfaces) < 3:
            errors.append("closure pass requires at least three distinct mandatory task surfaces")
        for task_id in sorted(mandatory_ids):
            contract_row = next(row for row in mandatory_rows if clean_code(row.get("Task ID", "")) == task_id)
            if clean_code(contract_row.get("Status", "")).lower() not in {"completed", "implemented_verified"}:
                errors.append(f"mandatory contract task not marked completed: {task_id}")
            completed_row = completed.get(task_id)
            if completed_row is None or clean_code(completed_row.get("Status", "")).lower() not in {
                "completed",
                "implemented_verified",
            }:
                errors.append(f"mandatory task not completed: {task_id}")
            elif clean_code(completed_row.get("Evidence", "")).lower() in {"", "none", "pending"}:
                errors.append(f"mandatory completed work has no evidence: {task_id}")
            task_checks = verification_by_task.get(task_id, [])
            valid_checks = [
                check
                for check in task_checks
                if clean_code(check.get("Result", "")).lower() == "pass"
                and clean_code(check.get("Evidence / exact command", "")).lower() not in {"", "none", "pending"}
            ]
            if not valid_checks:
                errors.append(f"mandatory task has no passing verification evidence: {task_id}")
            row = closure.get(task_id)
            if row is None:
                errors.append(f"mandatory task missing from Closure Audit: {task_id}")
                continue
            if clean_code(row.get("Acceptance result", "")).lower() != "pass":
                errors.append(f"mandatory task acceptance did not pass: {task_id}")
            if clean_code(row.get("Evidence valid", "")).lower() not in {"yes", "pass", "true"}:
                errors.append(f"mandatory task evidence is not valid: {task_id}")
            if clean_code(row.get("Auditor finding", "")).lower() in {"", "none", "pending", "unknown"}:
                errors.append(f"mandatory task has no auditor finding: {task_id}")
        for row in mandatory_unresolved:
            errors.append(f"mandatory unresolved item prevents closure: {clean_code(row.get('Task ID', ''))}")
        for source_row in carried_source_rows:
            task_id = clean_code(source_row.get("Task ID", ""))
            intent_ids = set(split_ids(source_row.get("Source intent IDs", ""), INTENT_ID_RE))
            unpreserved = sorted(
                intent_id
                for intent_id in intent_ids
                if intent_id not in next_intents and intent_statuses.get(intent_id) not in terminal_statuses
            )
            errors.extend(
                f"carried source intent is absent from Next-stage Task Set: {task_id}/{intent_id}"
                for intent_id in unpreserved
            )
    elif verdict not in {"pending", "fail", "blocked"}:
        errors.append(f"invalid closure verdict: {verdict or 'missing'}")

    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage_reports", nargs="*", type=Path)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--campaign-closure", action="store_true")
    args = parser.parse_args()
    failures = [
        *(f"registry: {error}" for error in validate_registry(args.registry, require_terminal=args.campaign_closure))
    ]
    for report in args.stage_reports:
        failures.extend(f"{report}: {error}" for error in validate_stage_report(report, args.registry))
    if failures:
        print("\n".join(failures))
        raise SystemExit(1)
    print("evolution control validation passed")


if __name__ == "__main__":
    main()
