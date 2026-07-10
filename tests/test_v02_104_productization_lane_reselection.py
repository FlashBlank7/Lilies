from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_104_productization_lane_reselection.py"
    spec = importlib.util.spec_from_file_location("v02_104_lane_reselection_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_104_selects_e08_scope_decomposition() -> None:
    module = load_module()
    result = module.select_lane()

    assert result["decision"] == "select_e08_broader_sidecar_scope_decomposition"
    assert result["selected_lane"]["lane_id"] == "e08_broader_sidecar_scope_decomposition"
    assert result["next_version"] == "v0.2.105_e08_broader_sidecar_scope_decomposition"


def test_v02_104_completed_e05_scheduled_hook_cannot_win() -> None:
    module = load_module()
    result = module.select_lane()
    e05 = next(item for item in result["candidates"] if item["lane_id"] == "e05_scheduled_monitoring_hook")

    assert e05["completion_state"] == "completed_productized"
    assert e05["selectable"] is False
    assert e05["score"] == -1000
    assert result["selected_lane"]["lane_id"] != "e05_scheduled_monitoring_hook"


def test_v02_104_blocked_lanes_cannot_win() -> None:
    module = load_module()
    result = module.select_lane()
    blocked = {item["lane_id"] for item in result["candidates"] if item["completion_state"] == "blocked"}

    assert {"e02_true_human_panel", "e10_governed_memory_surface"} <= blocked
    assert result["selected_lane"]["lane_id"] not in blocked


def test_v02_104_preserves_e08_no_full_completion_claim() -> None:
    module = load_module()
    result = module.select_lane()

    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert result["invariants"]["workingon_is_not_task_source"] is True
