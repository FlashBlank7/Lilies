from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_15_regression_suite_lane_guard.py"
    spec = importlib.util.spec_from_file_location("v03_15_regression_suite_lane_guard_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_15_manifest_and_source_markers_pass() -> None:
    module = load_audit_module()

    checks = [*module.source_marker_checks(), *module.manifest_checks()]

    assert all(check["passed"] for check in checks)


def test_v03_15_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["build_endpoint_called"] is False
    assert evidence["current_gate"]["expected"]["pass_count"] == 78
    assert evidence["diagnostic_lane"]["status"] == "diagnostic_non_gating"


def test_v03_15_unknown_historical_failures_are_blocking() -> None:
    module = load_audit_module()
    manifest = module.load_manifest()

    result = module.classify_full_sweep_failures(["tests/test_unknown.py::test_new_failure"], manifest)

    assert result["blocking"] is True
    assert result["unknown_count"] == 1


def test_v03_15_known_historical_failures_are_non_blocking() -> None:
    module = load_audit_module()
    manifest = module.load_manifest()
    known = sorted(module.known_failure_nodeids(manifest))

    result = module.classify_full_sweep_failures(known, manifest)

    assert result["blocking"] is False
    assert result["known_count"] == 25
    assert result["unknown_count"] == 0


def test_v03_15_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"

    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.15"
    assert loaded["status"] == "passed"
