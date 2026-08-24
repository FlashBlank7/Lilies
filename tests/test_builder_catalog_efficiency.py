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


def test_catalog_overview_carries_block_responsibilities_not_just_titles() -> None:
    """选型是架构那一步唯一重要的事，目录必须说清每个积木**干什么**。

    此前这张表只给 `type — English title`：模型看到的是
    "variable_aggregator — Variable Aggregator"，看不到那句要命的
    "只做分支值合并/透传，不做任何算术"——尽管平台早就写好了这句话，
    只是当时只发给编辑器界面。八轮真机构建里有四轮的架构师第一步都在盲查目录。
    """
    from agent_platform.blocks import build_block_registry
    from agent_platform.builder import WorkflowBuilder

    class _Probe(WorkflowBuilder):
        def __init__(self) -> None:
            self.blocks = build_block_registry()

            class _NoTools:
                def names(self) -> list[str]:
                    return []

            self.core_tools = _NoTools()

    overview = _Probe()._catalog_overview()

    # 那句最要命的反模式警告必须在选型面前就看得到
    assert "只做分支值合并/透传，不做任何算术" in overview
    assert "sum_by" in overview          # 正确做法也要指出来
    # 不能退回只有标题的老样子
    assert "variable_aggregator — Variable Aggregator" not in overview
    # 体量要留得住：这张表每轮都进上下文
    assert len(overview) < 8000, len(overview)


def test_every_block_has_a_chinese_one_liner() -> None:
    """目录概览按中文一句话职责选型，缺一条就有一个积木是"盲选"的。

    补齐前 replenishment_planner / deployed_forecast 两个都没有——恰好是工业
    任务包 T6（补货规划）和 T4（预测）的关键积木。
    """
    from agent_platform.blocks import _ZH_BLOCKS, build_block_registry

    missing = [item.type for item in build_block_registry().list() if item.type not in _ZH_BLOCKS]
    assert missing == [], missing
