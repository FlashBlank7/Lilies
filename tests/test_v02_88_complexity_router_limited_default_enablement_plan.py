from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_88_complexity_router_limited_default_enablement_plan.py"
    spec = importlib.util.spec_from_file_location("v02_88_limited_default_plan_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_88_plan_defines_limited_default_contract_without_enabling_runtime_default() -> None:
    module = load_module()

    plan = module.build_limited_default_enablement_plan()

    assert plan["implementation_in_this_version"] is False
    assert plan["default_enabled"] is False
    assert plan["allowed_to_enable_default"] is True
    assert plan["limited_default_scope"]["mode"] == "limited_default"
    assert plan["config_contract"]["runtime_default"] == "disabled"
    assert plan["config_contract"]["rollback_value"] == "disabled"
    assert plan["next_implementation_target"] == "v0.2.89_complexity_router_limited_default_enablement_contract"


def test_v02_88_plan_has_controls_rollback_and_verification_gates() -> None:
    module = load_module()

    plan = module.build_limited_default_enablement_plan()

    fields = {field["name"]: field for field in plan["config_contract"]["settings_fields_to_add"]}
    assert fields["complexity_router_default_mode"]["default"] == "disabled"
    assert "limited_default" in fields["complexity_router_default_mode"]["allowed_values"]
    assert "rollback_to_disabled_default" in plan["api_contract"]["operator_visible_controls"]
    assert "frontend_verification_failure" in plan["rollback"]["rollback_triggers"]
    assert plan["verification_gates"] == {
        "default_review_selected": True,
        "default_safety_allowed": True,
        "fresh_frontend_verification_passed": True,
        "runtime_default_still_disabled": True,
    }
