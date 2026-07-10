from __future__ import annotations

import importlib.util
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_92_complexity_router_limited_default_readiness_review.py"
    spec = importlib.util.spec_from_file_location("v02_92_readiness_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def passing_metrics() -> dict[str, object]:
    return {
        "default_metrics": {
            "rollback_value": "disabled",
            "decision_categories": {
                "active": 0,
                "bypassed": 1,
                "disabled_default": 1,
                "conservative_unknown": 0,
                "request_override": 0,
            },
        },
        "enabled_metrics": {
            "rollback_value": "disabled",
            "decision_categories": {
                "active": 2,
                "bypassed": 1,
                "disabled_default": 0,
                "conservative_unknown": 1,
                "request_override": 1,
            },
        },
        "frontend_verification": {"passed": True},
    }


def test_v02_92_readiness_selects_guarded_default_rollout_when_all_gates_pass() -> None:
    module = load_module()
    result = module.evaluate_readiness(passing_metrics())

    assert all(item["passed"] for item in result["readiness_gates"])
    assert result["decision"]["decision"] == "enter_guarded_default_rollout"
    assert result["decision"]["next_version"] == "v0.2.93_complexity_router_guarded_default_rollout"
    assert result["normal_default_settings"] == "disabled"


def test_v02_92_readiness_fails_closed_when_unknown_gate_missing() -> None:
    module = load_module()
    metrics = passing_metrics()
    metrics["enabled_metrics"]["decision_categories"]["conservative_unknown"] = 0  # type: ignore[index]
    result = module.evaluate_readiness(metrics)

    gates = {item["name"]: item["passed"] for item in result["readiness_gates"]}
    assert gates["unknown_bypass_safety"] is False
    assert result["decision"]["decision"] == "collect_more_runtime_evidence"
    assert result["normal_default_settings"] == "disabled"
