from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def load_validator() -> Any:
    module_path = ROOT / "scripts" / "validate_evolution_control.py"
    spec = importlib.util.spec_from_file_location("evolution_control_validator_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_report_intent_registry_is_valid() -> None:
    module = load_validator()

    assert module.validate_registry() == []


def test_campaign_closure_rejects_non_terminal_intents() -> None:
    module = load_validator()

    errors = module.validate_registry(require_terminal=True)

    assert any("is not terminal" in error for error in errors)


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
        template.replace("Explain why this is a serious version-sized unit. A prerequisite-only or repeated one-design version is invalid unless it is an explicit hotfix exception.", "Vertical process repair across docs, validator, tests, and runtime instructions.")
        .replace("`TASK-001`", "`V04-02-T01`")
        .replace("`INTENT-001`", "`EVOL-001`")
        .replace("process / backend / frontend / runtime / test / report / operations", "process")
        .replace("- Verdict: pending", "- Verdict: pass")
        .replace("- Version-size gate: pending", "- Version-size gate: pass")
    )
    report = tmp_path / "incomplete.md"
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "closure pass requires at least three distinct mandatory task surfaces" in errors
    assert "mandatory task not completed: V04-02-T01" in errors


def test_next_stage_task_requires_known_intent(tmp_path: Path) -> None:
    module = load_validator()
    template = (ROOT / "docs/stage-reports/STAGE_REPORT_TEMPLATE.md").read_text(encoding="utf-8")
    prefix, marker, next_stage = template.partition("## Next-stage Task Set")
    assert marker
    next_stage = next_stage.replace(
        "| `none` | `none` | none | none | none | none |",
        "| `V04-03-T01` | `PRODUCT-999` | Implement something | now | vertical | mandatory |",
        1,
    )
    text = prefix + marker + next_stage
    report = tmp_path / "unknown_intent.md"
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "unknown source intent id: PRODUCT-999" in errors


def test_masked_skill_cannot_legalize_drift_or_tiny_versions() -> None:
    skill_root = ROOT / "skills-tempmask" / "lilies-evolution-development"
    skill = (skill_root / "SKILL.md").read_text(encoding="utf-8")
    gates = (skill_root / "references" / "operating-gates.md").read_text(encoding="utf-8")
    templates = (skill_root / "references" / "templates.md").read_text(encoding="utf-8")
    combined = "\n".join((skill, gates, templates))

    assert "PROGRAM_CHARTER.md" in combined
    assert "report_intents.json" in combined
    assert "Closure Audit" in combined
    assert "Current task ID" in combined
    assert "First workingon" not in combined
    assert "open the smallest next stage" not in combined
    assert "Shrink the next stage" not in combined
    assert "explicitly blocked/deferred with evidence" not in combined
    assert "If a mandatory task is blocked" in combined
    assert "keep the stage open" in combined.lower()


def test_frozen_lock_rejects_deleted_or_reclassified_mandatory_task(tmp_path: Path) -> None:
    module = load_validator()
    source = ROOT / "docs/stage-reports/v0.4.2_report_baseline_and_evolution_control.md"
    text = source.read_text(encoding="utf-8")
    row = next(line for line in text.splitlines() if "`V04-02-T01A`" in line and "| report |" in line)
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
    text = text.replace("- Verdict: pending", "- Verdict: pass", 1).replace(
        "- Version-size gate: pending", "- Version-size gate: pass", 1
    )
    report = tmp_path / "v0.4.2_false_pass.md"
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert any(error.startswith("mandatory unresolved item prevents closure:") for error in errors)
    assert "mandatory task has no passing verification evidence: V04-02-T01D" in errors


def test_workingon_cannot_define_next_stage_authority(tmp_path: Path) -> None:
    module = load_validator()
    workingon = tmp_path / "docs/workingon"
    workingon.mkdir(parents=True)
    (workingon / "bad.md").write_text("## Next-stage Task Set\n\n- Next version: v9.9.9\n", encoding="utf-8")
    (workingon / "bad.json").write_text(json.dumps({"next_task_id": "TASK-999"}), encoding="utf-8")

    errors = module.validate_workingon_authority(tmp_path)

    assert any("authoritative next-task heading" in error for error in errors)
    assert any("authoritative next-task key next_task_id" in error for error in errors)


def test_terminal_registry_intent_requires_evidence(tmp_path: Path) -> None:
    module = load_validator()
    registry = json.loads((ROOT / "docs/evolution-control/report_intents.json").read_text(encoding="utf-8"))
    registry["source_report"] = "docs/source.docx"
    registry["intents"][0]["status"] = "implemented_verified"
    registry["intents"][0]["evidence"] = []
    registry_path = tmp_path / "docs/evolution-control/report_intents.json"
    registry_path.parent.mkdir(parents=True)
    (tmp_path / "docs/source.docx").write_bytes(b"placeholder")
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    errors = module.validate_registry(registry_path)

    assert "PRODUCT-001 is terminal without evidence" in errors
