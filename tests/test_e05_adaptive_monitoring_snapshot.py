from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_monitor_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "e05_adaptive_monitoring_snapshot.py"
    spec = importlib.util.spec_from_file_location("e05_adaptive_monitoring_snapshot_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_adaptive_monitoring_snapshot_has_expected_coverage_and_no_critical_alerts() -> None:
    module = load_monitor_module()

    snapshot = module.build_snapshot()

    assert snapshot["status"] == "completed"
    cases = {(case["family"], case["mode"]): case for case in snapshot["cases"]}
    assert ("data_analyzer", "adaptive_explicit") in cases
    assert ("code_review", "adaptive_explicit") in cases
    assert ("data_analyzer", "policy_default") in cases
    assert cases[("data_analyzer", "policy_default")]["build_status"] == "published"
    assert cases[("data_analyzer", "policy_default")]["effective_depth"] == "deep"
    assert snapshot["critical_alerts"] == []
    assert snapshot["override_options_visible"] is True
