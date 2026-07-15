from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_validator() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "validate_stage_report_template.py"
    spec = importlib.util.spec_from_file_location("stage_report_validator_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_stage_report_template_contains_required_sections() -> None:
    module = load_validator()
    template = Path(__file__).resolve().parents[1] / "docs" / "stage-reports" / "STAGE_REPORT_TEMPLATE.md"

    assert module.validate_stage_report(template) == []
    assert module.report_template_version(template.read_text(encoding="utf-8")) == 2


def test_stage_report_validator_rejects_missing_sections(tmp_path: Path) -> None:
    module = load_validator()
    report = tmp_path / "bad.md"
    report.write_text("# v0.0.0_bad\n\n## Goal\n\nOnly one section.\n", encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "missing section: Stage Identity" in errors
    assert "missing section: Automatic Evolution Handoff" in errors


def test_stage_report_validator_rejects_unlocked_v2_contract(tmp_path: Path) -> None:
    module = load_validator()
    template = Path(__file__).resolve().parents[1] / "docs" / "stage-reports" / "STAGE_REPORT_TEMPLATE.md"
    report = tmp_path / "bad_v2.md"
    text = template.read_text(encoding="utf-8").replace(
        "- Contract status: locked",
        "- Contract status: draft",
    )
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "stage contract must be locked" in errors


def test_stage_report_validator_requires_frozen_contract_markers(tmp_path: Path) -> None:
    module = load_validator()
    template = Path(__file__).resolve().parents[1] / "docs" / "stage-reports" / "STAGE_REPORT_TEMPLATE.md"
    report = tmp_path / "missing_lock.md"
    text = template.read_text(encoding="utf-8").replace("- Contract lock:", "- Removed lock:")
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "stage contract lock is missing" in errors


def test_stage_report_validator_rejects_workingon_handoff(tmp_path: Path) -> None:
    module = load_validator()
    template = Path(__file__).resolve().parents[1] / "docs" / "stage-reports" / "STAGE_REPORT_TEMPLATE.md"
    report = tmp_path / "bad_handoff.md"
    text = template.read_text(encoding="utf-8").replace(
        "- First task ID:",
        "- First workingon:",
    )
    report.write_text(text, encoding="utf-8")

    errors = module.validate_stage_report(report)

    assert "handoff must identify First task ID" in errors
    assert "handoff must not use First workingon" in errors
