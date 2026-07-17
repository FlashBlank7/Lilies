from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings


HEADERS = {"Authorization": "Bearer smoke-cleanup-test"}


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Settings(
        api_token="smoke-cleanup-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )))


def create_application(client: TestClient, marker: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={
            "name": f"{marker} cleanup boundary",
            "requirement": f"[{marker}] temporary human journey fixture",
            "mode": "workflow",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_smoke_cleanup_accepts_the_current_semantic_version_marker(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        application_id = create_application(client, "v0.4.11-smoke")

        response = client.post(
            f"/api/v1/applications/{application_id}/smoke-cleanup",
            headers=HEADERS,
            json={"smoke_marker": "v0.4.11-smoke", "dry_run": False},
        )

        assert response.status_code == 200, response.text
        assert response.json()["deleted"] is True
        assert client.get(
            f"/api/v1/applications/{application_id}", headers=HEADERS
        ).status_code == 404


def test_smoke_cleanup_rejects_an_unversioned_marker(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        application_id = create_application(client, "release-smoke")

        response = client.post(
            f"/api/v1/applications/{application_id}/smoke-cleanup",
            headers=HEADERS,
            json={"smoke_marker": "release-smoke", "dry_run": False},
        )

        assert response.status_code == 422
        assert client.get(
            f"/api/v1/applications/{application_id}", headers=HEADERS
        ).status_code == 200
