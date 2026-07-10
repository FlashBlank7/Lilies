from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_matrix_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_66_e08_control_behavior_matrix.py"
    spec = importlib.util.spec_from_file_location("v02_66_e08_control_behavior_matrix_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_66_e08_control_behavior_matrix_covers_expected_controls() -> None:
    module = load_matrix_module()

    matrix = module.build_matrix()
    rows = {row["id"]: row for row in matrix["behavior_matrix"]}

    assert matrix["not_full_sidecar_completion"] is True
    assert rows["workflow_passmode"]["enforcement"] == "soft_configurable"
    assert rows["cancellation_checkpoint"]["enforcement"] == "soft_checkpoint"
    assert rows["budget_limits"]["enforcement"] == "hard_counter"
    assert rows["worker_lease"]["status"] == "enabled"
    assert rows["network_egress_policy"]["status"] == "restricted"
    assert rows["secret_policy"]["status"] == "enabled"
