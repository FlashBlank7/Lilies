from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_4_browser_unavailable_smoke_retention.py"
    spec = importlib.util.spec_from_file_location("v03_4_browser_unavailable_smoke_retention_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sample_apps() -> list[dict[str, object]]:
    return [
        {"id": "a1", "name": "v0.3.2-smoke customer draft", "description": "", "requirement": "", "updated_at": "2"},
        {"id": "a2", "name": "normal app", "description": "", "requirement": "", "updated_at": "1"},
        {"id": "a3", "name": "starter", "description": "", "requirement": "[v0.3.3-smoke] skeleton", "updated_at": "3"},
    ]


def test_v03_4_smoke_retention_index_groups_markers() -> None:
    module = load_audit_module()
    index = module.smoke_retention_index(sample_apps())

    assert index["passed"] is True
    assert index["marker_count"] == 2
    assert index["total_smoke_app_count"] == 2
    assert index["markers"]["v0.3.2-smoke"]["latest_id"] == "a1"
    assert index["markers"]["v0.3.3-smoke"]["latest_id"] == "a3"


def test_v03_4_static_evidence_records_browser_unavailable() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False, applications=sample_apps())

    assert evidence["status"] == "passed"
    assert evidence["browser_evidence"]["status"] == "unavailable"
    assert evidence["browser_evidence"]["claim"] == "fallback_rendered_route_evidence_only"
    assert evidence["summary"]["smoke_app_count"] == 2


def test_v03_4_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False, applications=sample_apps())
    bug_check = next(check for check in evidence["checks"] if check["id"] == "p0_p1_bug_ledger_browser_retention")

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_4_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"

    module.write_evidence(output, module.build_evidence(live=False, applications=sample_apps()))
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.4"
    assert loaded["status"] == "passed"
