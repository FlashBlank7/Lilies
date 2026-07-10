from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_76_complexity_router_live_validation_plan.py"
    spec = importlib.util.spec_from_file_location("v02_76_live_validation_plan_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_76_plan_defines_cases_budget_and_default_disabled_status() -> None:
    module = load_module()

    plan = module.build_plan()

    assert plan["execution_in_this_stage"] is False
    assert plan["default_enabled"] is False
    assert plan["allowed_to_enable_default"] is True
    assert plan["budget_boundary"]["max_live_cases"] == 3
    assert [case["expected_class"] for case in plan["validation_cases"]] == ["simple", "medium", "complex"]


def test_v02_76_plan_has_metrics_and_pass_fail_criteria() -> None:
    module = load_module()

    plan = module.build_plan()

    assert "classification_distribution" in plan["metrics_capture"]
    assert "required metrics captured" in plan["pass_criteria"]
    assert "accidental default enablement" in plan["fail_criteria"]
