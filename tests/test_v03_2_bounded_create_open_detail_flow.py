from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_2_bounded_create_open_detail_flow.py"
    spec = importlib.util.spec_from_file_location("v03_2_bounded_create_open_detail_flow_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_v03_2_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)

    assert evidence["status"] == "passed"
    assert evidence["summary"]["open_p0_p1_bug_count"] == 0
    assert evidence["summary"]["build_endpoint_called"] is False


def test_v03_2_payload_is_marked_and_does_not_define_build() -> None:
    module = load_audit_module()
    payload = module.create_payload(now=123)

    assert payload["name"].startswith(module.SMOKE_MARKER)
    assert module.SMOKE_MARKER in payload["requirement"]
    assert payload["mode"] == "workflow"
    assert "build" not in payload
    assert "auto_publish" not in payload


def test_v03_2_token_loader_reads_env_file(tmp_path: Path) -> None:
    module = load_audit_module()
    env_file = tmp_path / ".env"
    env_file.write_text("API_TOKEN=test-token\n", encoding="utf-8")

    token, source = module.load_token(env_files=[env_file])

    assert token == "test-token"
    assert source.endswith(":API_TOKEN")


def test_v03_2_bug_ledger_allows_only_fixed_or_reasoned_p0_p1() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    bug_check = next(check for check in evidence["checks"] if check["id"] == "p0_p1_bug_ledger_create_open_detail")

    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0
    assert all(item["status"] in {"fixed", "verified_fixed", "deferred_with_reason"} for item in bug_check["bugs"])


def test_v03_2_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    evidence = module.build_evidence(live=False)

    module.write_evidence(output, evidence)
    loaded = json.loads(output.read_text(encoding="utf-8"))

    assert loaded["version"] == "v0.3.2"
    assert loaded["status"] == "passed"
