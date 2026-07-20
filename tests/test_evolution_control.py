from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_validator() -> Any:
    module_path = ROOT / "scripts" / "validate_evolution_control.py"
    spec = importlib.util.spec_from_file_location(
        "evolution_control_validator_under_test", module_path
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_report_intent_registry_is_valid() -> None:
    module = load_validator()

    assert module.validate_registry() == []


def test_campaign_priority_outranks_stage_mechanics_and_external_evidence() -> None:
    registry = json.loads(
        (ROOT / "docs/evolution-control/report_intents.json").read_text(encoding="utf-8")
    )
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    north_star = (ROOT / "docs/PRODUCT_NORTH_STAR.md").read_text(encoding="utf-8")
    charter = (ROOT / "docs/evolution-control/PROGRAM_CHARTER.md").read_text(encoding="utf-8")
    template = (ROOT / "docs/stage-reports/STAGE_REPORT_TEMPLATE.md").read_text(encoding="utf-8")

    assert "traditional-enterprise Product North Star" in registry["campaign_objective"]
    assert "Product Supremacy" in agents
    assert "docs/PRODUCT_NORTH_STAR.md" in agents
    assert "传统企业" in north_star
    assert "机器学习/深度学习" in north_star
    assert any(item["id"] == "PRODUCT-010" for item in registry["intents"])
    assert any(item["id"] == "SCENARIO-005" for item in registry["intents"])
    assert "Campaign Objective And Priority" in charter
    assert "blocked_by_environment" in charter
    assert "Do not retry an unchanged external blocker" in agents
    assert "## 证据债务" in template
    assert "forbids restoring, editing, or using" in agents


def test_external_evidence_stop_cannot_freeze_authorized_campaign_tasks(tmp_path: Path) -> None:
    module = load_validator()
    source = (
        ROOT / "docs/stage-reports/v0.4.3_usability_modes_evidence_and_regression_stabilization.md"
    )
    text = source.read_text(encoding="utf-8")
    text = re.sub(r"^- Continue:.*$", "- Continue: no", text, flags=re.MULTILINE)
    text = re.sub(
        r"^- Stop reason, if any:.*$",
        "- Stop reason, if any: Browser evidence provider is unavailable",
        text,
        flags=re.MULTILINE,
    )
    report = tmp_path / source.name
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert any(
        "evidence unavailability cannot block the report campaign" in error for error in errors
    )


def test_current_contract_lock_is_frozen_in_its_first_git_commit() -> None:
    module = load_validator()
    report = ROOT / "docs/stage-reports/v0.4.2_report_baseline_and_evolution_control.md"
    text = report.read_text(encoding="utf-8")
    source = module.parse_first_table(
        module.section_text(text, "Source Task Set", ["Stage Contract"])
    )
    contract = module.section_text(text, "Stage Contract", ["Stage Objective"])
    mandatory, optional = module.contract_tables(contract)

    errors = module.validate_contract_lock(
        root=ROOT,
        report_path=report,
        version="v0.4.2",
        contract=contract,
        source_rows=module.non_none_rows(source, "Task ID"),
        mandatory_rows=module.non_none_rows(mandatory, "Task ID"),
        optional_rows=module.non_none_rows(optional, "Task ID"),
        require_git_baseline=True,
    )

    assert errors == []


def test_program_charter_is_frozen_and_uses_registry_intent_ids() -> None:
    module = load_validator()
    registry = module.load_registry()

    errors = module.validate_program_charter_lock(ROOT, registry, require_git_baseline=True)

    assert errors == []


def test_prior_major_stage_report_sets_are_fully_archived() -> None:
    module = load_validator()

    assert module.validate_prior_major_archives(ROOT, "v0.4.2") == []


def test_prior_major_archive_validator_rejects_active_old_report(tmp_path: Path) -> None:
    module = load_validator()
    active = tmp_path / "docs/stage-reports"
    active.mkdir(parents=True)
    (active / "v0.3.56_old.md").write_text("# old\n", encoding="utf-8")
    for minor in (2, 3):
        archive = tmp_path / f"docs/stage-report-archives/v0.{minor}.x"
        archive.mkdir(parents=True)
        (archive / "README.md").write_text("# archive\n", encoding="utf-8")
        (archive / f"v0.{minor}.1_stage.md").write_text("# stage\n", encoding="utf-8")
        phases = tmp_path / "docs/phase-reports"
        phases.mkdir(parents=True, exist_ok=True)
        (phases / f"v0.{minor}.0_closeout.md").write_text("# closeout\n", encoding="utf-8")
    (tmp_path / "docs/stage-report-archives/README.md").write_text(
        "v0.2.x/\nv0.3.x/\n", encoding="utf-8"
    )

    errors = module.validate_prior_major_archives(tmp_path, "v0.4.2")

    assert any("completed prior-major stage report remains active" in error for error in errors)


def test_campaign_closure_rejects_reopened_product_intents() -> None:
    module = load_validator()
    assert module.validate_registry() == []
    errors = module.validate_registry(require_terminal=True)

    assert any("PRODUCT-010 is not terminal" in error for error in errors)
    assert any("SCENARIO-005 is not terminal" in error for error in errors)


def test_v2_template_is_structural_but_not_a_closable_stage() -> None:
    module = load_validator()
    template = ROOT / "docs/stage-reports/STAGE_REPORT_TEMPLATE.md"

    errors = module.validate_stage_report(template)

    assert "stage scope justification is missing or placeholder" in errors
    assert "mandatory task TASK-001 has no source intent ids" in errors


def test_closure_pass_rejects_incomplete_mandatory_task(tmp_path: Path) -> None:
    module = load_validator()
    template = (ROOT / "docs/stage-reports/STAGE_REPORT_TEMPLATE.md").read_text(encoding="utf-8")
    text = (
        template.replace(
            "说明为什么这是一个具有足够复杂度、值得单独推进版本的任务单元。除明确的紧急修复外，只完成一个前置条件或反复只做一份设计文档，不足以推进版本。",
            "跨越文档、验证器、测试和运行指令的垂直流程修复。",
        )
        .replace("`TASK-001`", "`V04-02-T01`")
        .replace("`INTENT-001`", "`EVOL-001`")
        .replace("流程 / 后端 / 前端 / 运行时 / 测试 / 报告 / 运维", "流程")
        .replace("- 结论: 待定", "- 结论: 通过")
        .replace("- 版本规模门禁: 待定", "- 版本规模门禁: 通过")
    )
    report = tmp_path / "incomplete.md"
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "closure pass requires at least three distinct mandatory task surfaces" in errors
    assert "mandatory task not completed: V04-02-T01" in errors


def test_next_stage_task_requires_known_intent(tmp_path: Path) -> None:
    module = load_validator()
    template = (ROOT / "docs/stage-reports/STAGE_REPORT_TEMPLATE.md").read_text(encoding="utf-8")
    prefix, marker, next_stage = template.partition("## 下一阶段任务集")
    assert marker
    next_stage = next_stage.replace(
        "| `无` | `无` | 无 | 无 | 无 | 无 |",
        "| `V04-03-T01` | `PRODUCT-999` | 实现某项能力 | 现在 | 垂直闭环 | 强制 |",
        1,
    )
    text = prefix + marker + next_stage
    report = tmp_path / "unknown_intent.md"
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "unknown source intent id: PRODUCT-999" in errors


def test_repository_rules_cannot_legalize_drift_or_tiny_versions() -> None:
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    charter = (ROOT / "docs/evolution-control/PROGRAM_CHARTER.md").read_text(encoding="utf-8")
    template = (ROOT / "docs/stage-reports/STAGE_REPORT_TEMPLATE.md").read_text(encoding="utf-8")
    combined = "\n".join((agents, charter, template))

    assert "PROGRAM_CHARTER.md" in combined
    assert "report_intents.json" in combined
    assert "闭环审计" in template
    assert "当前任务 ID" in template
    assert "First workingon" not in combined
    assert "open the smallest next stage" not in combined
    assert "Shrink the next stage" not in combined
    assert "explicitly blocked/deferred with evidence" not in combined
    assert "campaign blocker exists only when no remaining report intent" in combined.lower()
    assert "higher-level evidence limits the claim" in combined
    assert "具有足够复杂度、值得单独推进版本的任务单元" in template
    assert not (ROOT / "skills-tempmask/lilies-evolution-development").exists()


def test_frozen_lock_rejects_deleted_or_reclassified_mandatory_task(tmp_path: Path) -> None:
    module = load_validator()
    source = ROOT / "docs/stage-reports/v0.4.2_report_baseline_and_evolution_control.md"
    text = source.read_text(encoding="utf-8")
    row = next(
        line for line in text.splitlines() if "`V04-02-T01A`" in line and "| report |" in line
    )
    report = tmp_path / "v0.4.2_tampered.md"
    report.write_text(text.replace(row + "\n", "", 1), encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "mandatory Stage Contract differs from the frozen contract lock" in errors


def test_source_task_intent_cannot_disappear_from_coverage(tmp_path: Path) -> None:
    module = load_validator()
    source = ROOT / "docs/stage-reports/v0.4.2_report_baseline_and_evolution_control.md"
    text = source.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if line.startswith("| `ARCH-001` |"))
    report = tmp_path / "v0.4.2_missing_coverage.md"
    report.write_text(text.replace(row + "\n", "", 1), encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "source or contract intent missing from Intent Coverage: ARCH-001" in errors


def test_closure_pass_rejects_unresolved_mandatory_and_missing_verification(tmp_path: Path) -> None:
    module = load_validator()
    source = ROOT / "docs/stage-reports/v0.4.2_report_baseline_and_evolution_control.md"
    text = source.read_text(encoding="utf-8")
    unresolved_row = next(
        line
        for line in text.splitlines()
        if line.startswith("| `V04-03-T01` | Dirty-worktree product regression debt")
    )
    text = text.replace("| next-stage mandatory |", "| mandatory |", 1)
    final_audit_row = next(
        line
        for line in text.splitlines()
        if line.startswith("| `V04-02-T01E` | Independent final closure audit")
    )
    text = text.replace(final_audit_row, final_audit_row.replace("| pass |", "| fail |", 1), 1)
    report = tmp_path / "v0.4.2_false_pass.md"
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert unresolved_row
    assert "mandatory unresolved item prevents closure: V04-03-T01" in errors
    assert "mandatory task has no passing verification evidence: V04-02-T01E" in errors


def test_closure_pass_rejects_empty_completed_work_evidence(tmp_path: Path) -> None:
    module = load_validator()
    source = ROOT / "docs/stage-reports/v0.4.2_report_baseline_and_evolution_control.md"
    text = source.read_text(encoding="utf-8")
    row = next(
        line
        for line in text.splitlines()
        if line.startswith("| `V04-02-T01E` | Independent closure audit and blocker remediation")
    )
    cells = row.split("|")
    cells[3] = " completed "
    cells[4] = " none "
    text = text.replace(row, "|".join(cells), 1)
    text = text.replace("- Verdict: pending", "- Verdict: pass", 1).replace(
        "- Version-size gate: pending", "- Version-size gate: pass", 1
    )
    report = tmp_path / "v0.4.2_empty_evidence.md"
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "mandatory completed work has no evidence: V04-02-T01E" in errors


def test_workingon_cannot_define_next_stage_authority(tmp_path: Path) -> None:
    module = load_validator()
    workingon = tmp_path / "docs/workingon"
    workingon.mkdir(parents=True)
    (workingon / "bad.md").write_text(
        "## Next-stage Task Set\n\n- Next version: v9.9.9\n", encoding="utf-8"
    )
    (workingon / "bad.json").write_text(json.dumps({"next_task_id": "TASK-999"}), encoding="utf-8")

    errors = module.validate_workingon_authority(tmp_path)

    assert any("authoritative next-task heading" in error for error in errors)
    assert any("authoritative next-task key next_task_id" in error for error in errors)


def test_terminal_registry_intent_requires_evidence(tmp_path: Path) -> None:
    module = load_validator()
    registry = json.loads(
        (ROOT / "docs/evolution-control/report_intents.json").read_text(encoding="utf-8")
    )
    registry["source_report"] = "docs/source.docx"
    registry["intents"][0]["status"] = "implemented_verified"
    registry["intents"][0]["evidence"] = []
    registry_path = tmp_path / "docs/evolution-control/report_intents.json"
    registry_path.parent.mkdir(parents=True)
    (tmp_path / "docs/source.docx").write_bytes(b"placeholder")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    errors = module.validate_registry(registry_path)

    assert "PRODUCT-001 is terminal without evidence" in errors
