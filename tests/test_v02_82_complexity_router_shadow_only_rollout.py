from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_82_complexity_router_shadow_only_rollout.py"
    spec = importlib.util.spec_from_file_location("v02_82_shadow_rollout_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_82_shadow_only_rollout_executes_stage_0_without_behavior_change() -> None:
    module = load_module()

    result = module.execute_shadow_only_rollout()

    assert result["status"] == "completed"
    assert result["stage"]["stage_id"] == "stage_0_shadow_only"
    assert result["stage"]["mode"] == "shadow_only"
    assert result["behavior_change"] is False
    assert result["default_enabled"] is False
    assert result["sample_count"] == 3
    assert {sample["behavior_changed"] for sample in result["sample_results"]} == {False}


def test_v02_82_shadow_only_rollout_records_metrics_and_exit_criteria() -> None:
    module = load_module()

    result = module.execute_shadow_only_rollout()
    metrics = result["metrics"]

    assert metrics["classification_distribution"] == {"simple": 1, "medium": 1, "complex": 1}
    assert metrics["fallback_unknown_rate"] == 0.0
    assert metrics["unexpected_classification_rate"] == 0.0
    assert metrics["accidental_default_enablement_count"] == 0
    assert metrics["behavior_change_count"] == 0
    assert result["exit_criteria"]["classification_distribution_recorded"] is True
    assert result["exit_criteria"]["no_accidental_default_enablement"] is True
