from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_97_e08_post_api_productization_decision.py"
    spec = importlib.util.spec_from_file_location("v02_97_e08_path_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_97_selects_studio_editable_policy_controls() -> None:
    module = load_module()
    result = module.select_path()

    assert result["decision"] == "select_studio_editable_policy_controls"
    assert result["selected_path"]["path_id"] == "studio_editable_policy_controls"
    assert result["next_version"] == "v0.2.98_e08_studio_editable_policy_controls"
    assert result["first_design"] == "docs/current-design/design_v0_2_98_e08_studio_editable_policy_controls.md"


def test_v02_97_defers_runbook_and_broader_boundary() -> None:
    module = load_module()
    result = module.select_path()
    candidates = {item["path_id"]: item for item in result["candidates"]}

    assert "defer" in candidates["operator_runbook_lifecycle"]["disposition"]
    assert "too broad" in candidates["broader_sidecar_boundary_closure"]["disposition"]
    assert "reject" in candidates["pause_e08_after_api"]["disposition"]
    assert candidates["broader_sidecar_boundary_closure"]["score"] < result["selected_path"]["score"]


def test_v02_97_next_stage_has_verification_targets_and_e07_invariant() -> None:
    module = load_module()
    result = module.select_path()

    assert result["e07_invariant"]["no_e07_code_or_default_change"] is True
    targets = result["v02_98_verification_targets"]
    assert any("frontend" in item for item in targets)
    assert any("browser" in item or "executable" in item for item in targets)
    assert any("backend policy-controls" in item for item in targets)
