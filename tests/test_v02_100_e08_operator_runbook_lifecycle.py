from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_100_e08_operator_runbook_lifecycle.py"
    spec = importlib.util.spec_from_file_location("v02_100_e08_runbook_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_100_runbook_validation_passes() -> None:
    module = load_module()
    result = module.validate_runbook()

    assert result["status"] == "passed"
    assert result["missing_sections"] == []
    assert result["missing_phrases"] == []
    assert result["checklist_item_count"] >= 6


def test_v02_100_runbook_preserves_e07_and_full_sidecar_boundary() -> None:
    module = load_module()
    result = module.validate_runbook()

    assert result["e07_invariant"]["no_e07_code_or_default_change"] is True
    assert result["not_full_sidecar_completion"] is True
