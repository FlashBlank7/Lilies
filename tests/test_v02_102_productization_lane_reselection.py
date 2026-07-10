from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_102_productization_lane_reselection.py"
    spec = importlib.util.spec_from_file_location("v02_102_lane_reselection_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_102_selects_e05_scheduled_monitoring_hook() -> None:
    module = load_module()
    result = module.select_lane()

    assert result["decision"] == "select_e05_scheduled_monitoring_hook"
    assert result["selected_lane"]["lane_id"] == "e05_scheduled_monitoring_hook"
    assert result["next_version"] == "v0.2.103_e05_scheduled_monitoring_hook"
    assert result["first_design"] == "docs/current-design/design_v0_2_103_e05_scheduled_monitoring_hook.md"


def test_v02_102_blocked_lanes_are_visible_but_not_selected() -> None:
    module = load_module()
    result = module.select_lane()
    blocked = {item["lane_id"] for item in result["blocked_lanes"]}

    assert {"e02_true_human_panel", "e10_governed_memory_surface"} <= blocked
    assert result["selected_lane"]["lane_id"] not in blocked
    assert all(item["score"] == -1000 for item in result["blocked_lanes"])


def test_v02_102_preserves_e07_and_e08_boundaries() -> None:
    module = load_module()
    result = module.select_lane()
    invariants = result["invariants"]

    assert invariants["task_source"] == "stage_report_next_stage_task_set"
    assert invariants["workingon_is_not_task_source"] is True
    assert invariants["e07_guarded_default_preserved"] is True
    assert invariants["e08_current_tranche_productized"] is True
    assert invariants["e08_full_sidecar_completion_claimed"] is False


def test_v02_102_broad_e08_sidecar_closure_does_not_win() -> None:
    module = load_module()
    result = module.select_lane()
    e08_broad = next(
        item for item in result["candidates"] if item["lane_id"] == "e08_broader_sidecar_boundary_closure"
    )

    assert e08_broad["blocked"] is False
    assert e08_broad["scope_risk"] > result["selected_lane"]["scope_risk"]
    assert result["selected_lane"]["lane_id"] != "e08_broader_sidecar_boundary_closure"


def test_v02_102_summary_states_workingon_is_not_task_source(tmp_path: Path) -> None:
    module = load_module()
    result = module.select_lane()
    _, summary_path = module.write_outputs(result, tmp_path)
    summary = summary_path.read_text(encoding="utf-8")

    assert "Workingon is not task source: `True`" in summary
    assert "Workingon is task source" not in summary
