from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "v02_103_e05_scheduled_monitoring_hook_verification.py"
    )
    spec = importlib.util.spec_from_file_location("v02_103_e05_monitoring_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_103_verifies_existing_scheduled_monitoring_contract() -> None:
    module = load_module()
    result = module.verify()

    assert result["status"] == "verified_existing_product_capability"
    assert result["implementation_origin"] == "v0.2.63_adaptive_monitoring_schedule_and_report_audit"
    assert result["new_backend_implementation_required"] is False
    assert all(result["checks"].values())


def test_v02_103_preserves_manual_refresh_and_override_controls() -> None:
    module = load_module()
    result = module.verify()
    invariants = result["invariants"]

    assert invariants["manual_refresh_preserved"] is True
    assert invariants["fixed_depth_overrides_visible"] is True
    assert result["contract"]["current_status_after_run"]["available_overrides"] == [
        "adaptive",
        "deep",
        "none",
        "shallow",
    ]


def test_v02_103_records_boundary_invariants() -> None:
    module = load_module()
    result = module.verify()
    invariants = result["invariants"]

    assert invariants["e07_guarded_default_preserved"] is True
    assert invariants["e08_full_sidecar_completion_claimed"] is False
    assert invariants["e02_true_human_panel_blocked"] is True
    assert invariants["e10_governed_memory_blocked"] is True
    assert invariants["workingon_is_not_task_source"] is True


def test_v02_103_summary_states_existing_implementation(tmp_path: Path) -> None:
    module = load_module()
    result = module.verify()
    _, summary_path = module.write_outputs(result, tmp_path)
    summary = summary_path.read_text(encoding="utf-8")

    assert "New backend implementation required: `False`" in summary
    assert "Workingon is not task source: `True`" in summary
