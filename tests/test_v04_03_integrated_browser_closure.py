from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.storage import Storage
from tests.test_runtime import ScriptedProvider


HEADERS = {
    "Authorization": "Bearer workflow-test",
    "Content-Type": "application/json",
}


def _mutate(
    client: TestClient,
    application_id: str,
    revision: int,
    operation: str,
    data: dict,
) -> dict:
    response = client.post(
        f"/api/v1/applications/{application_id}/draft",
        headers=HEADERS,
        json={
            "expected_revision": revision,
            "idempotency_key": str(uuid4()),
            "op": operation,
            "data": data,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _create(client: TestClient, name: str, mode: str, hard_gate: bool = False) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={
            "name": name,
            "requirement": "Let a non-technical operator run and verify a customer request.",
            "delivery_mode": mode,
            "governed_hard_gate": hard_gate,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _seed_passing_workflow(client: TestClient, application_id: str) -> dict:
    revision = 0
    for node in (
        {"id": "start", "type": "start", "title": "Customer input", "config": {"inputs": []}},
        {
            "id": "end",
            "type": "end",
            "title": "Customer result",
            "config": {"outputs": {"answer": "ready"}},
        },
    ):
        changed = _mutate(client, application_id, revision, "add_node", {"node": node})
        revision = changed["revision"]
    changed = _mutate(
        client,
        application_id,
        revision,
        "add_edge",
        {
            "edge": {
                "id": "start-end",
                "source": "start",
                "target": "end",
                "source_port": "output",
                "target_port": "input",
            }
        },
    )
    revision = changed["revision"]
    changed = _mutate(
        client,
        application_id,
        revision,
        "add_test",
        {
            "test": {
                "id": "customer-result",
                "name": "Customer result is visible",
                "requirement": "The customer receives a result.",
                "assertions": [{"path": ["answer"], "operator": "equals", "expected": "ready"}],
                "mandatory": True,
            }
        },
    )
    return changed


def _seed_legacy_workflow_database(data_dir: Path) -> None:
    storage = Storage(data_dir)
    storage._initialize_sync()
    snapshot = {
        "name": "Legacy customer workflow",
        "description": "Created before delivery modes.",
        "mode": "workflow",
        "requirement": "Keep the legacy customer workflow usable.",
        "workflow": {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 0.8}},
        "agents": {},
        "tests": [],
    }
    now = "2026-07-16T00:00:00+00:00"
    with storage._connect() as connection:
        connection.executescript(
            """
            CREATE TABLE applications (
              id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              description TEXT NOT NULL,
              mode TEXT NOT NULL,
              requirement TEXT NOT NULL,
              active_version INTEGER,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE application_drafts (
              application_id TEXT PRIMARY KEY,
              revision INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              content_hash TEXT NOT NULL,
              tested_hash TEXT,
              validation_report_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
            (
                "legacy-browser",
                snapshot["name"],
                snapshot["description"],
                snapshot["mode"],
                snapshot["requirement"],
                None,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO application_drafts VALUES(?,?,?,?,?,?,?)",
            (
                "legacy-browser",
                0,
                json.dumps(snapshot),
                "legacy-content-hash",
                None,
                "{}",
                now,
            ),
        )


def test_integrated_delivery_evidence_repair_and_publication_journey(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    _seed_legacy_workflow_database(data_dir)
    settings = Settings(
        api_token="workflow-test",
        data_dir=data_dir,
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())

    with TestClient(app) as client:
        legacy = client.get("/api/v1/applications/legacy-browser", headers=HEADERS)
        assert legacy.status_code == 200, legacy.text
        assert legacy.json()["delivery_mode"] == "guided"
        assert legacy.json()["governed_hard_gate"] is False

        quick_id = _create(client, "Quick customer journey", "quick")
        guided_id = _create(client, "Guided customer journey", "guided")
        governed_id = _create(client, "Governed customer journey", "governed", True)
        modes = {
            item["id"]: item["delivery_mode"]
            for item in client.get("/api/v1/applications", headers=HEADERS).json()
        }
        assert modes[quick_id] == "quick"
        assert modes[guided_id] == "guided"
        assert modes[governed_id] == "governed"

        quick_draft = _seed_passing_workflow(client, quick_id)
        report = client.post(f"/api/v1/applications/{quick_id}/tests/run", headers=HEADERS)
        assert report.status_code == 200, report.text
        assert report.json()["passed"] is True
        current = client.get(f"/api/v1/applications/{quick_id}/draft", headers=HEADERS).json()
        assert current["evidence"]["state"] == "current"

        changed = _mutate(
            client,
            quick_id,
            quick_draft["revision"],
            "set_metadata",
            {"description": "Customer-visible behavior changed after validation."},
        )
        assert changed["evidence_state"] == "stale"
        stale_decision = client.get(
            f"/api/v1/applications/{quick_id}/publication-decision", headers=HEADERS
        ).json()
        assert stale_decision["warning_codes"] == ["stale_evidence"]
        assert stale_decision["requires_confirmation"] is True
        assert stale_decision["blocked"] is False
        refused = client.post(f"/api/v1/applications/{quick_id}/versions", headers=HEADERS)
        assert refused.status_code == 409
        quick_publish = client.post(
            f"/api/v1/applications/{quick_id}/versions",
            headers=HEADERS,
            json={"acknowledge_warnings": True},
        )
        assert quick_publish.status_code == 200, quick_publish.text
        assert quick_publish.json()["publication_decision"]["evidence_state"] == "stale"

        guided_decision = client.get(
            f"/api/v1/applications/{guided_id}/publication-decision", headers=HEADERS
        ).json()
        assert guided_decision["warning_codes"] == ["missing_evidence"]
        assert guided_decision["requires_confirmation"] is True
        guided_publish = client.post(
            f"/api/v1/applications/{guided_id}/versions",
            headers=HEADERS,
            json={"acknowledge_warnings": True},
        )
        assert guided_publish.status_code == 200, guided_publish.text

        governed_decision = client.get(
            f"/api/v1/applications/{governed_id}/publication-decision", headers=HEADERS
        ).json()
        assert governed_decision["blocked"] is False
        assert governed_decision["requires_confirmation"] is True
        assert governed_decision["policy"]["hard_gate_enabled"] is False
        governed_publish = client.post(
            f"/api/v1/applications/{governed_id}/versions",
            headers=HEADERS,
            json={"acknowledge_warnings": True},
        )
        assert governed_publish.status_code == 200, governed_publish.text

        repair_base = client.get(
            f"/api/v1/applications/{quick_id}/draft", headers=HEADERS
        ).json()
        with_failure = _mutate(
            client,
            quick_id,
            repair_base["revision"],
            "add_test",
            {
                "test": {
                    "id": "permission-boundary",
                    "name": "Permission boundary is visible",
                    "requirement": "The workflow exposes a permission boundary before output.",
                    "required_node_types": ["permission_gate"],
                    "assertions": [],
                    "mandatory": True,
                }
            },
        )
        failed = client.post(f"/api/v1/applications/{quick_id}/tests/run", headers=HEADERS)
        assert failed.status_code == 200, failed.text
        assert failed.json()["passed"] is False
        failed_cases = [
            item
            for item in failed.json()["tests"]
            if item["name"] == "Permission boundary is visible"
        ]
        assert failed_cases, failed.json()
        failed_case = failed_cases[0]
        assert failed_case["passed"] is False

        preview = client.post(
            f"/api/v1/applications/{quick_id}/tests/repair-preview",
            headers=HEADERS,
            json={"report": failed.json(), "test_id": failed_case["test_id"]},
        )
        assert preview.status_code == 200, preview.text
        preview_body = preview.json()
        assert preview_body["supported"] is True
        assert preview_body["expected_revision"] == with_failure["revision"]
        assert preview_body["repair_context"]["run_id"] == failed_case["run_id"]
        assert "permission_gate" in preview_body["missing_node_types"]

        applied = client.post(
            f"/api/v1/applications/{quick_id}/tests/repair-apply",
            headers=HEADERS,
            json={
                "expected_revision": preview_body["expected_revision"],
                "expected_content_hash": preview_body["expected_content_hash"],
                "operations": preview_body["operations"],
                "idempotency_key": str(uuid4()),
            },
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["evidence_state"] == "stale"
        repaired = client.get(f"/api/v1/applications/{quick_id}/draft", headers=HEADERS).json()
        assert "permission_gate" in {
            node["type"] for node in repaired["snapshot"]["workflow"]["nodes"]
        }

        quick_versions = client.get(
            f"/api/v1/applications/{quick_id}/versions", headers=HEADERS
        ).json()
        assert quick_versions[0]["publication_decision"]["acknowledged_warnings"] is True
        assert quick_versions[0]["publication_decision"]["evidence_state"] == "stale"
