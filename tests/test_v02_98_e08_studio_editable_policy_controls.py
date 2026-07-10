from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "v02_98_e08_studio_editable_policy_controls.py"
    spec = importlib.util.spec_from_file_location("v02_98_e08_studio_controls_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v02_98_evidence_confirms_studio_patch_contract() -> None:
    module = load_module()
    evidence = module.generate_evidence()

    assert evidence["checks"]["patch_endpoint_wired"] is True
    assert evidence["checks"]["save_function_present"] is True
    assert evidence["checks"]["editable_controls_present"] is True
    assert evidence["checks"]["type_contract_present"] is True
    assert evidence["checks"]["i18n_present"] is True
    assert evidence["checks"]["css_present"] is True
    assert evidence["e07_invariant"]["no_e07_code_or_default_change"] is True
    assert evidence["not_full_sidecar_completion"] is True
