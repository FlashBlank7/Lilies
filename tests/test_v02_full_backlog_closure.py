from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_full_backlog_closure.py"
    spec = importlib.util.spec_from_file_location("v02_full_backlog_closure_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_full_backlog_closure_counts_and_boundaries_are_conservative() -> None:
    module = load_module()

    snapshot = module.build_snapshot()

    assert snapshot["status"] == "completed"
    assert snapshot["counts"] == {
        "total": 10,
        "completed_or_validated": 8,
        "external_or_scope_blocked": 2,
    }
    items = {item["experiment_id"]: item for item in snapshot["items"]}
    assert sorted(items) == [f"E{i:02d}" for i in range(1, 11)]
    assert items["E02"]["final_disposition"] == "completed_for_proxy_blocked_for_true_human_panel"
    assert "human panel" in items["E02"]["remaining_boundary"]
    assert items["E10"]["final_disposition"] == "blocked_until_governed_boundary"
    assert items["E10"]["metrics"]["unrestricted_memory"]["allowed"] is False
    assert items["E06"]["closure_level"] == "slot_coverage_fixture"
    assert items["E07"]["metrics"]["router_ready_for_default"] is False
