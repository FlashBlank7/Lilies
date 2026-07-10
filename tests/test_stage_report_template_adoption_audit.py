from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_audit_module() -> Any:
    repo = Path(__file__).resolve().parents[1]
    scripts_dir = repo / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    module_path = scripts_dir / "audit_stage_report_template_adoption.py"
    spec = importlib.util.spec_from_file_location("stage_report_template_audit_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_report(path: Path, sections: list[str]) -> None:
    body = "\n\n".join(f"## {section}\n\ncontent" for section in sections)
    path.write_text(f"# {path.stem}\n\n{body}\n", encoding="utf-8")


def test_stage_report_audit_recommends_forward_only_for_historical_mismatch(tmp_path: Path) -> None:
    module = load_audit_module()
    reports = tmp_path / "stage-reports"
    reports.mkdir()
    write_report(reports / "v0.2.61_old.md", ["Goal"])
    write_report(reports / "v0.2.62_new.md", module.REQUIRED_SECTIONS)

    audits = module.audit_stage_reports(reports, limit=2)

    assert [audit.version for audit in audits] == ["v0.2.61", "v0.2.62"]
    assert audits[0].conforms is False
    assert audits[1].conforms is True
    assert module.migration_recommendation(audits) == "forward_only_keep_historical_reports_as_is"


def test_stage_report_audit_requires_plan_when_latest_report_fails(tmp_path: Path) -> None:
    module = load_audit_module()
    reports = tmp_path / "stage-reports"
    reports.mkdir()
    write_report(reports / "v0.2.62_good.md", module.REQUIRED_SECTIONS)
    write_report(reports / "v0.2.63_bad.md", ["Goal"])

    audits = module.audit_stage_reports(reports, limit=2)

    assert audits[-1].version == "v0.2.63"
    assert audits[-1].conforms is False
    assert module.migration_recommendation(audits) == "create_migration_plan_for_recent_reports"
