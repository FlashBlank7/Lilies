from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_80_complexity_router_staged_rollout_preparation.py"
    spec = importlib.util.spec_from_file_location("v02_80_rollout_preparation_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_80_rollout_preparation_defines_three_non_default_stages() -> None:
    module = load_module()

    plan = module.build_rollout_preparation()

    assert plan["default_enabled"] is False
    assert plan["allowed_to_enable_default"] is True
    assert [stage["stage_id"] for stage in plan["rollout_stages"]] == [
        "stage_0_shadow_only",
        "stage_1_operator_opt_in",
        "stage_2_limited_default_review",
    ]
    assert {stage["behavior_change"] for stage in plan["rollout_stages"]} == {False}


def test_v02_80_rollout_preparation_has_controls_and_rollback() -> None:
    module = load_module()

    plan = module.build_rollout_preparation()

    assert "force_complex_with_reason" in plan["operator_controls"]
    assert "rollback_to_shadow_only" in plan["operator_controls"]
    assert "any_accidental_default_enablement" in plan["rollback_criteria"]
