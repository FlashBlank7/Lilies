from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_84_complexity_router_operator_opt_in_rollout.py"
    spec = importlib.util.spec_from_file_location("v02_84_operator_opt_in_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_84_operator_opt_in_rollout_executes_stage_1_without_default_enablement() -> None:
    module = load_module()

    result = module.execute_operator_opt_in_rollout()

    assert result["status"] == "completed"
    assert result["stage"]["stage_id"] == "stage_1_operator_opt_in"
    assert result["stage"]["mode"] == "operator_opt_in"
    assert result["behavior_change"] is False
    assert result["default_enabled"] is False
    assert result["sample_count"] == 3
    assert {sample["operator_opt_in"] for sample in result["sample_results"]} == {True}
    assert {sample["behavior_changed"] for sample in result["sample_results"]} == {False}


def test_v02_84_operator_opt_in_rollout_records_exit_metrics() -> None:
    module = load_module()

    result = module.execute_operator_opt_in_rollout()
    metrics = result["metrics"]

    assert metrics["classification_distribution"] == {"simple": 1, "medium": 1, "complex": 1}
    assert metrics["override_rate"] == 1.0
    assert metrics["override_reason_coverage"] == 1.0
    assert metrics["unexpected_classification_rate"] == 0.0
    assert metrics["accidental_default_enablement_count"] == 0
    assert metrics["behavior_change_count"] == 0
    assert result["exit_criteria"]["override_reason_coverage_at_least_0_95"] is True
    assert result["exit_criteria"]["unexpected_classification_rate_at_most_0_05"] is True
    assert result["exit_criteria"]["post_shadow_decision_selected_operator_opt_in"] is True
