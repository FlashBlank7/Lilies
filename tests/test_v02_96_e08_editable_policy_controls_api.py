from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_96_e08_editable_policy_controls_api.py"
    spec = importlib.util.spec_from_file_location("v02_96_e08_policy_controls_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_96_evidence_generates_before_after_and_rejection() -> None:
    module = load_module()
    evidence = module.generate_evidence()

    assert evidence["endpoint"] == "PATCH /api/v1/platform/harness/policy-controls"
    assert evidence["before"]["network_egress_policy"] == "full"
    assert evidence["after"]["network_egress_policy"] == "allowlist"
    assert evidence["after"]["cancellation_policy"] == "disabled"
    assert evidence["after"]["limits"]["max_model_calls_per_task"] == 5
    assert evidence["patch_response"]["audit"]["version"] == "v0.2.96"
    assert "cancellation_policy" in evidence["patch_response"]["audit"]["changed_fields"]
    assert "limits.max_model_calls_per_task" in evidence["patch_response"]["audit"]["changed_fields"]
    assert evidence["invalid_update_rejection"]["status_code"] == 422
    assert evidence["e07_invariant"]["no_e07_code_or_default_change"] is True
    assert evidence["not_full_sidecar_completion"] is True
