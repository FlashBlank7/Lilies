"""某一天的失败分别属于谁——总数和归属之间要有东西连着。

回归背景（2026-08-29 真机）：问「昨天一共有几次失败的运行」，管家答

    昨天（8月28日）失败的运行共有 5 次。
    其中「文本行数与净字数统计」有一次失败记录……

5 是对的，"一次"是错的——那 5 次**全是**它。

原因不在模型：
· week 只给每天的总数（5），没有分工作流；
· recent_failures 是**整窗合并**的（一个原因一行，带整窗合计 13）；
· recent_runs 能按天查，但必须先指定是哪个工作流。

也就是说"某天几次"和"是谁"之间没有任何数据把它们连起来，只能猜；
而它把清单里的一**行**读成了一**次**。这是本周第三次同一个病：
给一页数据、不说这是一页（或不说这行代表什么），模型就当它是全部。

修法照旧在数据这一侧：直接给每天每个工作流的失败次数。
"""

from __future__ import annotations

import pytest

from agent_platform.overview import build_overview
from helpers_overview import _seed, services  # noqa: F401


def _add_run(services, app_id: str, status: str, days_ago: int, run_id: str,
             version: int | None = 1) -> None:
    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'{}','{}',?,datetime('now',?),datetime('now',?))",
            (run_id, app_id, version, None if version else 3, status,
             "缺少必填输入" if status == "failed" else None,
             f"-{days_ago} days", f"-{days_ago} days"))


def _add_app(services, app_id: str, name: str) -> None:
    from helpers_overview import PLAIN_SNAPSHOT

    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,active_version,"
            "created_at,updated_at) VALUES(?,?,'','','workflow',1,"
            "datetime('now'),datetime('now'))", (app_id, name))
        conn.execute(
            "INSERT INTO application_versions(application_id,version,snapshot_json,"
            "content_hash,validation_report_json,created_at) "
            "VALUES(?,1,?,?,'{}',datetime('now','-30 days'))",
            (app_id, PLAIN_SNAPSHOT, "g" * 64))


def _lookup(rows, day_offset: int, workflow: str) -> int | None:
    from datetime import datetime, timedelta, timezone

    day = (datetime.now(timezone.utc) - timedelta(days=day_offset)).strftime("%Y-%m-%d")
    for row in rows:
        if row["day"] == day and row["workflow"] == workflow:
            return row["failed"]
    return None


@pytest.mark.asyncio
async def test_a_days_failures_are_attributed_to_the_workflow(services):
    """五次失败全是一个工作流的——就要看得出是"5"而不是"有一条记录"。"""
    _seed(services, real_runs=[])
    for index in range(5):
        _add_run(services, "app-1", "failed", 1, f"y-{index}")
    data = await build_overview(services)
    self_row = _lookup(data["week_failures"], 1, "被测工作流")
    assert self_row == 5, data["week_failures"]


@pytest.mark.asyncio
async def test_two_days_are_kept_apart(services):
    """昨天和前天的次数不能混在一起——问的就是"某一天"。"""
    _seed(services, real_runs=[])
    for index in range(5):
        _add_run(services, "app-1", "failed", 1, f"y-{index}")
    for index in range(8):
        _add_run(services, "app-1", "failed", 2, f"d-{index}")
    data = await build_overview(services)
    assert _lookup(data["week_failures"], 1, "被测工作流") == 5
    assert _lookup(data["week_failures"], 2, "被测工作流") == 8


@pytest.mark.asyncio
async def test_two_workflows_on_the_same_day_are_kept_apart(services):
    """同一天两个工作流都失败了——总数拆到人头上才有意义。"""
    _seed(services, real_runs=[])
    _add_app(services, "app-2", "另一个工作流")
    for index in range(5):
        _add_run(services, "app-1", "failed", 1, f"a-{index}")
    for index in range(2):
        _add_run(services, "app-2", "failed", 1, f"b-{index}")
    data = await build_overview(services)
    assert _lookup(data["week_failures"], 1, "被测工作流") == 5
    assert _lookup(data["week_failures"], 1, "另一个工作流") == 2


@pytest.mark.asyncio
async def test_it_agrees_with_the_week_totals(services):
    """两处口径必须一致：拆开的和加起来的对不上，面板就在自己跟自己打架。

    这条是整个改动的要害——真机上「本周失败 25 次而清单只解释得了 13 次」
    就是这么来的（另外 12 次属于已归档工作流）。
    """
    _seed(services, real_runs=[])
    _add_app(services, "app-2", "另一个工作流")
    for index in range(5):
        _add_run(services, "app-1", "failed", 1, f"a-{index}")
    for index in range(2):
        _add_run(services, "app-2", "failed", 1, f"b-{index}")
    _add_run(services, "app-1", "succeeded", 1, "ok-1")
    data = await build_overview(services)
    from datetime import datetime, timedelta, timezone

    day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    week_total = next(d["fail"] for d in data["week"] if d["date"] == day)
    split_total = sum(r["failed"] for r in data["week_failures"] if r["day"] == day)
    assert week_total == split_total == 7


@pytest.mark.asyncio
async def test_draft_selftests_are_excluded_here_too(services):
    """草稿自测不算真实运行——和 week、清单、体检同一个口径。

    漏掉这条过滤的话，拆出来的数会比 week 的总数大，
    用户看到的就是"5 次失败，其中它 25 次"。
    """
    _seed(services, real_runs=[])
    for index in range(5):
        _add_run(services, "app-1", "failed", 1, f"y-{index}")
    for index in range(20):
        _add_run(services, "app-1", "failed", 1, f"draft-{index}", version=None)
    data = await build_overview(services)
    assert _lookup(data["week_failures"], 1, "被测工作流") == 5


@pytest.mark.asyncio
async def test_archived_workflows_are_excluded_here_too(services):
    """收起来的工作流不占面板——和 week 同一个口径，否则两处又对不上。"""
    _seed(services, real_runs=[])
    _add_app(services, "app-2", "已收起来的")
    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute("UPDATE applications SET archived_at=datetime('now') WHERE id='app-2'")
    for index in range(3):
        _add_run(services, "app-2", "failed", 1, f"z-{index}")
    data = await build_overview(services)
    assert _lookup(data["week_failures"], 1, "已收起来的") is None


@pytest.mark.asyncio
async def test_older_than_the_window_is_not_included(services):
    """周视图是 7 天，拆开的那份也得是 7 天——窗口不同就又对不上了。"""
    _seed(services, real_runs=[])
    for index in range(4):
        _add_run(services, "app-1", "failed", 30, f"old-{index}")
    data = await build_overview(services)
    assert _lookup(data["week_failures"], 30, "被测工作流") is None


@pytest.mark.asyncio
async def test_the_model_sees_it_in_plain_chinese(services):
    """挂没挂上给模型的那一份才是关键——算出来没端上去等于没算。"""
    from agent_platform.assistant_agent import WorkflowConcierge

    _seed(services, real_runs=[])
    for index in range(5):
        _add_run(services, "app-1", "failed", 1, f"y-{index}")
    concierge = WorkflowConcierge(services, settings=None)
    result = await concierge._exec("platform_overview", {}, user={})
    rows = result["week_failures"]
    assert rows and set(rows[0]) == {"日期", "工作流", "这天失败几次"}, rows
    assert rows[0]["这天失败几次"] == 5
