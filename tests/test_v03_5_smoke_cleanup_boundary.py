from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings


HEADERS = {"Authorization": "Bearer test-token-2024"}


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        api_token="test-token-2024",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    settings.prepare()
    return TestClient(create_app(settings=settings))


def create_application(client: TestClient, *, name: str, requirement: str) -> str:
    response = client.post(
        "/api/v1/applications",
        headers=HEADERS,
        json={"name": name, "requirement": requirement, "mode": "workflow"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_v03_5_smoke_cleanup_dry_run_does_not_delete() -> None:
    with TemporaryDirectory() as temp_dir:
        with make_client(Path(temp_dir)) as client:
            app_id = create_application(
                client,
                name="v0.3.5-smoke dry run",
                requirement="[v0.3.5-smoke] local evidence app",
            )

            cleanup = client.post(
                f"/api/v1/applications/{app_id}/smoke-cleanup",
                headers=HEADERS,
                json={"smoke_marker": "v0.3.5-smoke", "dry_run": True},
            )

            assert cleanup.status_code == 200, cleanup.text
            assert cleanup.json()["deleted"] is False
            assert cleanup.json()["related_counts"]["application_drafts"] == 1
            assert client.get(f"/api/v1/applications/{app_id}", headers=HEADERS).status_code == 200


def test_v03_5_smoke_cleanup_deletes_matching_smoke_app() -> None:
    with TemporaryDirectory() as temp_dir:
        with make_client(Path(temp_dir)) as client:
            app_id = create_application(
                client,
                name="v0.3.5-smoke delete",
                requirement="[v0.3.5-smoke] local evidence app",
            )

            cleanup = client.post(
                f"/api/v1/applications/{app_id}/smoke-cleanup",
                headers=HEADERS,
                json={"smoke_marker": "v0.3.5-smoke", "dry_run": False},
            )

            assert cleanup.status_code == 200, cleanup.text
            assert cleanup.json()["deleted"] is True
            assert client.get(f"/api/v1/applications/{app_id}", headers=HEADERS).status_code == 404
            assert client.get(f"/api/v1/applications/{app_id}/draft", headers=HEADERS).status_code == 404


def test_v03_5_smoke_cleanup_rejects_non_matching_application() -> None:
    with TemporaryDirectory() as temp_dir:
        with make_client(Path(temp_dir)) as client:
            app_id = create_application(
                client,
                name="real customer app",
                requirement="production-like non-smoke requirement",
            )

            cleanup = client.post(
                f"/api/v1/applications/{app_id}/smoke-cleanup",
                headers=HEADERS,
                json={"smoke_marker": "v0.3.5-smoke", "dry_run": False},
            )

            assert cleanup.status_code == 422
            assert client.get(f"/api/v1/applications/{app_id}", headers=HEADERS).status_code == 200
