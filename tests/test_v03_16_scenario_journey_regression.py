from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_16_scenario_journey_regression.py"
    spec = importlib.util.spec_from_file_location("v03_16_scenario_journey_regression_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_16_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    checks = module.source_marker_checks()

    assert all(check["passed"] for check in checks)


def test_v03_16_scenario_matrix_includes_demo_reviewer() -> None:
    module = load_audit_module()
    matrix = module.scenario_matrix_evidence()

    assert matrix["passed"] is True
    assert matrix["journey_count"] >= 5
    assert "investor_demo_reviewer" in matrix["roles"]
    assert matrix["selected_p1_gap"]["id"] == "investor_demo_reviewer_application_list"


def test_v03_16_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["build_endpoint_called"] is False
    assert evidence["summary"]["journey_count"] >= 5


def test_v03_16_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_16_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"

    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.16"
    assert loaded["status"] == "passed"
