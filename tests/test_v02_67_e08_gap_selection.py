from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_selection_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_67_e08_gap_selection.py"
    spec = importlib.util.spec_from_file_location("v02_67_e08_gap_selection_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_67_selects_cancellation_budget_behavior() -> None:
    module = load_selection_module()

    selection = module.select_gap()

    assert selection["winner"]["slice_id"] == "cancellation_budget_live_behavior"
    assert selection["winner"]["next_version"] == "v0.2.68_e08_cancellation_budget_behavior"
    assert selection["winner"]["first_design"] == "docs/current-design/design_e08_cancellation_budget_behavior.md"


def test_v02_67_preserves_non_winning_dispositions() -> None:
    module = load_selection_module()

    selection = module.select_gap()
    slices = {item["slice_id"]: item for item in selection["slices"]}

    assert "deferred" in slices["editable_policy_controls"]["disposition"]
    assert "deferred" in slices["operator_runbook_lifecycle"]["disposition"]
    assert "rejected" in slices["stop_e08_productization"]["disposition"]
