from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


ROOT = Path(__file__).resolve().parents[1]
HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def load_audit_module():
    module_path = ROOT / "scripts" / "v03_50_bounded_japanese_learning_runtime_validation.py"
    spec = importlib.util.spec_from_file_location("v03_50_bounded_japanese_learning_runtime_validation_under_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def mutate(client: TestClient, app_id: str, revision: int, op: str, data: dict) -> int:
    response = client.post(
        f"/api/v1/applications/{app_id}/draft",
        headers=HEADERS,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": op,
            "data": data,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["revision"]


def test_v03_50_source_and_manifest_markers_pass() -> None:
    module = load_audit_module()
    assert all(check["passed"] for check in module.source_marker_checks())


def test_v03_50_bounded_fixture_declares_no_external_claim() -> None:
    module = load_audit_module()
    fixture = module.bounded_learning_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["fixture_does_not_claim_live_collection"] is True
    assert fixture["cases"]["fixture_contains_three_spoken_expressions"] is True


def test_v03_50_summary_template_has_learning_card_sections() -> None:
    module = load_audit_module()
    fixture = module.learning_summary_template_fixture()
    assert fixture["passed"] is True
    assert fixture["cases"]["summary_has_meaning"] is True
    assert fixture["cases"]["summary_has_example"] is True
    assert fixture["cases"]["summary_has_tone_context"] is True
    assert fixture["cases"]["summary_has_learning_reminder"] is True


def test_v03_50_run_guidance_shows_controlled_fixture_boundary() -> None:
    module = load_audit_module()
    fixture = module.controlled_fixture_ui_boundary()
    assert fixture["passed"] is True
    assert fixture["cases"]["zh_copy_present"] is True
    assert fixture["cases"]["run_page_marker_present"] is True


def test_v03_50_runtime_quality_expectations_cover_learner_result() -> None:
    module = load_audit_module()
    fixture = module.runtime_quality_expectations()
    assert fixture["passed"] is True
    assert "今日日语口语总结：校园生活" in fixture["expected_terms"]
    assert "受控样例来源" in fixture["expected_terms"]


def test_v03_50_in_process_runtime_returns_learning_card(tmp_path: Path) -> None:
    module = load_audit_module()
    workflow = module.runtime_workflow_fixture()
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        app_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "今日日语口语总结", "requirement": "为日语学习者验证受控样例运行结果。"},
        ).json()["id"]
        revision = 0
        for node in workflow["nodes"]:
            revision = mutate(client, app_id, revision, "add_node", {"node": node})
        for edge in workflow["edges"]:
            revision = mutate(client, app_id, revision, "add_edge", {"edge": edge})
        revision = mutate(client, app_id, revision, "add_test", {"test": workflow["test"]})
        assert revision > 0

        report_response = client.post(f"/api/v1/applications/{app_id}/tests/run", headers=HEADERS)
        assert report_response.status_code == 200, report_response.text
        report = report_response.json()
        assert report["passed"] is True, report
        run_id = report["tests"][0]["run_id"]
        for _ in range(100):
            record = client.get(f"/api/v1/runs/{run_id}", headers=HEADERS).json()
            if record["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)
        assert record["status"] == "succeeded", record
        answer = record["outputs"]["answer"]
        for expected in ["今日日语口语总结：校园生活", "それな", "中文含义", "自然例句", "语气/场景", "学习提醒", "受控样例来源"]:
            assert expected in answer


def test_v03_50_static_evidence_passes_without_live_services() -> None:
    module = load_audit_module()
    evidence = module.build_evidence(live=False)
    assert evidence["status"] == "passed"
    assert evidence["safety"]["model_call_used"] is False
    assert evidence["safety"]["forbidden_endpoint_called"] is False


def test_v03_50_bug_ledger_has_no_open_p0_or_p1() -> None:
    module = load_audit_module()
    bug_check = module.bug_ledger_evidence()
    assert bug_check["passed"] is True
    assert bug_check["blocking_bug_count"] == 0


def test_v03_50_writes_json(tmp_path: Path) -> None:
    module = load_audit_module()
    output = tmp_path / "audit.json"
    module.write_evidence(output, module.build_evidence(live=False))
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded["version"] == "v0.3.50"
    assert loaded["status"] == "passed"
