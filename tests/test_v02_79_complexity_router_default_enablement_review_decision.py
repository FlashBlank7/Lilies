from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_79_complexity_router_default_enablement_review_decision.py"
    spec = importlib.util.spec_from_file_location("v02_79_enablement_review_decision_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_79_decision_selects_staged_rollout_preparation() -> None:
    module = load_module()

    decision = module.decide_enablement_review()

    assert decision["decision"]["option_id"] == "prepare_staged_rollout"
    assert decision["decision"]["next_version"] == "v0.2.80_complexity_router_staged_rollout_preparation"
    assert decision["live_evidence"]["passed"] is True
    assert decision["live_evidence"]["case_count"] == 3
    assert decision["default_enabled"] is False
    assert decision["allowed_to_enable_default"] is True


def test_v02_79_decision_defers_immediate_enablement_review() -> None:
    module = load_module()

    decision = module.decide_enablement_review()
    options = {option["option_id"]: option for option in decision["options"]}

    assert options["prepare_staged_rollout"]["score"] > options["enter_immediate_enablement_review"]["score"]
    assert "deferred" in options["enter_immediate_enablement_review"]["disposition"]
    assert "rejected" in options["continue_deferral"]["disposition"]
