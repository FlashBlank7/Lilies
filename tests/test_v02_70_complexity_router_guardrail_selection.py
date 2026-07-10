from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_selection_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_70_complexity_router_guardrail_selection.py"
    spec = importlib.util.spec_from_file_location("v02_70_complexity_router_guardrail_selection_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_70_selects_default_safety_gate_first() -> None:
    module = load_selection_module()

    selection = module.select_guardrail()

    assert selection["winner"]["candidate_id"] == "default_safety_gate"
    assert selection["winner"]["next_version"] == "v0.2.71_complexity_router_default_safety_gate"
    assert selection["router_ready_for_default"] is False


def test_v02_70_preserves_supporting_guardrails() -> None:
    module = load_selection_module()

    selection = module.select_guardrail()
    candidates = {candidate["candidate_id"]: candidate for candidate in selection["candidates"]}

    assert "deferred" in candidates["requirement_classification_contract"]["disposition"]
    assert "deferred" in candidates["override_controls"]["disposition"]
    assert "deferred" in candidates["rollout_metrics"]["disposition"]
