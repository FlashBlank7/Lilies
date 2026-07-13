from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_19_application_card_quick_actions.py"
    spec = importlib.util.spec_from_file_location("v03_19_application_card_quick_actions_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_19_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    checks = module.source_marker_checks()

    assert all(check["passed"] for check in checks)


def test_v03_19_quick_action_fixture_behavior() -> None:
    module = load_audit_module()
    fixture = module.quick_action_fixture_evidence()

    assert fixture["passed"] is True
    assert fixture["ids"]["draft"] == ["edit", "acceptance"]
    assert fixture["ids"]["ready"] == ["acceptance", "publish_check"]
    assert fixture["ids"]["published"] == ["try", "monitor"]
    assert fixture["forbidden_hrefs"] == []


def test_v03_19_detail_tab_guard() -> None:
    module = load_audit_module()
    detail = module.detail_tab_deeplink_evidence()

    assert detail["passed"] is True
    assert detail["valid"]["test"] is True
    assert detail["valid"]["run"] is True
    assert detail["valid"]["monitor"] is True
    assert detail["invalid"]["publish"] is False


def test_v03_19_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["forbidden_endpoint_called"] is False


def test_v03_19_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_19_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"

    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.19"
    assert loaded["status"] == "passed"
