from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_e08_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "e08_harness_sidecar_passmode_experiment.py"
    spec = importlib.util.spec_from_file_location("e08_harness_sidecar_passmode_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_e08_runner_classifies_soft_passmode_and_sidecar_boundary() -> None:
    module = load_e08_module()

    result = module.build_result()

    assert result["status"] == "completed"
    scenarios = {item["name"]: item for item in result["scenarios"]}
    assert scenarios["workflow_internal_permission_pause"]["status"] == "paused"
    assert scenarios["workflow_internal_permission_pause"]["enforcement_strength"] == "soft_pause"
    assert scenarios["workflow_internal_permission_pause"]["bypassable_by_workflow_config"] is True
    assert "permission.requested" in scenarios["workflow_internal_permission_pause"]["observability"]
    assert scenarios["workflow_internal_permission_auto_approve"]["status"] == "succeeded"
    assert scenarios["workflow_internal_permission_auto_approve"]["enforcement_strength"] == "soft_pass"
    assert scenarios["platform_sidecar_network_block"]["status"] == "failed"
    assert scenarios["platform_sidecar_network_block"]["enforcement_strength"] == "hard_block"
    assert scenarios["platform_sidecar_network_block"]["bypassable_by_workflow_config"] is False
    assert "network egress policy blocked" in scenarios["platform_sidecar_network_block"]["error"]
    assert "not a substitute" not in result["conclusion"].casefold()
