from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_101_e08_post_runbook_disposition.py"
    spec = importlib.util.spec_from_file_location("v02_101_e08_disposition_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_101_pauses_e08_and_reselects_lane() -> None:
    module = load_module()
    result = module.select_disposition()

    assert result["decision"] == "pause_e08_and_reselect_productization_lane"
    assert result["selected_disposition"]["candidate_id"] == "pause_e08_and_reselect_lane"
    assert result["next_version"] == "v0.2.102_productization_lane_reselection"
    assert result["first_design"] == "docs/current-design/design_v0_2_102_productization_lane_reselection.md"


def test_v02_101_preserves_full_sidecar_boundary() -> None:
    module = load_module()
    result = module.select_disposition()
    candidates = {item["candidate_id"]: item for item in result["candidates"]}

    assert result["e08_current_tranche"]["status"] == "productized_without_full_sidecar_completion"
    assert "broader sidecar boundary closure remains deferred" == result["e08_current_tranche"]["remaining_boundary"]
    assert "defer" in candidates["broader_sidecar_boundary_closure_now"]["disposition"]
    assert candidates["broader_sidecar_boundary_closure_now"]["score"] < result["selected_disposition"]["score"]


def test_v02_101_records_e07_invariant() -> None:
    module = load_module()
    result = module.select_disposition()

    assert result["e07_invariant"]["no_e07_code_or_default_change"] is True
