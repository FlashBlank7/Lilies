from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_decision_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_69_e08_continuation_decision.py"
    spec = importlib.util.spec_from_file_location("v02_69_e08_continuation_decision_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_69_decision_pauses_e08_and_moves_to_complexity_router() -> None:
    module = load_decision_module()

    decision = module.decide()

    assert decision["decision"]["option_id"] == "pause_e08_move_complexity_router"
    assert decision["decision"]["next_version"] == "v0.2.70_complexity_router_guardrail_selection"
    assert decision["decision"]["first_design"] == "docs/current-design/design_complexity_router_guardrail_selection.md"


def test_v02_69_rejects_false_full_sidecar_completion() -> None:
    module = load_decision_module()

    decision = module.decide()
    options = {option["option_id"]: option for option in decision["options"]}

    assert "rejected" in options["declare_full_sidecar_complete"]["disposition"]
    assert options["declare_full_sidecar_complete"]["score"] < options["pause_e08_move_complexity_router"]["score"]
    assert "deferred" in options["continue_e08_editable_policy_controls"]["disposition"]
    assert "deferred" in options["continue_e08_operator_runbook"]["disposition"]
