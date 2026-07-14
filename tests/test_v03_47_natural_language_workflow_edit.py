from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_47_natural_language_workflow_edit.py"
    spec = importlib.util.spec_from_file_location("v03_47_natural_language_workflow_edit_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_47_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_47_surface_is_workflow_level_not_draft_patch() -> None:
    module = load_audit_module()
    fixture = module.workflow_edit_surface_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["feature_name_is_workflow_editing"] is True
    assert fixture["cases"]["edit_dialog_is_whole_workflow"] is True
    assert fixture["cases"]["readable_summary_exists_before_json"] is True


def test_v03_47_reference_bricks_are_context_not_scope_limit() -> None:
    module = load_audit_module()
    fixture = module.workflow_reference_context_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["references_are_sent_to_preview_api"] is True
    assert fixture["cases"]["references_do_not_limit_edit_scope"] is True


def test_v03_47_preview_supports_workflow_level_intents() -> None:
    module = load_audit_module()
    fixture = module.workflow_level_preview_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["whole_workflow_update_supported"] is True
    assert fixture["cases"]["reference_ids_are_validated"] is True
    assert fixture["cases"]["start_input_update_supported"] is True
    assert fixture["cases"]["legacy_node_rename_still_supported"] is True


def test_v03_47_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_47_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_47_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.47"
    assert loaded["status"] == "passed"
