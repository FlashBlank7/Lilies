"""管家报的运行次数，得和面板是同一个口径。

回归背景（2026-08-29 真机测量）：面板（overview）早就只算发布版的真实运行
（`version IS NOT NULL`）——草稿自测是搭建过程的中间产物，
把它算进去会让体检把搭建期的自测失败判成"工作流坏了"。
可管家的 recent_runs 走的是 list_runs，全给。

于是同一个问题两个数：
    【服务器GPU日报】管家报 33 次、面板报 10 次——3.3 倍。
全库 386 条运行里 262 条（67%）是自测，这个差距不是边角料。

published_only 默认仍是 False：验收取证要看的正是那些自测运行。
谁在做"给人看的统计"，谁就该显式打开。
"""
import pytest

from helpers_overview import services  # noqa: F401  (pytest fixture)


def _state(run_id: str, app_id: str) -> str:
    """list_runs 会把 state_json 反序列化成 WorkflowRunState，得是合法的。

    （helpers_overview 里塞 '{}' 不出事，是因为 build_overview 不解析 state。）
    """
    from agent_platform.workflow_models import WorkflowRunState

    return WorkflowRunState(
        run_id=run_id, application_id=app_id,
        snapshot={"name": "x", "workflow": {"nodes": [], "edges": []}},
        inputs={}, workspace_path="/tmp/x").model_dump_json()


def _seed(services, app_id="app-1", published=3, drafts=5):  # noqa: F811
    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,"
            "active_version,created_at,updated_at) "
            "VALUES(?,?,'','','workflow',1,datetime('now'),datetime('now'))",
            (app_id, "被测工作流"))
        index = 0
        for version in [1] * published + [None] * drafts:
            index += 1
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,"
                "status,state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,?,?,?,'succeeded',?,'{}',NULL,"
                "datetime('now',?),datetime('now',?))",
                (f"r{index}", app_id, version, None if version else 1,
                 _state(f"r{index}", app_id),
                 f"-{index} seconds", f"-{index} seconds"))
    return app_id


@pytest.mark.asyncio
async def test_published_only_drops_the_draft_self_tests(services):  # noqa: F811
    app_id = _seed(services)
    store = services._store
    assert len(await store.list_runs(app_id, limit=50)) == 8
    assert len(await store.list_runs(app_id, limit=50, published_only=True)) == 3


@pytest.mark.asyncio
async def test_what_survives_all_has_a_version(services):  # noqa: F811
    app_id = _seed(services)
    runs = await services._store.list_runs(app_id, limit=50, published_only=True)
    assert runs and all(r["version"] is not None for r in runs)


@pytest.mark.asyncio
async def test_the_default_still_returns_everything(services):  # noqa: F811
    """验收取证靠的就是自测运行，默认不能把它们滤掉。"""
    app_id = _seed(services)
    runs = await services._store.list_runs(app_id, limit=50)
    assert any(r["version"] is None for r in runs)


@pytest.mark.asyncio
async def test_the_concierge_uses_the_panel_s_yardstick(services):  # noqa: F811
    """这条才是 bug 本体：两个面必须报同一个数。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_platform.assistant_agent import WorkflowConcierge
    from agent_platform.overview import build_overview

    app_id = _seed(services)
    agent = WorkflowConcierge.__new__(WorkflowConcierge)
    agent.services = services
    agent._resolve_app = AsyncMock(return_value={"id": app_id, "name": "被测工作流"})
    services.workflow_store = SimpleNamespace(
        storage=services.workflow_store.storage,
        list_runs=services._store.list_runs)

    result = await agent._exec("recent_runs", {"name_or_id": "被测工作流",
                                               "limit": 50}, {})
    panel = await build_overview(services)
    assert len(result["runs"]) == panel["runs_today"]["total"], (
        f"管家说 {len(result['runs'])} 次，面板说 {panel['runs_today']['total']} 次")


@pytest.mark.asyncio
async def test_it_says_when_there_are_more(services):  # noqa: F811
    """截断了要说出来——不然模型只能猜，实测它会回「我只能看到一部分」。"""
    app_id = _seed(services, published=10, drafts=0)
    from unittest.mock import AsyncMock
    from types import SimpleNamespace

    from agent_platform.assistant_agent import WorkflowConcierge

    agent = WorkflowConcierge.__new__(WorkflowConcierge)
    agent.services = services
    agent._resolve_app = AsyncMock(return_value={"id": app_id, "name": "被测工作流"})
    services.workflow_store = SimpleNamespace(
        storage=services.workflow_store.storage,
        list_runs=services._store.list_runs)

    few = await agent._exec("recent_runs", {"name_or_id": "x", "limit": 3}, {})
    many = await agent._exec("recent_runs", {"name_or_id": "x", "limit": 50}, {})
    assert "还有更多" in few and len(few["runs"]) == 3
    assert "还有更多" not in many
