from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_21_application_list_url_state.py"
    spec = importlib.util.spec_from_file_location("v03_21_application_list_url_state_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_21_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    checks = module.source_marker_checks()

    assert all(check["passed"] for check in checks)


def test_v03_21_query_parser_guards_values() -> None:
    module = load_audit_module()
    fixture = module.app_list_query_parser_fixture()

    assert fixture["passed"] is True
    assert fixture["valid"] == {"filter": "published", "q": "demo", "sort": "name"}
    assert fixture["invalid"] == {"filter": "all", "q": "demo", "sort": "readiness"}


def test_v03_21_url_writer_uses_expected_history_methods() -> None:
    module = load_audit_module()
    fixture = module.app_list_url_writer_fixture()

    assert fixture["passed"] is True
    assert fixture["filter_push"]["history_method"] == "pushState"
    assert fixture["search_replace"]["history_method"] == "replaceState"
    assert fixture["defaults_removed"]["url"] == "/"
    assert fixture["forbidden_urls"] == []


def test_v03_21_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["forbidden_endpoint_called"] is False


def test_v03_21_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_21_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"

    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.21"
    assert loaded["status"] == "passed"
