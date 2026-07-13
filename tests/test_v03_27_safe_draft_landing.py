from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_27_safe_draft_landing.py"
    spec = importlib.util.spec_from_file_location("v03_27_safe_draft_landing_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_27_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_27_safe_draft_landing_fixture() -> None:
    module = load_audit_module()
    fixture = module.safe_draft_landing_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["safe_draft"] is True


def test_v03_27_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False


def test_v03_27_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_27_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.27"
    assert loaded["status"] == "passed"


def test_v03_27_safe_draft_actions_do_not_trigger_work() -> None:
    module = load_audit_module()
    safety = module.safe_draft_action_safety_fixture()
    assert safety["passed"] is True
    assert safety["actions"]["build_later"]["target_tab"] == "build"
