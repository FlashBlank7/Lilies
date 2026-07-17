from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def test_schedule_status_distinguishes_absent_and_unpublished_draft(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            api_token="workflow-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            scheduler_poll_seconds=3600,
        ),
        ScriptedProvider(),
    )
    with TestClient(app) as client:
        application_id = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={"name": "Schedule boundary", "requirement": "Classify one customer message."},
        ).json()["id"]

        absent = client.get(
            f"/api/v1/applications/{application_id}/schedule-status",
            headers=HEADERS,
        )
        assert absent.status_code == 200, absent.text
        assert absent.json()["status"] == "not_configured"
        assert absent.json()["draft_has_schedule"] is False
        assert absent.json()["schedule"] is None

        operation = client.post(
            f"/api/v1/applications/{application_id}/draft",
            headers=HEADERS,
            json={
                "expected_revision": 0,
                "idempotency_key": str(uuid4()),
                "op": "add_node",
                "data": {
                    "node": {
                        "id": "daily",
                        "type": "schedule_trigger",
                        "title": "Daily run",
                        "config": {
                            "timezone": "Asia/Tokyo",
                            "hour": 8,
                            "minute": 0,
                            "inputs": {"topic": "customer feedback"},
                        },
                    },
                },
            },
        )
        assert operation.status_code == 200, operation.text

        unpublished = client.get(
            f"/api/v1/applications/{application_id}/schedule-status",
            headers=HEADERS,
        )
        assert unpublished.status_code == 200, unpublished.text
        assert unpublished.json()["status"] == "draft_unpublished"
        assert unpublished.json()["draft_has_schedule"] is True
        assert unpublished.json()["schedule"] is None

        missing = client.get(
            "/api/v1/applications/missing/schedule-status",
            headers=HEADERS,
        )
        assert missing.status_code == 404
