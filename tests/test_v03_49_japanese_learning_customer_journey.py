from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_49_japanese_learning_customer_journey.py"
    spec = importlib.util.spec_from_file_location("v03_49_japanese_learning_customer_journey_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_49_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_49_intake_exposes_japanese_learner_customer_example() -> None:
    module = load_audit_module()
    fixture = module.japanese_learning_intake_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["homepage_has_japanese_learner_role"] is True
    assert fixture["cases"]["acceptance_requires_learner_readable_summary"] is True


def test_v03_49_safe_draft_is_scenario_specific() -> None:
    module = load_audit_module()
    fixture = module.japanese_learning_safe_draft_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["topic_input_is_first_visible_field"] is True
    assert fixture["cases"]["daily_summary_answer_step_is_visible"] is True


def test_v03_49_run_guidance_uses_learning_language() -> None:
    module = load_audit_module()
    fixture = module.japanese_learning_run_guidance_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["topic_field_label_is_customer_language"] is True
    assert fixture["cases"]["progress_steps_are_learning_steps_not_raw_node_ids"] is True


def test_v03_49_result_expectation_is_learner_readable() -> None:
    module = load_audit_module()
    fixture = module.japanese_learning_result_expectation_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["checklist_includes_meaning_examples_tone"] is True
    assert fixture["cases"]["raw_json_does_not_replace_learner_expectation"] is True


def test_v03_49_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_49_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_49_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.49"
    assert loaded["status"] == "passed"
