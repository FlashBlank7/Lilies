from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_32_try_run_sample_input.py"
    spec = importlib.util.spec_from_file_location("v03_32_try_run_sample_input_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_32_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_32_sample_summary_fixture_counts_sources() -> None:
    module = load_audit_module()
    fixture = module.try_run_sample_summary_fixture()
    assert fixture["passed"] is True
    assert fixture["field_count"] == 4
    assert fixture["acceptance_sample_count"] == 2


def test_v03_32_next_action_fixture_maps_customer_states() -> None:
    module = load_audit_module()
    fixture = module.try_run_next_action_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["missing_required"]["target"] == "sample_button"
    assert fixture["cases"]["ready"]["target"] == "draft_run_button"


def test_v03_32_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_32_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_32_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.32"
    assert loaded["status"] == "passed"
