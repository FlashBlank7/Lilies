from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_51_canvas_layout_keyboard_navigation.py"
    spec = importlib.util.spec_from_file_location("v03_51_canvas_layout_keyboard_navigation_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_51_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_51_layout_algorithm_produces_readable_columns() -> None:
    module = load_audit_module()
    fixture = module.layout_algorithm_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["start_is_left_of_collect"] is True
    assert fixture["cases"]["layout_uses_stable_spacing"] is True


def test_v03_51_arrange_persists_node_positions() -> None:
    module = load_audit_module()
    fixture = module.layout_persistence_contract_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["arrange_persists_position_changes"] is True
    assert fixture["cases"]["arrange_refits_canvas_after_layout"] is True


def test_v03_51_wasd_pan_contract_maps_keys_to_viewport_motion() -> None:
    module = load_audit_module()
    fixture = module.keyboard_pan_contract_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["w_moves_view_up"] is True
    assert fixture["cases"]["d_moves_view_right"] is True


def test_v03_51_wasd_guard_does_not_interfere_with_text_entry() -> None:
    module = load_audit_module()
    fixture = module.keyboard_guard_contract_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["text_targets_are_ignored"] is True
    assert fixture["cases"]["ctrl_and_meta_are_ignored"] is True


def test_v03_51_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence()
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_51_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_51_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence())
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.51"
    assert loaded["status"] == "passed"
