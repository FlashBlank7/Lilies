from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_78_complexity_router_bounded_live_validation.py"
    spec = importlib.util.spec_from_file_location("v02_78_bounded_live_validation_under_test", module_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_78_skip_result_records_provider_command_and_cases(monkeypatch: Any) -> None:
    module = load_module()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = module.build_skip_result(".venv/bin/python scripts/v02_78_complexity_router_bounded_live_validation.py")

    assert result["status"] == "skipped"
    assert result["reason"] == "DEEPSEEK_API_KEY is not configured"
    assert result["provider"] == "deepseek"
    assert result["command"].endswith("v02_78_complexity_router_bounded_live_validation.py")
    assert result["case_budget"] == 3
    assert len(result["case_results"]) == 3
    assert {case["status"] for case in result["case_results"]} == {"skipped"}
    assert result["default_enabled"] is False
    assert result["pass_fail"]["passed"] is False


def test_v02_78_metric_helpers_summarize_completed_cases() -> None:
    module = load_module()
    cases = [
        {"expected_class": "simple", "predicted_class": "simple", "passed": True, "duration_seconds": 1.0},
        {"expected_class": "medium", "predicted_class": "complex", "passed": False, "duration_seconds": 2.0},
    ]

    assert module.distribution(cases) == {"simple": 1, "complex": 1}
    assert module.success_rate_by_class(cases) == {"simple": 1.0, "medium": 0.0}
    assert module.latency_by_class(cases)["medium"]["avg_duration_seconds"] == 2.0
