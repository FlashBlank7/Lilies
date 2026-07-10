from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_94_productization_lane_reselection.py"
    spec = importlib.util.spec_from_file_location("v02_94_lane_reselection_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_94_selects_e08_as_next_unblocked_productization_lane() -> None:
    module = load_module()
    result = module.select_lane()

    assert result["decision"] == "select_e08_followup_controls"
    assert result["selected_lane"]["lane_id"] == "e08_followup_controls"
    assert result["next_version"] == "v0.2.95_e08_followup_controls_scope"
    assert result["e07_invariant"]["status"] == "guarded_default_rollout_implemented"


def test_v02_94_blocked_lanes_are_not_selected() -> None:
    module = load_module()
    result = module.select_lane()
    blocked = {
        item["lane_id"]
        for item in result["candidates"]
        if item["blocked"]
    }

    assert result["selected_lane"]["lane_id"] not in blocked
    assert {"e02_true_human_panel", "e10_governed_memory_surface"} <= blocked
