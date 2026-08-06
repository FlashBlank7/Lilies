import asyncio
from pathlib import Path
from typing import AsyncIterator

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.models import ChatMessage, StreamEvent, ToolDefinition
from agent_platform.providers.base import ModelProvider, ProviderCapabilities


HEADERS = {"Authorization": "Bearer smoke-cleanup-test"}


class SlowCleanupBuildProvider(ModelProvider):
    name = "slow-cleanup-build-provider"

    def capabilities(self, model: str) -> ProviderCapabilities:
        return ProviderCapabilities(True, True, True, False, False, 100_000, 8_000)

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[ChatMessage],
        tools: list[ToolDefinition],
        max_output_tokens: int,
        thinking_enabled: bool,
        effort: str,
        tool_choice: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        await asyncio.sleep(30)
        yield StreamEvent(type="message_start", data={
            "message": {"usage": {"input_tokens": 1}},
        })


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


def test_smoke_cleanup_accepts_marker_preserved_only_in_draft_contract(tmp_path: Path) -> None:
    marker = "v0.4.11-smoke"
    with make_client(tmp_path) as client:
        response = client.post(
            "/api/v1/applications",
            headers=HEADERS,
            json={
                "name": "AI completed workflow plan",
                "description": "The visible fields no longer contain the fixture marker.",
                "requirement": f"{marker}: temporary full customer journey",
                "mode": "workflow",
            },
        )
        assert response.status_code == 201, response.text
        application_id = response.json()["id"]

        cleanup = client.post(
            f"/api/v1/applications/{application_id}/smoke-cleanup",
            headers=HEADERS,
            json={"smoke_marker": marker, "dry_run": False},
        )

        assert cleanup.status_code == 200, cleanup.text
        assert cleanup.json()["deleted"] is True
        assert client.get(
            f"/api/v1/applications/{application_id}", headers=HEADERS
        ).status_code == 404


def test_smoke_cleanup_cancels_an_active_builder_before_deleting_rows(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            api_token="smoke-cleanup-test",
            data_dir=tmp_path / "data",
            workspace_root=tmp_path / "workspaces",
            scheduler_poll_seconds=3600,
        ),
        SlowCleanupBuildProvider(),
    )
    with TestClient(app) as client:
        application_id = create_application(client, "v0.4.11-smoke")
        build = client.post(
            f"/api/v1/applications/{application_id}/builds",
            headers=HEADERS,
            json={
                "requirement": "[v0.4.11-smoke] keep building until cleanup cancels it",
                "auto_publish": False,
                "max_turns": 5,
                "max_elapsed_seconds": 60,
            },
        )
        assert build.status_code == 202, build.text
        build_id = build.json()["build_id"]

        cleanup = client.post(
            f"/api/v1/applications/{application_id}/smoke-cleanup",
            headers=HEADERS,
            json={"smoke_marker": "v0.4.11-smoke", "dry_run": False},
        )

        assert cleanup.status_code == 200, cleanup.text
        assert cleanup.json()["cancelled_build_ids"] == [build_id]
        assert build_id not in app.state.services.builder.active
        assert client.get(
            f"/api/v1/applications/{application_id}", headers=HEADERS
        ).status_code == 404
