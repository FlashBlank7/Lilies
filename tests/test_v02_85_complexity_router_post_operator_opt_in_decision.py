from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_85_complexity_router_post_operator_opt_in_decision.py"
    spec = importlib.util.spec_from_file_location("v02_85_post_operator_decision_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_85_decision_selects_frontend_verification_environment_repair() -> None:
    module = load_module()

    decision = module.decide_post_operator_opt_in_path()

    assert decision["stage_1_passed"] is True
    assert decision["decision"]["option_id"] == "repair_frontend_verification_environment"
    assert decision["decision"]["next_version"] == "v0.2.86_frontend_verification_environment_repair"
    assert decision["frontend_environment"]["package_json_present"] is True
    assert decision["frontend_environment"]["node_modules_present"] is True
    assert decision["default_enabled"] is False


def test_v02_85_decision_rejects_default_review_until_frontend_verification_exists() -> None:
    module = load_module()

    decision = module.decide_post_operator_opt_in_path()
    options = {option["option_id"]: option for option in decision["options"]}

    assert options["repair_frontend_verification_environment"]["score"] > options["continue_operator_opt_in_observation"]["score"]
    assert options["repair_frontend_verification_environment"]["score"] > options["begin_default_enablement_review"]["score"]
    assert "rejected" in options["begin_default_enablement_review"]["disposition"]
    assert decision["default_enablement_review_deferred_until_frontend_verification"] is True
