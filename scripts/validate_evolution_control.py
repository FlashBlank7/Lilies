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
    r"^#{1,6}\s+(?:Next-stage Task Set|Automatic Evolution Handoff|Roadmap Authority|Next Version|下一阶段任务集|自动演进交接|路线图权限|下一版本)\b",
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

SECTION_ALIASES = {
    "Stage Identity": ("Stage Identity", "阶段信息"),
    "Campaign Alignment": ("Campaign Alignment", "总体目标对齐"),
    "Source Task Set": ("Source Task Set", "来源任务集"),
    "Stage Contract": ("Stage Contract", "阶段合同"),
    "Stage Objective": ("Stage Objective", "阶段目标"),
    "Completed Work": ("Completed Work", "已完成工作"),
    "Verification": ("Verification", "验证"),
    "Closure Audit": ("Closure Audit", "闭环审计"),
    "Deviations": ("Deviations", "偏移记录"),
    "Unresolved / Blocked / Deferred": (
        "Unresolved / Blocked / Deferred",
        "未解决、阻塞与延期",
    ),
    "Evidence Debt": ("Evidence Debt", "证据债务"),
    "Intent Coverage": ("Intent Coverage", "意图覆盖"),
    "Experiment / Product Status Updates": (
        "Experiment / Product Status Updates",
        "实验与产品状态更新",
    ),
    "Historical Designs": ("Historical Designs", "历史设计"),
    "Workingon Archive": ("Workingon Archive", "工作记录归档"),
    "Next-stage Task Set": ("Next-stage Task Set", "下一阶段任务集"),
    "Archive Commit": ("Archive Commit", "归档提交"),
    "Automatic Evolution Handoff": (
        "Automatic Evolution Handoff",
        "自动演进交接",
    ),
}

SUBSECTION_ALIASES = {
    "Mandatory Tasks": ("Mandatory Tasks", "强制任务"),
    "Optional Tasks": ("Optional Tasks", "可选任务"),
}

TABLE_HEADER_ALIASES = {
    "字段": "Field",
    "值": "Value",
    "任务 ID": "Task ID",
    "来源意图 ID 列表": "Source intent IDs",
    "来源意图 IDs": "Source intent IDs",
    "上一阶段报告中的来源任务": "Source task from previous stage report",
    "本阶段处置": "Disposition in this stage",
    "设计 / 证据": "Design / evidence",
    "授权 / 原因": "Authority / reason",
    "涉及界面 / 角色": "Surface / role",
    "验收标准": "Acceptance criteria",
    "必需证据": "Required evidence",
    "状态": "Status",
    "事项": "Item",
    "证据": "Evidence",
    "检查": "Check",
    "结果": "Result",
    "证据 / 准确命令": "Evidence / exact command",
    "强制任务 ID": "Mandatory task ID",
    "验收结果": "Acceptance result",
    "证据有效": "Evidence valid",
    "审计结论": "Auditor finding",
    "偏移 ID": "Deviation ID",
    "类型": "Class",
    "原合同": "Original contract",
    "调整后的路线": "Changed route",
    "验收是否保持": "Acceptance preserved",
    "授权 / 证据": "Authority / evidence",
    "强制 / 可选": "Mandatory / optional",
    "原因": "Reason",
    "下一步 / 决策权": "Next action / decision authority",
    "证据债务 ID": "Evidence debt ID",
    "目标等级": "Intended level",
    "已达到等级": "Achieved level",
    "不可用依赖": "Unavailable dependency",
    "声明上限": "Claim ceiling",
    "负责人 / 承接目标": "Owner / carry target",
    "复查触发条件": "Recheck trigger",
    "来源意图 ID": "Source intent ID",
    "阶段前": "Before stage",
    "阶段后": "After stage",
    "剩余差距": "Remaining gap",
    "台账 / 界面": "Ledger / surface",
    "更新": "Update",
    "历史设计": "Historical design",
    "最终状态": "Final status",
    "归档": "Archive",
    "内容": "Contents",
    "任务": "Task",
    "为什么现在做": "Why now",
    "闭环目标": "Closure target",
}

FIELD_LABEL_ALIASES = {
    "模板版本": "Template version",
    "版本": "Version",
    "项目总纲": "Program charter",
    "来源报告": "Source report",
    "来源阶段报告": "Source stage report",
    "阶段类型": "Stage type",
    "闭环等级": "Closure level",
    "阶段范围说明": "Stage scope justification",
    "总体目标": "Campaign objective",
    "本阶段贡献": "Stage contribution",
    "声明边界": "Claim boundary",
    "总体阻塞判断": "Campaign blocker test",
}

BULLET_LABEL_ALIASES = {
    "Contract status": ("Contract status", "合同状态"),
    "Contract revision": ("Contract revision", "合同修订"),
    "Contract lock": ("Contract lock", "合同锁文件"),
    "Contract fingerprint": ("Contract fingerprint", "合同指纹"),
    "Contract approval": ("Contract approval", "合同授权"),
    "Contract baseline commit": ("Contract baseline commit", "合同基线提交"),
    "Contract change authority": ("Contract change authority", "合同变更权限"),
    "Auditor context": ("Auditor context", "审计上下文"),
    "Verdict": ("Verdict", "结论"),
    "Missing mandatory tasks": ("Missing mandatory tasks", "缺失的强制任务"),
    "Unsupported claims": ("Unsupported claims", "不受支持的声明"),
    "Version-size gate": ("Version-size gate", "版本规模门禁"),
    "Commit": ("Commit", "提交"),
    "Closure validator": ("Closure validator", "闭环验证器"),
    "Intent coverage validator": ("Intent coverage validator", "意图覆盖验证器"),
    "Active current-design clean": (
        "Active current-design clean",
        "当前 current-design 是否干净",
    ),
    "Active workingon clean": (
        "Active workingon clean",
        "当前 workingon 是否干净",
    ),
    "Continue": ("Continue", "是否继续"),
    "Current task ID": ("Current task ID", "当前任务 ID"),
    "Next version": ("Next version", "下一版本"),
    "First task ID": ("First task ID", "第一任务 ID"),
    "Resume from stage report": (
        "Resume from stage report",
        "恢复所依据的阶段报告",
    ),
    "Stop reason, if any": ("Stop reason, if any", "停止原因"),
}

VALUE_ALIASES = {
    "已锁定": "locked",
    "已接受": "accepted",
    "进行中": "in_progress",
    "已完成": "completed",
    "已实现并验证": "implemented_verified",
    "阻塞": "blocked",
    "通过": "pass",
    "失败": "fail",
    "是": "yes",
    "否": "no",
    "无": "none",
    "待定": "pending",
    "未知": "unknown",
    "强制": "mandatory",
    "可选": "optional",
    "已接受，强制": "accepted mandatory",
    "延续": "carried",
    "延期": "deferred",
    "已取代": "superseded",
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


def validate_registry(
    path: Path = DEFAULT_REGISTRY, *, require_terminal: bool = False
) -> list[str]:
    data = load_registry(path)
    root = repository_root_for_registry(path)
    errors: list[str] = []
    if not str(data.get("campaign_objective", "")).strip():
        errors.append("registry campaign_objective is missing")
    priority_rule = str(data.get("priority_rule", ""))
    required_priority_layers = (
        "latest_user_instruction",
        "product_north_star",
        "report_campaign_and_intent_registry",
        "stage_report_sequence",
        "stage_contract",
    )
    if not all(layer in priority_rule for layer in required_priority_layers):
        errors.append(
            "registry priority_rule does not keep the Product North Star and report campaign above stage mechanics"
        )
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
    required_product_intents = {
        "PRODUCT-010",
        "PRODUCT-011",
        "PRODUCT-012",
        "PRODUCT-013",
        "PRODUCT-014",
        "PRODUCT-015",
        "PRODUCT-016",
        "SCENARIO-004",
        "SCENARIO-005",
        "SCENARIO-006",
        "SCENARIO-007",
        "SCENARIO-008",
        "SCENARIO-009",
        "SCENARIO-010",
        "SCENARIO-011",
    }
    for intent_id in sorted(required_product_intents - seen):
        errors.append(f"registry is missing restored Product North Star intent: {intent_id}")
    source_report = data.get("source_report", "")
    if not source_report or not (root / source_report).exists():
        errors.append(f"source report does not exist: {source_report}")
    elif source_report == "docs/PRODUCT_NORTH_STAR.md":
        north_star = (root / source_report).read_text(encoding="utf-8")
        required_anchors = (
            "Dify",
            "传统企业",
            "个人",
            "机器学习",
            "RAG",
            "Excel",
            "检测与诊断",
            "预测与决策",
            "优化与规划",
            "增补型",
            "替换型",
            "接口适配",
        )
        for anchor in required_anchors:
            if anchor not in north_star:
                errors.append(f"Product North Star is missing required anchor: {anchor}")
    errors.extend(validate_program_charter_lock(root, data, require_git_baseline=require_terminal))
    return errors


def _heading_match(
    text: str,
    heading: str,
    *,
    level: int,
    start: int = 0,
) -> tuple[int, int] | None:
    matches: list[tuple[int, int]] = []
    aliases = (
        SECTION_ALIASES.get(heading, (heading,))
        if level == 2
        else SUBSECTION_ALIASES.get(heading, (heading,))
    )
    prefix = "#" * level
    for alias in aliases:
        marker = f"{prefix} {alias}"
        position = text.find(marker, start)
        if position >= 0:
            matches.append((position, len(marker)))
    return min(matches) if matches else None


def section_text(text: str, heading: str, next_headings: Iterable[str] = ()) -> str:
    match = _heading_match(text, heading, level=2)
    if match is None:
        return ""
    position, marker_length = match
    start = position + marker_length
    end = len(text)
    for next_heading in next_headings:
        next_match = _heading_match(text, next_heading, level=2, start=start)
        if next_match is not None:
            end = min(end, next_match[0])
    return text[start:end]


def parse_first_table(block: str) -> MarkdownTable | None:
    lines = [line.strip() for line in block.splitlines()]
    for index, line in enumerate(lines):
        if not line.startswith("|") or line.count("|") < 2:
            continue
        if index + 1 >= len(lines) or not re.fullmatch(r"\|[\s:|-]+\|", lines[index + 1]):
            continue
        headers = [
            TABLE_HEADER_ALIASES.get(cell.strip(), cell.strip())
            for cell in line.strip("|").split("|")
        ]
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
        cleaned = cleaned[1:-1].strip()
    lock_value = re.search(r"<!--\s*lock:(.*?)\s*-->", cleaned, flags=re.DOTALL)
    if lock_value is not None:
        cleaned = lock_value.group(1).strip()
    return VALUE_ALIASES.get(cleaned, cleaned)


def split_ids(value: str, pattern: re.Pattern[str]) -> list[str]:
    return pattern.findall(value)


def field_value(block: str, field: str) -> str:
    table = parse_first_table(block)
    if not table:
        return ""
    for row in table.rows:
        row_field = FIELD_LABEL_ALIASES.get(
            clean_code(row.get("Field", "")),
            clean_code(row.get("Field", "")),
        )
        if row_field == field:
            return clean_code(row.get("Value", ""))
    return ""


def bullet_value(block: str, label: str) -> str:
    for alias in BULLET_LABEL_ALIASES.get(label, (label,)):
        match = re.search(
            rf"^- {re.escape(alias)}:[ \t]*(.*)$",
            block,
            flags=re.MULTILINE,
        )
        if match:
            return clean_code(match.group(1))
    return ""


def contract_tables(stage_contract: str) -> tuple[MarkdownTable | None, MarkdownTable | None]:
    mandatory_match = _heading_match(stage_contract, "Mandatory Tasks", level=3)
    optional_match = _heading_match(stage_contract, "Optional Tasks", level=3)
    mandatory_start = mandatory_match[0] if mandatory_match else -1
    optional_start = optional_match[0] if optional_match else -1
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
    version_match = re.search(
        r"^\| Charter version \| `([^`]+)` \|$", charter_text, flags=re.MULTILINE
    )
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
            frozen_charter = git_file_at_commit(
                root, baseline_commit, charter_path.relative_to(root).as_posix()
            )
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
            for key, value in payload.items() if isinstance(payload, dict) else []:
                if key == "task_authority" and value != "stage_report_only":
                    errors.append(f"workingon claims task authority: {relative}")
        if FORBIDDEN_WORKINGON_HEADINGS.search(text):
            errors.append(f"workingon contains authoritative next-task heading: {relative}")
        if re.search(
            r"^- (?:First task ID|Next version|第一任务 ID|下一版本):",
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        ):
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
            errors.append(
                f"completed prior-major stage report remains active: {path.relative_to(root)}"
            )
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
    if (
        lock.get("approval_ref") != approval
        or not approval
        or approval.lower() in {"none", "pending"}
    ):
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
    if (
        not field_value(identity, "Stage scope justification")
        or "Explain why" in identity
        or "说明为什么" in identity
    ):
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
        if any(
            marker in disposition for marker in ("carried", "deferred", "blocked", "superseded")
        ):
            carried_source_rows.append(row)

    contract = section_text(text, "Stage Contract", ["Stage Objective"])
    if bullet_value(contract, "Contract status") != "locked":
        errors.append("stage contract must be locked")
    change_authority = bullet_value(contract, "Contract change authority").lower()
    if "user" not in change_authority and "用户" not in change_authority:
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
        if status not in {
            "accepted",
            "in_progress",
            "completed",
            "implemented_verified",
            "blocked",
        }:
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
    errors.extend(
        f"source or contract intent missing from Intent Coverage: {item}"
        for item in missing_coverage
    )

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
    continue_value = bullet_value(handoff, "Continue").lower()
    current_task = bullet_value(handoff, "Current task ID")
    first_task = bullet_value(handoff, "First task ID")
    stop_reason = bullet_value(handoff, "Stop reason, if any")
    if (
        verdict != "pass"
        and current_task
        and current_task.lower() != "none"
        and current_task not in mandatory_ids
    ):
        errors.append(f"handoff Current task ID is not in the Stage Contract: {current_task}")
    if first_task and first_task.lower() != "none" and first_task not in next_task_ids:
        errors.append(f"handoff First task ID is not in Next-stage Task Set: {first_task}")
    if "First workingon" in handoff or "第一 workingon" in handoff:
        errors.append("workingon cannot be used as handoff task authority")
    evidence_only_stop = any(
        marker in stop_reason.lower()
        for marker in (
            "browser",
            "evidence provider",
            "verification environment",
            "live evidence",
            "浏览器",
            "证据提供方",
            "验证环境",
            "真实证据",
        )
    )
    if continue_value == "no" and next_rows and evidence_only_stop:
        errors.append(
            "stage-local evidence unavailability cannot block the report campaign while authorized next-stage tasks remain"
        )

    evidence_debt_block = section_text(text, "Evidence Debt", ["Intent Coverage"])
    evidence_debt_table = parse_first_table(evidence_debt_block)
    evidence_debt_rows = non_none_rows(evidence_debt_table, "Evidence debt ID")
    if "blocked_by_environment" in text and not evidence_debt_rows:
        errors.append("blocked_by_environment requires an Evidence Debt row")
    for row in evidence_debt_rows:
        debt_id = clean_code(row.get("Evidence debt ID", ""))
        for field in ("Achieved level", "Claim ceiling", "Recheck trigger"):
            if clean_code(row.get(field, "")).lower() in {"", "none", "pending"}:
                errors.append(f"evidence debt {debt_id} is missing {field.lower()}")

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
            contract_row = next(
                row for row in mandatory_rows if clean_code(row.get("Task ID", "")) == task_id
            )
            if clean_code(contract_row.get("Status", "")).lower() not in {
                "completed",
                "implemented_verified",
            }:
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
                and clean_code(check.get("Evidence / exact command", "")).lower()
                not in {"", "none", "pending"}
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
            if clean_code(row.get("Auditor finding", "")).lower() in {
                "",
                "none",
                "pending",
                "unknown",
            }:
                errors.append(f"mandatory task has no auditor finding: {task_id}")
        for row in mandatory_unresolved:
            errors.append(
                f"mandatory unresolved item prevents closure: {clean_code(row.get('Task ID', ''))}"
            )
        for source_row in carried_source_rows:
            task_id = clean_code(source_row.get("Task ID", ""))
            intent_ids = set(split_ids(source_row.get("Source intent IDs", ""), INTENT_ID_RE))
            unpreserved = sorted(
                intent_id
                for intent_id in intent_ids
                if intent_id not in next_intents
                and intent_statuses.get(intent_id) not in terminal_statuses
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
        *(
            f"registry: {error}"
            for error in validate_registry(args.registry, require_terminal=args.campaign_closure)
        )
    ]
    for report in args.stage_reports:
        failures.extend(
            f"{report}: {error}" for error in validate_stage_report(report, args.registry)
        )
    if failures:
        print("\n".join(failures))
        raise SystemExit(1)
    print("evolution control validation passed")


if __name__ == "__main__":
    main()
