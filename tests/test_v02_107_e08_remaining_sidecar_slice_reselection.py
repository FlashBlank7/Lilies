from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "v02_107_e08_remaining_sidecar_slice_reselection.py"
    )
    spec = importlib.util.spec_from_file_location("v02_107_e08_slice_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_107_selects_secret_kms_rotation_contract() -> None:
    module = load_module()
    result = module.select_slice()

    assert result["decision"] == "select_secret_kms_rotation_contract"
    assert result["selected_slice"]["slice_id"] == "secret_kms_rotation_contract"
    assert result["next_version"] == "v0.2.108_e08_secret_kms_rotation_contract"


def test_v02_107_completed_stdio_slice_is_visible_but_excluded() -> None:
    module = load_module()
    result = module.select_slice()
    completed = {item["slice_id"] for item in result["completed_slices"]}

    assert "stdio_container_egress_allowlist_contract" in completed
    assert result["selected_slice"]["slice_id"] not in completed
    assert result["invariants"]["completed_stdio_slice_excluded"] is True


def test_v02_107_preserves_no_full_sidecar_completion_claim() -> None:
    module = load_module()
    result = module.select_slice()

    assert result["invariants"]["e08_full_sidecar_completion_claimed"] is False
    assert result["invariants"]["workingon_is_not_task_source"] is True
