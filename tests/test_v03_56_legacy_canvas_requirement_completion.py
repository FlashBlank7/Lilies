from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_56_legacy_canvas_requirement_completion.py"
    spec = importlib.util.spec_from_file_location("v03_56_legacy_canvas_requirement_completion_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_56_legacy_canvas_compatibility_markers_pass() -> None:
    module = load_audit_module()
    check = module.legacy_canvas_compatibility_markers()
    assert check["passed"] is True
    assert check["cases"]["raw_block_type_replace_removed"] is True
    assert check["cases"]["sync_canvas_uses_sanitized_data"] is True
    assert check["cases"]["drag_persistence_uses_safe_position"] is True


def test_v03_56_requirement_completion_markers_pass() -> None:
    module = load_audit_module()
    check = module.requirement_completion_markers()
    assert check["passed"] is True
    assert check["cases"]["local_question_builder_replaced"] is True
    assert check["cases"]["local_plan_builder_replaced"] is True
    assert check["cases"]["ai_endpoint_is_called"] is True
    assert check["cases"]["apply_writes_back_to_requirement"] is True
    assert check["cases"]["apply_waits_for_ai_ready"] is True


def test_v03_56_claude_plan_reference_boundary_passes() -> None:
    module = load_audit_module()
    check = module.claude_plan_reference_boundary_markers()
    assert check["passed"] is True
    assert check["cases"]["reference_has_needs_input_phase"] is True
    assert check["cases"]["lilies_does_not_copy_exit_plan_mode_markers"] is True
    assert check["cases"]["no_remote_session_dependency"] is True


def test_v03_56_static_evidence_passes_and_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    evidence = module.build_evidence()
    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    output = tmp_path / "evidence.json"
    module.write_evidence(output, evidence)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.56"
    assert loaded["status"] == "passed"
