from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_45_try_run_input_error_recovery_ready_state.py"
    spec = importlib.util.spec_from_file_location("v03_45_try_run_input_error_recovery_ready_state_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_45_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_45_recovery_ready_state_is_explicit_after_correction() -> None:
    module = load_audit_module()
    fixture = module.try_input_error_recovery_ready_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["tracks_that_error_was_seen"] is True
    assert fixture["cases"]["shows_ready_after_error_clears"] is True
    assert fixture["cases"]["keeps_run_start_explicit"] is True


def test_v03_45_recovery_copy_points_to_preview_without_api_call() -> None:
    module = load_audit_module()
    fixture = module.try_input_recovery_confidence_copy_fixture()
    assert fixture["passed"] is True
    assert fixture["guidance"]["copy_points_to_payload_preview"] is True
    assert fixture["guidance"]["preview_focus_action_is_local"] is True
    assert fixture["guidance"]["does_not_call_run_api"] is True


def test_v03_45_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_45_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_45_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.45"
    assert loaded["status"] == "passed"

