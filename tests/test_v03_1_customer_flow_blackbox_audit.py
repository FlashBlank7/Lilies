from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_1_customer_flow_blackbox_audit.py"
    spec = importlib.util.spec_from_file_location("v03_1_customer_flow_blackbox_audit_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_1_customer_flow_audit_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["fixture_count"] >= 4
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["failed_check_count"] == 0


def test_v03_1_customer_fixtures_are_distinct_and_actionable() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    fixtures = evidence["customer_requirement_fixtures"]
    fixture_ids = {fixture["id"] for fixture in fixtures}

    assert {"business_owner", "implementation_consultant", "operator", "technical_reviewer"}.issubset(fixture_ids)
    assert len(fixture_ids) == len(fixtures)
    assert all(len(fixture["requirement"]) >= 120 for fixture in fixtures)
    assert all(fixture["expected_outcome"] for fixture in fixtures)
    assert all(fixture["acceptance_signal"] for fixture in fixtures)


def test_v03_1_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    bug_check = next(check for check in evidence["checks"] if check["id"] == "p0_p1_bug_ledger")

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0
    assert all(item["status"] in {"fixed", "verified_fixed", "deferred_with_reason"} for item in bug_check["bugs"])


def test_v03_1_customer_flow_audit_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    evidence = module.build_evidence(live=False)

    module.write_evidence(output, evidence)
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["status"] == "passed"
    assert loaded["version"] == "v0.3.1"
