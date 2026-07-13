from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_39_try_run_active_status_refresh.py"
    spec = importlib.util.spec_from_file_location("v03_39_try_run_active_status_refresh_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_39_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_39_active_status_fixture_distinguishes_queued_and_running() -> None:
    module = load_audit_module()
    fixture = module.try_active_status_summary_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["queued_zh"] != fixture["cases"]["running_zh"]
    assert fixture["visible_for"] == ["queued", "running"]


def test_v03_39_stale_guidance_fixture_preserves_duplicate_start_boundary() -> None:
    module = load_audit_module()
    fixture = module.try_stale_status_guidance_fixture()
    assert fixture["passed"] is True
    assert fixture["guidance"]["auto_refresh_expectation"] is True
    assert fixture["guidance"]["duplicate_start_not_suggested"] is True


def test_v03_39_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_39_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_39_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.39"
    assert loaded["status"] == "passed"
