from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_0_usability_customer_journey_audit.py"
    spec = importlib.util.spec_from_file_location("v03_0_usability_audit_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_0_customer_journey_audit_passes_required_surfaces() -> None:
    module = load_audit_module()
    evidence = module.build_evidence()

    assert evidence["status"] == "passed"
    assert evidence["summary"]["persona_count"] >= 4
    assert evidence["summary"]["journey_count"] >= 4
    assert evidence["summary"]["frontdoor_journey_present"] is True
    assert evidence["summary"]["draft_canvas_journey_present"] is True
    assert evidence["summary"]["failed_check_count"] == 0


def test_v03_0_customer_journey_audit_has_distinct_customer_behaviors() -> None:
    module = load_audit_module()
    evidence = module.build_evidence()
    personas = {persona["id"] for persona in evidence["customer_personas"]}

    assert {"business_owner", "implementation_consultant", "operator", "technical_reviewer"}.issubset(personas)
    assert "customer-section" in evidence["journey_surface_index"]
    assert "draft-readiness" in evidence["journey_surface_index"]
    assert "bug-triage-panel" in evidence["journey_surface_index"]


def test_v03_0_customer_journey_audit_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    evidence = module.build_evidence()

    module.write_evidence(output, evidence)
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["status"] == "passed"
    assert loaded["version"] == "v0.3.0"
