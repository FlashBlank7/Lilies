from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_10_frontend_verification_recovery.py"
    spec = importlib.util.spec_from_file_location("v03_10_frontend_verification_recovery_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_10_frontend_toolchain_preflight_records_project_files() -> None:
    module = load_audit_module()
    check = module.frontend_toolchain_preflight()

    assert check["passed"] is True
    assert all(check["required_files"].values())
    assert set(check["tools"]) == {"node", "npm", "pnpm", "yarn"}
    assert isinstance(check["fallback_required"], bool)


def test_v03_10_hydrated_guard_fallback_passes() -> None:
    module = load_audit_module()
    check = module.hydrated_guard_state_machine_fallback()

    assert check["passed"] is True
    assert check["mode"] == "source_state_machine_fallback"
    assert all(item["passed"] for item in check["checks"])


def test_v03_10_i18n_key_completeness_passes() -> None:
    module = load_audit_module()
    check = module.i18n_key_completeness()

    assert check["passed"] is True
    assert check["missing_in_zh"] == []
    assert check["missing_in_en"] == []
    assert check["locale_drift"] == {"zh_only": [], "en_only": []}
    assert all(check["guard_keys_present"].values())


def test_v03_10_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["build_endpoint_called"] is False
    assert evidence["smoke_marker"] == "v0.3.10-smoke"


def test_v03_10_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_10_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"

    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.10"
    assert loaded["status"] == "passed"
