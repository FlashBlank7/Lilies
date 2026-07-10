from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_77_complexity_router_live_validation_execution_decision.py"
    spec = importlib.util.spec_from_file_location("v02_77_live_validation_decision_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_77_decision_selects_bounded_live_validation_next() -> None:
    module = load_module()

    decision = module.decide_live_validation_execution()

    assert decision["decision"]["option_id"] == "execute_bounded_live_validation"
    assert decision["decision"]["next_version"] == "v0.2.78_complexity_router_bounded_live_validation"
    assert decision["default_enabled"] is False
    assert decision["allowed_to_enable_default"] is True
    assert decision["plan_summary"]["case_count"] == 3
    assert decision["plan_summary"]["max_live_cases"] == 3


def test_v02_77_decision_rejects_dry_run_and_deferral() -> None:
    module = load_module()

    decision = module.decide_live_validation_execution()
    options = {option["option_id"]: option for option in decision["options"]}

    assert options["execute_bounded_live_validation"]["score"] > options["prepare_additional_dry_run"]["score"]
    assert options["execute_bounded_live_validation"]["score"] > options["defer_live_validation"]["score"]
    assert "rejected" in options["prepare_additional_dry_run"]["disposition"]
    assert "rejected" in options["defer_live_validation"]["disposition"]
