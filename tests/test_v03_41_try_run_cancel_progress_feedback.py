from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_41_try_run_cancel_progress_feedback.py"
    spec = importlib.util.spec_from_file_location("v03_41_try_run_cancel_progress_feedback_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_41_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_41_cancel_progress_fixture_distinguishes_requested_completed_none() -> None:
    module = load_audit_module()
    fixture = module.try_cancel_progress_state_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["running_same_run"] == "requested"
    assert fixture["cases"]["cancelled_same_run"] == "completed"
    assert fixture["cases"]["running_other_run"] == "none"


def test_v03_41_cancel_recovery_guidance_requires_explicit_retry() -> None:
    module = load_audit_module()
    fixture = module.try_cancel_recovery_guidance_fixture()
    assert fixture["passed"] is True
    assert fixture["guidance"]["retry_is_explicit"] is True
    assert fixture["guidance"]["does_not_claim_successful_output"] is True


def test_v03_41_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_41_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_41_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.41"
    assert loaded["status"] == "passed"
