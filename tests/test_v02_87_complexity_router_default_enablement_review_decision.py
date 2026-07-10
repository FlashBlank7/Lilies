from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_87_complexity_router_default_enablement_review_decision.py"
    spec = importlib.util.spec_from_file_location("v02_87_default_review_decision_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_87_decision_enters_default_enablement_review_when_gates_pass() -> None:
    module = load_module()

    decision = module.decide_default_enablement_review()

    assert decision["all_gates_passed"] is True
    assert decision["decision"]["option_id"] == "enter_default_enablement_review"
    assert decision["decision"]["next_version"] == "v0.2.88_complexity_router_limited_default_enablement_plan"
    assert decision["fresh_frontend_verification"]["passed"] is True
    assert decision["default_enabled"] is False
    assert decision["allowed_to_enable_default"] is True


def test_v02_87_decision_rejects_observation_and_deferral() -> None:
    module = load_module()

    decision = module.decide_default_enablement_review()
    options = {option["option_id"]: option for option in decision["options"]}

    assert options["enter_default_enablement_review"]["score"] > options["continue_operator_opt_in_observation"]["score"]
    assert options["enter_default_enablement_review"]["score"] > options["explicit_default_review_deferral"]["score"]
    assert "rejected" in options["continue_operator_opt_in_observation"]["disposition"]
    assert "rejected" in options["explicit_default_review_deferral"]["disposition"]
