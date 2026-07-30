from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.delivery_policy import resolve_delivery_policy
from agent_platform.storage import Storage
from agent_platform.workflow_models import ApplicationCreateRequest, DeliveryMode
from agent_platform.workflow_storage import WorkflowStorage
from tests.test_runtime import ScriptedProvider


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def test_delivery_policy_matrix_is_explicit() -> None:
    quick = resolve_delivery_policy("quick")
    guided = resolve_delivery_policy("guided")
    governed_advisory = resolve_delivery_policy("governed", governed_hard_gate=False)
    governed_hard = resolve_delivery_policy("governed", governed_hard_gate=True)

    assert quick.publication_behavior == "advisory_confirmation"
    assert quick.warning_ack_required is True
    assert guided.publication_behavior == "advisory_confirmation"
    assert guided.warning_ack_required is True
    assert governed_advisory.publication_behavior == "advisory_confirmation"
    assert governed_advisory.hard_gate_enabled is False
    assert governed_hard.publication_behavior == "advisory_confirmation"
    assert governed_hard.missing_evidence_action == "confirm"
    assert governed_hard.stale_evidence_action == "confirm"
    assert governed_hard.hard_gate_enabled is False


def test_application_round_trips_shape_and_delivery_mode_independently(tmp_path: Path) -> None:
    settings = Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/applications",
            headers=_headers(),
            json={
                "name": "Support copilot",
                "requirement": "Support a customer conversation.",
                "mode": "chat",
                "delivery_mode": "quick",
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        application_id = body["id"]
        assert body["mode"] == "chat"
        assert body["delivery_mode"] == "quick"
        assert body["delivery_policy"]["publication_behavior"] == "advisory_confirmation"

        changed = client.post(
            f"/api/v1/applications/{application_id}/draft",
            headers=_headers(),
            json={
                "expected_revision": 0,
                "idempotency_key": str(uuid4()),
                "op": "set_metadata",
                "data": {
                    "delivery_mode": "governed",
                    "governed_hard_gate": True,
                },
            },
        )
        assert changed.status_code == 200, changed.text

        detail = client.get(
            f"/api/v1/applications/{application_id}", headers=_headers()
        ).json()
        draft = client.get(
            f"/api/v1/applications/{application_id}/draft", headers=_headers()
        ).json()
        listed = client.get("/api/v1/applications", headers=_headers()).json()[0]

        for item in (detail, listed):
            assert item["mode"] == "chat"
            assert item["delivery_mode"] == "governed"
            assert item["governed_hard_gate"] is True
            assert item["delivery_policy"]["hard_gate_enabled"] is False
        assert draft["snapshot"]["mode"] == "chat"
        assert draft["snapshot"]["delivery_mode"] == "governed"
        assert draft["snapshot"]["governed_hard_gate"] is True
        assert draft["delivery_policy"]["publication_behavior"] == "advisory_confirmation"


def test_legacy_application_schema_and_snapshot_migrate_to_guided(tmp_path: Path) -> None:
    storage = Storage(tmp_path / "data")
    storage._initialize_sync()
    now = "2026-07-16T00:00:00+00:00"
    snapshot = {
        "name": "Legacy workflow",
        "description": "Created before delivery modes.",
        "mode": "workflow",
        "requirement": "Keep this record readable.",
        "workflow": {"nodes": [], "edges": [], "viewport": {"x": 0, "y": 0, "zoom": 0.8}},
        "agents": {},
        "tests": [],
    }
    with storage._connect() as conn:
        conn.executescript(
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
        conn.execute(
            "INSERT INTO applications VALUES(?,?,?,?,?,?,?,?)",
            ("legacy", "Legacy workflow", "Created before delivery modes.", "workflow", "Keep this record readable.", None, now, now),
        )
        conn.execute(
            "INSERT INTO application_drafts VALUES(?,?,?,?,?,?,?)",
            ("legacy", 0, json.dumps(snapshot), "legacy-hash", None, "{}", now),
        )

    store = WorkflowStorage(storage)
    store._initialize_sync()
    application = asyncio.run(store.get_application("legacy"))
    draft = asyncio.run(store.get_draft("legacy"))

    assert application["delivery_mode"] == "guided"
    assert application["governed_hard_gate"] is False
    assert application["delivery_policy"]["publication_behavior"] == "advisory_confirmation"
    assert draft["snapshot"].delivery_mode.value == "guided"
    assert draft["snapshot"].governed_hard_gate is False


def test_frontend_exposes_creation_and_studio_delivery_mode_controls() -> None:
    root = Path(__file__).resolve().parents[1]
    home = (root / "platform/frontend/app/page.tsx").read_text()
    studio = (root / "platform/frontend/app/applications/[id]/page.tsx").read_text()
    frontend_types = (root / "platform/frontend/lib/platform.ts").read_text()

    assert "data-delivery-mode={deliveryMode}" in home
    assert "delivery_mode: deliveryMode" in home
    assert "data-delivery-mode={currentDeliveryMode}" in studio
    assert "governed_hard_gate: governedHardGate" in studio
    assert 'type="checkbox"' in studio
    assert "export type DeliveryMode = 'quick' | 'guided' | 'governed'" in frontend_types


def test_delivery_mode_survives_publish_and_restore(tmp_path: Path) -> None:
    async def scenario() -> None:
        storage = Storage(tmp_path / "data")
        await storage.initialize()
        store = WorkflowStorage(storage)
        await store.initialize()
        application = await store.create_application(ApplicationCreateRequest(
            name="Governed release",
            requirement="Preserve assurance policy with every immutable version.",
            delivery_mode=DeliveryMode.governed,
            governed_hard_gate=True,
        ))
        application_id = application["id"]
        draft = await store.get_draft(application_id)
        await store.mark_tested(
            application_id,
            draft["revision"],
            draft["content_hash"],
            {"passed": True},
        )
        published = await store.publish(application_id)
        assert published["version"] == 1

        changed_snapshot = draft["snapshot"].model_copy(update={
            "delivery_mode": DeliveryMode.quick,
            "governed_hard_gate": False,
        })
        changed = await store.save_draft(
            application_id,
            changed_snapshot,
            expected_revision=draft["revision"],
            idempotency_key="change-to-quick",
        )
        assert changed["revision"] == 1
        await store.restore_version(application_id, 1)
        restored = await store.get_draft(application_id)
        assert restored["snapshot"].delivery_mode is DeliveryMode.governed
        assert restored["snapshot"].governed_hard_gate is True

    asyncio.run(scenario())
