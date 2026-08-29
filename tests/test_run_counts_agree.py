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


def _concierge(services):  # noqa: F811
    """把管家接到这套真库夹具上（只替换它取运行记录的那条路）。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_platform.assistant_agent import WorkflowConcierge

    agent = WorkflowConcierge.__new__(WorkflowConcierge)
    agent.services = services
    agent._resolve_app = AsyncMock(return_value={"id": "app-1", "name": "被测工作流"})
    services.workflow_store = SimpleNamespace(
        storage=services.workflow_store.storage, list_runs=services._store.list_runs)
    return agent


@pytest.mark.asyncio
async def test_a_day_filter_returns_that_whole_day(services):  # noqa: F811
    """问「某天跑了几次」不该靠翻最近 N 条去数。

    真机实测（2026-08-29）：问「文本行数与净字数统计昨天失败了几次」，
    它翻了最近 10 条、数出 2 次，并如实说"最近 10 条里"——
    而当天实际失败 5 次。它没编，是**没有按天查的能力**。
    """
    from datetime import datetime, timezone

    _seed(services, published=6, drafts=0)
    agent = _concierge(services)
    today = datetime.now(timezone.utc).date().isoformat()
    result = await agent._exec("recent_runs", {"name_or_id": "x", "day": today}, {})
    assert "这一天的全部" in result
    assert "不是抽样" in result["这一天的全部"]
    assert len(result["runs"]) == 6
    assert all(str(r["created_at"]).startswith(today) for r in result["runs"])


@pytest.mark.asyncio
async def test_the_payload_counts_so_the_model_need_not(services):  # noqa: F811
    """计数直接给，别让它数。

    工具返回 31 条（26 成 5 败）且标注了"是全部不是抽样"，
    它仍答成"失败 4 次"——逐条数 31 行本来就容易错，
    而这个数平台算得出来。
    """
    _seed(services, published=4, drafts=0)
    agent = _concierge(services)
    result = await agent._exec("recent_runs", {"name_or_id": "x", "limit": 50}, {})
    counts = result["按情况计数"]
    assert sum(counts.values()) == len(result["runs"])
    # 计数用的是翻译后的说法，不是状态码——状态码会被原样念出来
    assert all("succeeded" not in k and "failed" not in k for k in counts)


@pytest.mark.asyncio
async def test_a_day_with_no_runs_says_zero(services):  # noqa: F811
    """那天一次都没跑，要明确说 0，而不是给个空列表让它猜。"""
    _seed(services, published=2, drafts=0)
    agent = _concierge(services)
    result = await agent._exec("recent_runs", {"name_or_id": "x", "day": "2020-01-01"}, {})
    assert result["runs"] == []
    assert "0 次" in result["这一天的全部"]


@pytest.mark.asyncio
async def test_the_last_failure_is_always_included(services):  # noqa: F811
    """「最近一次失败是什么原因」不该靠翻页碰运气。

    真机实测（2026-08-29）：默认只给 5 条，那 5 条恰好都成功，
    它就答"没有失败记录"——而更早那天确实失败过。
    这个记录一句 SQL 就查得到，平台算得出来的就直接给。
    """
    from datetime import datetime, timezone

    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,"
            "active_version,created_at,updated_at) "
            "VALUES('app-1','被测工作流','','','workflow',1,"
            "datetime('now'),datetime('now'))")
        # 先失败一次，再成功 5 次——默认窗口里看不到那次失败
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES('bad','app-1',1,NULL,'failed',?,'{}',"
            "'node start failed: missing required input: text',"
            "datetime('now','-99 seconds'),datetime('now','-99 seconds'))",
            (_state("bad", "app-1"),))
        for index in range(5):
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
                "state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,'app-1',1,NULL,'succeeded',?,'{}',NULL,"
                "datetime('now',?),datetime('now',?))",
                (f"ok{index}", _state(f"ok{index}", "app-1"),
                 f"-{index} seconds", f"-{index} seconds"))

    agent = _concierge(services)
    result = await agent._exec("recent_runs", {"name_or_id": "x", "limit": 5}, {})
    assert all(r["情况"] == "跑成了" for r in result["runs"]), "窗口里本来就该只有成功"
    last_bad = result["最近一次没跑成"]
    assert isinstance(last_bad, dict), "窗口外的那次失败没给出来"
    assert "text" in last_bad["原因"], last_bad
    assert last_bad["run_id"] == "bad"


@pytest.mark.asyncio
async def test_a_workflow_that_never_failed_says_so(services):  # noqa: F811
    """从没失败过要明说，别给个 None 让它猜。"""
    _seed(services, published=3, drafts=0)
    agent = _concierge(services)
    result = await agent._exec("recent_runs", {"name_or_id": "x"}, {})
    assert result["最近一次没跑成"] == "从来没失败过"
