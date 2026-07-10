from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_selection_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_productization_lane_selection.py"
    spec = importlib.util.spec_from_file_location("v02_productization_lane_selection_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_productization_lane_selection_prefers_monitoring_surface() -> None:
    module = load_selection_module()

    selection = module.select_lane()
    lanes = {lane["lane_id"]: lane for lane in selection["lanes"]}

    assert set(lanes) == {
        "adaptive_monitoring_product_surface",
        "complexity_router_rollout",
        "e08_extended_controls",
        "human_panel",
        "governed_memory_surface",
    }
    assert selection["winner"]["lane_id"] == "adaptive_monitoring_product_surface"
    assert selection["next_version"] == "v0.2.60_adaptive_monitoring_product_surface"
    assert lanes["human_panel"]["blocked"] is True
    assert lanes["governed_memory_surface"]["blocked"] is True
    assert lanes["adaptive_monitoring_product_surface"]["score"] > lanes["e08_extended_controls"]["score"]
    assert lanes["complexity_router_rollout"]["safety_governance_blocker"] > 0
