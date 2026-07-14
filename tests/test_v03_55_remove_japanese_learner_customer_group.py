from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_55_remove_japanese_learner_customer_group.py"
    spec = importlib.util.spec_from_file_location("v03_55_remove_japanese_learner_customer_group_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_55_customer_group_removal_passes() -> None:
    module = load_audit_module()
    check = module.customer_group_removal_checks()
    assert check["passed"] is True
    assert check["cases"]["zh_customer_scenarios_do_not_include_japanese_learner"] is True
    assert check["cases"]["en_customer_examples_do_not_include_japanese_student"] is True


def test_v03_55_explicit_japanese_workflow_support_is_preserved() -> None:
    module = load_audit_module()
    check = module.explicit_japanese_workflow_support_preserved_checks()
    assert check["passed"] is True
    assert check["cases"]["typed_requirement_detector_remains"] is True
    assert check["cases"]["safe_draft_seed_remains_for_explicit_requirement"] is True


def test_v03_55_v0349_supersession_checks_pass() -> None:
    module = load_audit_module()
    check = module.v0349_supersession_checks()
    assert check["passed"] is True
    assert check["cases"]["v0349_tests_expect_removal"] is True
    assert check["cases"]["old_presence_assertion_removed"] is True


def test_v03_55_static_evidence_passes_and_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    evidence = module.build_evidence()
    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    output = tmp_path / "evidence.json"
    module.write_evidence(output, evidence)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.55"
    assert loaded["status"] == "passed"
