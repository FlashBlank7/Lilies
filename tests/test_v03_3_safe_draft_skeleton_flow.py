from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_3_safe_draft_skeleton_flow.py"
    spec = importlib.util.spec_from_file_location("v03_3_safe_draft_skeleton_flow_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_3_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["build_endpoint_called"] is False


def test_v03_3_skeleton_operations_define_visible_structure() -> None:
    module = load_audit_module()
    operations = module.skeleton_operations("test")
    op_names = [op for op, _ in operations]
    nodes = [data["node"] for op, data in operations if op == "add_node"]
    tests = [data["test"] for op, data in operations if op == "add_test"]

    assert op_names == ["add_node", "add_node", "add_edge", "add_test"]
    assert {node["type"] for node in nodes} == {"start", "answer"}
    assert tests and tests[0]["structural_only"] is True


def test_v03_3_retention_policy_is_explicit() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["retention_policy"]["marker"] == module.SMOKE_MARKER
    assert "delete/archive" in evidence["retention_policy"]["reason"]


def test_v03_3_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    bug_check = next(check for check in evidence["checks"] if check["id"] == "p0_p1_bug_ledger_safe_draft_skeleton")

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_3_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"

    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.3"
    assert loaded["status"] == "passed"
