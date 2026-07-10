from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_95_e08_followup_controls_scope.py"
    spec = importlib.util.spec_from_file_location("v02_95_e08_scope_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_95_selects_editable_policy_controls_api() -> None:
    module = load_module()
    result = module.select_scope()

    assert result["decision"] == "select_editable_policy_controls_api"
    assert result["selected_slice"]["slice_id"] == "editable_policy_controls_api"
    assert result["next_version"] == "v0.2.96_e08_editable_policy_controls_api"
    assert result["first_design"] == "docs/current-design/design_v0_2_96_e08_editable_policy_controls_api.md"
    assert result["e07_invariant"]["no_e07_code_or_default_change"] is True


def test_v02_95_does_not_reselect_closed_or_blocked_e08_slices() -> None:
    module = load_module()
    result = module.select_scope()

    closed_or_blocked = {
        item["slice_id"]
        for item in result["candidates"]
        if item["already_closed"] or item["blocked"]
    }
    assert result["selected_slice"]["slice_id"] not in closed_or_blocked
    assert "cancellation_budget_behavior_repeat" in closed_or_blocked
    assert "worker_lease_behavior_repeat" in closed_or_blocked
    assert "full_sidecar_completion_claim" in closed_or_blocked


def test_v02_95_next_version_has_executable_verification_targets() -> None:
    module = load_module()
    result = module.select_scope()

    targets = result["v02_96_verification_targets"]
    assert any("backend API" in item for item in targets)
    assert any("rejection tests" in item for item in targets)
    assert any("before/after" in item for item in targets)
    assert any("E07 guarded default" in item for item in targets)
