from __future__ import annotations

from functools import partial
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.workflow_models import BuildTeamState
from tests.test_runtime import ScriptedProvider


HEADERS = {"Authorization": "Bearer workflow-test", "Content-Type": "application/json"}


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_token="workflow-test",
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "workspaces",
        scheduler_poll_seconds=3600,
    )


def test_catalog_overview_lists_every_block_once(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        services = client.app.state.services
        overview = services.builder._catalog_overview()
        for definition in services.blocks.list():
            assert definition.type in overview
        # Compact by construction: one line per category, not one per block.
        assert len(overview.splitlines()) <= len(services.blocks.list())


def test_repeated_catalog_search_returns_pointer_not_full_results(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), ScriptedProvider())
    with TestClient(app) as client:
        services = client.app.state.services
        state = BuildTeamState()

        run = lambda data: client.portal.call(partial(
            services.builder._execute,
            "build-x",
            "app-x",
            state,
            "catalog_search",
            data,
            max_repair_cycles=4,
            auto_publish=False,
        ))
        first = run({"query": "llm"})
        assert isinstance(first, list) and first, "first search returns full results"
        assert state.catalog_queries == ["llm"]

        second = run({"query": "  llm "})
        assert isinstance(second, dict), "repeat search collapses to a pointer"
        assert "matching_types" in second
        assert "llm" in second["matching_types"]
        # A different query still runs normally.
        third = run({"query": "http"})
        assert isinstance(third, list) and third
        assert state.catalog_queries == ["llm", "http"]
