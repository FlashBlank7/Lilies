from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "platform" / "backend" / "src"))

from fastapi.testclient import TestClient

from agent_platform import PRODUCT_PHASE, __version__
from agent_platform.api import create_app
from agent_platform.config import Settings


def make_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        api_token="test-token-2024",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
    )
    settings.prepare()
    return TestClient(create_app(settings=settings))


def test_v03_6_fastapi_version_uses_runtime_identity() -> None:
    with TemporaryDirectory() as temp_dir:
        client = make_client(Path(temp_dir))

        assert client.app.version == __version__
        assert client.app.version.startswith(("v0.3.", "v0.4."))


def test_v03_6_health_exposes_current_code_route_identity() -> None:
    with TemporaryDirectory() as temp_dir:
        with make_client(Path(temp_dir)) as client:
            response = client.get("/health")

            assert response.status_code == 200
            health = response.json()
            runtime = health["runtime"]
            routes = runtime["route_availability"]
            assert runtime["version"] == __version__
            assert runtime["product_phase"] == PRODUCT_PHASE
            assert runtime["current_code_ready"] is True
            assert routes["applications_create"] is True
            assert routes["draft_detail"] is True
            assert routes["smoke_cleanup"] is True
            assert set(runtime["git"]) == {"commit", "branch", "tracked_dirty", "untracked_present"}
