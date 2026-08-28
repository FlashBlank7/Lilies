"""连败判定的窗口要按工作流分，不能全局取 N 条再分桶。

回归背景（2026-08-29）：体检里取"每个应用最近 8 次运行"的写法是
`ORDER BY created_at DESC LIMIT 400` 之后在 Python 里分桶。
一个忙碌的工作流跑满 400 条，别人一条都进不了窗口——
于是安静工作流的连败**根本看不到**，体检照样报「0 个反复失败」。

和上午修的 recent_failures（SQL `LIMIT 8` 却在 Python 里合并）是同一个错：
**先截窗口再分组**。真机上单个工作流已占全部发布版运行的 56%，
124 条还没撑破 400，但方向明确。

最近一条失败原因那个查询同样：全局 LIMIT 200，一个高频失败的工作流
会把别人的原因全挤掉，于是那些工作流被判成"反复失败"却给不出原因。
"""
import pytest

from agent_platform.overview import build_health

from helpers_overview import services  # noqa: F401  (pytest fixture)

BUSY, QUIET = "busy-app", "quiet-app"


def _app(conn, app_id, name):
    conn.execute(
        "INSERT INTO applications(id,name,description,requirement,mode,"
        "active_version,created_at,updated_at) "
        "VALUES(?,?,'','','workflow',1,datetime('now'),datetime('now'))",
        (app_id, name))


def _runs(conn, app_id, statuses, *, offset=0):
    for index, status in enumerate(statuses):
        seconds = offset + index
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,"
            "status,state_json,outputs_json,error,created_at,updated_at) "
            "VALUES(?,?,1,NULL,?,'{}','{}',?,datetime('now',?),datetime('now',?))",
            (f"{app_id}-{seconds}", app_id, status,
             "节点跑挂了" if status == "failed" else None,
             f"-{seconds} seconds", f"-{seconds} seconds"))


@pytest.fixture
def crowded(services):  # noqa: F811
    """忙碌工作流 500 条（撑破旧的全局 400 窗口）+ 安静工作流连败 5 次。

    安静那个的运行更早，所以在全局倒序里排在 500 条之后——
    旧写法下它一条都进不了窗口。
    """
    with services.workflow_store.storage._connect() as conn:
        _app(conn, BUSY, "忙碌的")
        _app(conn, QUIET, "安静的")
        _runs(conn, BUSY, ["succeeded"] * 500)
        _runs(conn, QUIET, ["failed"] * 5, offset=1000)
    return services


def _item(report, name):
    return next(i for i in report["items"] if i["workflow"] == name)


@pytest.mark.asyncio
async def test_a_quiet_workflow_s_failures_are_still_seen(crowded):
    report = await build_health(crowded)
    assert _item(report, "安静的")["fail_streak"] == 5, (
        "安静工作流被忙碌工作流挤出了窗口，连败看不见")


@pytest.mark.asyncio
async def test_it_is_actually_counted_as_broken(crowded):
    report = await build_health(crowded)
    assert _item(report, "安静的")["state"] != "ok"
    assert report["counts"]["broken"] >= 1


@pytest.mark.asyncio
async def test_the_reason_survives_too(crowded):
    """判成反复失败却给不出原因，等于让人干瞪眼。"""
    assert _item(await build_health(crowded), "安静的")["last_error"]


@pytest.mark.asyncio
async def test_the_busy_workflow_is_still_judged_correctly(crowded):
    """分窗不能把忙碌那个judge错——它 500 次全成功。"""
    item = _item(await build_health(crowded), "忙碌的")
    assert item["fail_streak"] == 0
    assert item["state"] == "ok"


@pytest.mark.asyncio
async def test_the_window_is_eight_per_workflow_not_more(crowded):
    """窗口本身还得是 8：无限放大就成了全量扫描。"""
    with crowded.workflow_store.storage._connect() as conn:
        _runs(conn, QUIET, ["succeeded"] * 20, offset=2000)
    # 20 条更早的成功不该冲淡最近 5 次失败（它们在窗口外）
    assert _item(await build_health(crowded), "安静的")["fail_streak"] == 5
