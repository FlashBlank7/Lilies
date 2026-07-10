from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_75_complexity_router_default_enablement_boundary.py"
    spec = importlib.util.spec_from_file_location("v02_75_enablement_boundary_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_75_decision_requires_live_validation_before_default_change() -> None:
    module = load_module()

    decision = module.decide_enablement_boundary()

    assert decision["decision"]["option_id"] == "require_live_validation_before_default_change"
    assert decision["decision"]["next_version"] == "v0.2.76_complexity_router_live_validation_plan"
    assert decision["allowed_to_enable_default"] is True
    assert decision["default_enabled"] is False


def test_v02_75_decision_rejects_immediate_enablement_review() -> None:
    module = load_module()

    decision = module.decide_enablement_boundary()
    options = {option["option_id"]: option for option in decision["options"]}

    assert options["require_live_validation_before_default_change"]["score"] > options["enter_enablement_review_now"]["score"]
    assert "deferred" in options["enter_enablement_review_now"]["disposition"]
