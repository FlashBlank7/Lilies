from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_40_try_run_cancel_safety_confirmation.py"
    spec = importlib.util.spec_from_file_location("v03_40_try_run_cancel_safety_confirmation_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_40_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_40_cancel_confirmation_fixture_blocks_accidental_cancel() -> None:
    module = load_audit_module()
    fixture = module.try_cancel_confirmation_fixture()
    assert fixture["passed"] is True
    assert fixture["first_click"] == "opens_confirmation_without_cancel_api"
    assert fixture["bound_to_current_run_id"] is True


def test_v03_40_cancel_intent_guidance_keeps_waiting_path() -> None:
    module = load_audit_module()
    fixture = module.try_cancel_intent_guidance_fixture()
    assert fixture["passed"] is True
    assert fixture["guidance"]["keep_waiting_available"] is True
    assert fixture["guidance"]["duplicate_start_not_suggested"] is True


def test_v03_40_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_40_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_40_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.40"
    assert loaded["status"] == "passed"
