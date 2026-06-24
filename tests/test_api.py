from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from tests.test_runtime import ScriptedProvider


def test_health_and_authentication(tmp_path: Path) -> None:
    settings = Settings(
        api_token="secret-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        assert client.get("/v1/models").status_code == 401
        response = client.get(
            "/v1/models", headers={"Authorization": "Bearer secret-test-token"}
        )
        assert response.status_code == 200
        assert response.json()["provider"] == "deepseek"


def test_debug_page_is_available(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.get("/debug")
        assert response.status_code == 200
        assert "根据需求生成" in response.text

