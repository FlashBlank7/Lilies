from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_99_e08_post_studio_controls_decision.py"
    spec = importlib.util.spec_from_file_location("v02_99_e08_post_studio_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_99_selects_operator_runbook_lifecycle() -> None:
    module = load_module()
    result = module.select_candidate()

    assert result["decision"] == "select_operator_runbook_lifecycle"
    assert result["selected_candidate"]["candidate_id"] == "operator_runbook_lifecycle"
    assert result["next_version"] == "v0.2.100_e08_operator_runbook_lifecycle"
    assert result["first_design"] == "docs/current-design/design_v0_2_100_e08_operator_runbook_lifecycle.md"


def test_v02_99_defers_broader_boundary_and_rejects_pause() -> None:
    module = load_module()
    result = module.select_candidate()
    candidates = {item["candidate_id"]: item for item in result["candidates"]}

    assert "defer" in candidates["broader_sidecar_boundary_closure"]["disposition"]
    assert "reject" in candidates["pause_e08_after_studio_controls"]["disposition"]
    assert candidates["broader_sidecar_boundary_closure"]["score"] < result["selected_candidate"]["score"]


def test_v02_99_next_stage_has_runbook_verification_targets() -> None:
    module = load_module()
    result = module.select_candidate()

    assert result["e07_invariant"]["no_e07_code_or_default_change"] is True
    targets = result["v02_100_verification_targets"]
    assert any("runbook checklist" in item for item in targets)
    assert any("rollback" in item for item in targets)
    assert any("Studio editable controls" in item for item in targets)
