from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_48_customer_facing_workflow_run_interface.py"
    spec = importlib.util.spec_from_file_location("v03_48_customer_facing_workflow_run_interface_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_48_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_48_run_overview_is_customer_facing() -> None:
    module = load_audit_module()
    fixture = module.customer_run_overview_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["purpose_is_visible_without_brick_inspection"] is True
    assert fixture["cases"]["workflow_step_preview_visible"] is True


def test_v03_48_start_controls_preserve_explicit_run_boundary() -> None:
    module = load_audit_module()
    fixture = module.customer_start_controls_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["draft_and_published_start_stay_explicit"] is True
    assert fixture["cases"]["raw_payload_is_secondary_details"] is True


def test_v03_48_progress_uses_run_events_without_overclaiming() -> None:
    module = load_audit_module()
    fixture = module.customer_progress_data_flow_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["step_progress_uses_existing_run_events"] is True
    assert fixture["cases"]["missing_trace_evidence_not_overclaimed"] is True


def test_v03_48_result_card_prioritizes_user_result_over_raw_json() -> None:
    module = load_audit_module()
    fixture = module.customer_result_card_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["result_card_visible_before_technical_details"] is True
    assert fixture["cases"]["raw_json_remains_available_but_secondary"] is True


def test_v03_48_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_48_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_48_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.48"
    assert loaded["status"] == "passed"
