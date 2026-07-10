from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_83_complexity_router_post_shadow_rollout_decision.py"
    spec = importlib.util.spec_from_file_location("v02_83_post_shadow_decision_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_83_decision_selects_operator_opt_in_rollout() -> None:
    module = load_module()

    decision = module.decide_post_shadow_rollout()

    assert decision["stage_0_passed"] is True
    assert decision["decision"]["option_id"] == "execute_operator_opt_in_rollout"
    assert decision["decision"]["next_version"] == "v0.2.84_complexity_router_operator_opt_in_rollout"
    assert decision["stage_1"]["stage_id"] == "stage_1_operator_opt_in"
    assert decision["stage_1"]["behavior_change"] is False
    assert decision["default_enabled"] is False


def test_v02_83_decision_rejects_stagnation_and_default_review() -> None:
    module = load_module()

    decision = module.decide_post_shadow_rollout()
    options = {option["option_id"]: option for option in decision["options"]}

    assert options["execute_operator_opt_in_rollout"]["score"] > options["continue_shadow_only_observation"]["score"]
    assert options["execute_operator_opt_in_rollout"]["score"] > options["begin_default_enablement_review"]["score"]
    assert "rejected" in options["continue_shadow_only_observation"]["disposition"]
    assert "frontend verification" in options["begin_default_enablement_review"]["disposition"]
    assert decision["frontend_verification_required_before_default_review"] is True
