from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_42_try_run_status_last_updated.py"
    spec = importlib.util.spec_from_file_location("v03_42_try_run_status_last_updated_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_42_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_42_recency_signal_updates_on_run_poll_and_cancel() -> None:
    module = load_audit_module()
    fixture = module.try_status_recency_signal_fixture()
    assert fixture["passed"] is True
    assert fixture["events"]["start_run_initializes"] is True
    assert fixture["events"]["poll_tick_updates"] is True
    assert fixture["events"]["cancel_confirm_updates"] is True


def test_v03_42_recency_guidance_does_not_claim_backend_completion_time() -> None:
    module = load_audit_module()
    fixture = module.try_status_recency_guidance_fixture()
    assert fixture["passed"] is True
    assert fixture["guidance"]["not_backend_completion_time"] is True
    assert fixture["guidance"]["current_status_is_truth"] is True


def test_v03_42_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_42_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_42_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.42"
    assert loaded["status"] == "passed"
