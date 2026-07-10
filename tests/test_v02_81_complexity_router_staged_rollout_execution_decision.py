from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_81_complexity_router_staged_rollout_execution_decision.py"
    spec = importlib.util.spec_from_file_location("v02_81_rollout_decision_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_81_decision_selects_shadow_only_rollout() -> None:
    module = load_module()

    decision = module.decide_rollout_execution()

    assert decision["decision"]["option_id"] == "execute_shadow_only_rollout"
    assert decision["decision"]["next_version"] == "v0.2.82_complexity_router_shadow_only_rollout"
    assert decision["first_stage"]["stage_id"] == "stage_0_shadow_only"
    assert decision["first_stage"]["behavior_change"] is False
    assert decision["default_enabled"] is False


def test_v02_81_decision_rejects_more_docs_and_deferral() -> None:
    module = load_module()

    decision = module.decide_rollout_execution()
    options = {option["option_id"]: option for option in decision["options"]}

    assert options["execute_shadow_only_rollout"]["score"] > options["prepare_more_rollout_docs"]["score"]
    assert options["execute_shadow_only_rollout"]["score"] > options["defer_rollout_execution"]["score"]
    assert "rejected" in options["prepare_more_rollout_docs"]["disposition"]
    assert "rejected" in options["defer_rollout_execution"]["disposition"]
