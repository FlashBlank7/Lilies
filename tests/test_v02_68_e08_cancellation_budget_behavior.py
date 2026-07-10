from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_behavior_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_68_e08_cancellation_budget_behavior.py"
    spec = importlib.util.spec_from_file_location("v02_68_e08_cancellation_budget_behavior_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_68_e08_cancellation_budget_evidence(tmp_path: Path) -> None:
    module = load_behavior_module()

    evidence = module.build_evidence(tmp_path)

    cancellation = evidence["cancellation_api"]
    assert cancellation["status_code"] == 200
    assert cancellation["response"]["status"] == "cancelling"
    assert cancellation["cancel_called"] is True

    budget = evidence["budget_record"]
    assert budget["status"] == "failed"
    assert "model call budget exceeded" in budget["violation"]
    assert budget["usage_counts"]["model_call"] == 2
    assert evidence["not_full_sidecar_completion"] is True
