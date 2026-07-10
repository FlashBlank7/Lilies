from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "v02_105_e08_broader_sidecar_scope_decomposition.py"
    )
    spec = importlib.util.spec_from_file_location("v02_105_e08_scope_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_105_selects_stdio_container_egress_allowlist_contract() -> None:
    module = load_module()
    result = module.decompose()

    assert result["decision"] == "select_stdio_container_egress_allowlist_contract"
    assert result["selected_slice"]["slice_id"] == "stdio_container_egress_allowlist_contract"
    assert result["next_version"] == "v0.2.106_e08_stdio_container_egress_allowlist_contract"


def test_v02_105_does_not_duplicate_completed_current_tranche() -> None:
    module = load_module()
    result = module.decompose()
    completed = {item["capability"] for item in result["completed_current_tranche"]}

    assert "editable_policy_controls_api" in completed
    assert "studio_editable_policy_controls" in completed
    assert "operator_runbook_lifecycle" in completed
    assert result["selected_slice"]["slice_id"] not in completed
    assert result["invariants"]["current_tranche_not_duplicated"] is True


def test_v02_105_preserves_no_full_sidecar_completion_claim() -> None:
    module = load_module()
    result = module.decompose()

    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert result["invariants"]["workingon_is_not_task_source"] is True


def test_v02_105_keeps_non_e08_boundaries() -> None:
    module = load_module()
    result = module.decompose()
    invariants = result["invariants"]

    assert invariants["e05_scheduled_hook_productized"] is True
    assert invariants["e07_guarded_default_productized"] is True
    assert invariants["e02_true_human_panel_blocked"] is True
    assert invariants["e10_governed_memory_blocked"] is True
