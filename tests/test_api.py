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
        assert response.json()["provider"] in ("deepseek", "multi", "scripted")


def test_debug_page_is_available(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", workspace_root=tmp_path / "workspaces")
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        response = client.get("/debug")
        assert response.status_code == 200
        assert "根据需求生成" in response.text


def test_block_manual_endpoints_expose_agent_architecture_catalog(tmp_path: Path) -> None:
    settings = Settings(
        api_token="secret-test-token",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    app = create_app(settings, ScriptedProvider())
    with TestClient(app) as client:
        auth = {"Authorization": "Bearer secret-test-token"}
        blocks = client.get("/api/v1/blocks", headers=auth)
        assert blocks.status_code == 200
        by_type = {item["type"]: item for item in blocks.json()}
        assert by_type["model_turn"]["block_kind"] == "agent_architecture"
        assert by_type["claude_agent"]["block_kind"] == "legacy_compatibility"

        manual = client.get("/api/v1/blocks/model_turn/manual", headers=auth)
        assert manual.status_code == 200
        assert manual.json()["claude_architecture_mapping"] == "Model sampling turn"
        assert manual.json()["when_to_use"]
        assert manual.json()["common_errors"]

        search = client.get(
            "/api/v1/block-manuals",
            headers=auth,
            params={"query": "permission", "block_kind": "agent_architecture"},
        )
        assert search.status_code == 200
        assert any(item["type"] == "permission_gate" for item in search.json())

        blueprint = client.get("/api/v1/claude-architecture-blueprint", headers=auth)
        assert blueprint.status_code == 200
        assert "model_loop" in blueprint.json()["groups"]
