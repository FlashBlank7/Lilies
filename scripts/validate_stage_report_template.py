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
    "Campaign Alignment",
    "Source Task Set",
    "Stage Contract",
    "Stage Objective",
    "Completed Work",
    "Verification",
    "Closure Audit",
    "Deviations",
    "Unresolved / Blocked / Deferred",
    "Evidence Debt",
    "Intent Coverage",
    "Experiment / Product Status Updates",
    "Historical Designs",
    "Workingon Archive",
    "Next-stage Task Set",
    "Archive Commit",
    "Automatic Evolution Handoff",
]

SECTION_ALIASES = {
    "Stage Identity": ("Stage Identity", "阶段信息"),
    "Campaign Alignment": ("Campaign Alignment", "总体目标对齐"),
    "Source Task Set": ("Source Task Set", "来源任务集"),
    "Stage Contract": ("Stage Contract", "阶段合同"),
    "Stage Objective": ("Stage Objective", "阶段目标"),
    "Goal": ("Goal", "目标"),
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
CANONICAL_SECTION = {
    alias: canonical
    for canonical, aliases in SECTION_ALIASES.items()
    for alias in aliases
}

# Backward-compatible import for the adoption audit. New reports use v2;
# historical reports remain valid under the legacy section contract.
REQUIRED_SECTIONS = V2_REQUIRED_SECTIONS


def headings(text: str) -> list[str]:
    return [
        CANONICAL_SECTION.get(line[3:].strip(), line[3:].strip())
        for line in text.splitlines()
        if line.startswith("## ")
    ]


def report_template_version(text: str) -> int:
    if (
        "| Template version | `2.0` |" in text
        or "| 模板版本 | `2.0` |" in text
        or "## Stage Contract" in text
        or "## 阶段合同" in text
    ):
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
            "stage contract must be locked": (
                "- Contract status: locked",
                "- 合同状态: 已锁定",
            ),
            "campaign alignment must state Campaign objective": (
                "| Campaign objective |",
                "| 总体目标 |",
            ),
            "stage contract lock is missing": ("- Contract lock:", "- 合同锁文件:"),
            "stage contract fingerprint is missing": (
                "- Contract fingerprint:",
                "- 合同指纹:",
            ),
            "stage contract approval is missing": (
                "- Contract approval:",
                "- 合同授权:",
            ),
            "stage contract baseline commit is missing": (
                "- Contract baseline commit:",
                "- 合同基线提交:",
            ),
            "closure audit verdict is missing": ("- Verdict:", "- 结论:"),
            "intent coverage table must include Source intent ID": (
                "| Source intent ID | Before stage | After stage |",
                "| 来源意图 ID | 阶段前 | 阶段后 |",
            ),
            "evidence debt table must include claim controls": (
                "| Evidence debt ID | Task ID | Intended level | Achieved level | Status | Unavailable dependency | Claim ceiling | Owner / carry target | Recheck trigger |",
                "| 证据债务 ID | 任务 ID | 目标等级 | 已达到等级 | 状态 | 不可用依赖 | 声明上限 | 负责人 / 承接目标 | 复查触发条件 |",
            ),
            "next-stage task table must include Task ID": (
                "| Task ID | Source intent IDs | Task |",
                "| 任务 ID | 来源意图 ID 列表 | 任务 |",
            ),
            "handoff must identify Current task ID": (
                "- Current task ID:",
                "- 当前任务 ID:",
            ),
            "handoff must identify First task ID": (
                "- First task ID:",
                "- 第一任务 ID:",
            ),
            "handoff must identify resume stage report": (
                "- Resume from stage report:",
                "- 恢复所依据的阶段报告:",
            ),
        }
        contract_errors.extend(
            message
            for message, markers in required_markers.items()
            if not any(marker in text for marker in markers)
        )
        if "First workingon" in text or "第一 workingon" in text:
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
