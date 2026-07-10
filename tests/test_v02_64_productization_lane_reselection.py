from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_selection_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_64_productization_lane_reselection.py"
    spec = importlib.util.spec_from_file_location("v02_64_productization_lane_reselection_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_64_reselection_prefers_e08_controls() -> None:
    module = load_selection_module()

    selection = module.select_lane()
    lanes = {lane["lane_id"]: lane for lane in selection["lanes"]}

    assert set(lanes) == {"e08_extended_controls", "complexity_router_rollout"}
    assert selection["winner"]["lane_id"] == "e08_extended_controls"
    assert selection["next_version"] == "v0.2.65_e08_policy_controls_surface"
    assert selection["first_design"] == "docs/current-design/design_e08_policy_controls_surface.md"
    assert lanes["e08_extended_controls"]["score"] > lanes["complexity_router_rollout"]["score"]


def test_v02_64_reselection_preserves_complexity_router_boundary() -> None:
    module = load_selection_module()

    selection = module.select_lane()
    lanes = {lane["lane_id"]: lane for lane in selection["lanes"]}
    complexity = lanes["complexity_router_rollout"]

    assert complexity["blocked"] is False
    assert complexity["default_risk_blocker"] == 3
    assert "router_ready_for_default=false" in complexity["source_detail"]
    assert selection["deferred"][0]["lane_id"] == "complexity_router_rollout"
