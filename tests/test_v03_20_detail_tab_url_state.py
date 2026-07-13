from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_20_detail_tab_url_state.py"
    spec = importlib.util.spec_from_file_location("v03_20_detail_tab_url_state_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_20_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    checks = module.source_marker_checks()

    assert all(check["passed"] for check in checks)


def test_v03_20_tab_url_state_preserves_build_and_method() -> None:
    module = load_audit_module()
    fixture = module.tab_url_state_fixture()

    assert fixture["passed"] is True
    assert fixture["preserved_build"]["url"] == "/applications/app-1?build=b123&tab=run"
    assert fixture["preserved_build"]["history_method"] == "pushState"
    assert fixture["build_replace"]["url"] == "/applications/app-1?build=b123&tab=build"
    assert fixture["build_replace"]["history_method"] == "replaceState"
    assert fixture["no_duplicate"]["changed"] is False


def test_v03_20_popstate_and_direct_set_tab_guards() -> None:
    module = load_audit_module()
    popstate = module.popstate_tab_guard_fixture()
    direct = module.direct_set_tab_guard()

    assert popstate["passed"] is True
    assert popstate["valid"]["monitor"] is True
    assert popstate["invalid"]["publish"] is False
    assert direct["passed"] is True


def test_v03_20_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["forbidden_endpoint_called"] is False


def test_v03_20_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_20_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"

    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.20"
    assert loaded["status"] == "passed"
