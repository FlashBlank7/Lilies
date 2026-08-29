"""发布了却一次都没跑过的工作流，不能混在"正常"里一笔带过。

四个状态是按"哪儿坏了"分的：没定时→不 stale、没终态→不 broken、
没在跑→不 waiting，于是从没跑过的那个落到 ok。面板说正常、管家答"都正常"。
可"正常"是个结论，而这种工作流**一条证据都没有**——第一次跑会怎样谁也不知道。
（有定时的那种已经被判 stale 了，见 test_health_report 里那条；
漏的是没定时、纯手动触发的那一类。）

也不该判成"有问题"：它确实没坏，每个刚发布的工作流都要经过这一阶段，
判成问题就是天天报警。所以既不说好也不说坏，把事实单列出来。

顺带钉一件容易看走眼的事：items 里的 `runs` / `last_run` / `last_success`
**都是窗口内**的数（名字不像）。"从没跑过"和"最近没跑"在那几个字段里
长得一模一样，全是 0 和 None。分得开这两件事的只有 `ever_ran`。
"""

from __future__ import annotations

import pytest

from agent_platform.overview import build_health

from helpers_overview import PLAIN_SNAPSHOT, _seed, services  # noqa: F401


def _manual(services, **kwargs):
    """没定时、纯手动触发的工作流——草稿快照也得是不带定时的那份。

    体检读的是**草稿**快照来判有没有定时（夹具默认给的是带定时的），
    默认值直接用会走到 stale 那一支，测的就不是这里要测的东西了。
    """
    _seed(services, draft_snapshot=PLAIN_SNAPSHOT, **kwargs)


def _insert_old_run(services, *, app_id="app-1", days_ago=30):
    """补一条窗口之外的历史运行——夹具只会插"几秒前"的。"""
    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES(?,?,1,NULL,'succeeded','{}','{}',NULL,"
            "datetime('now', ?),datetime('now', ?))",
            ("old-1", app_id, f"-{days_ago} days", f"-{days_ago} days"))


@pytest.mark.asyncio
async def test_a_workflow_that_never_ran_is_listed_apart(services):  # noqa: F811
    """一次没跑过：状态仍是 ok（它没坏），但要出现在单列的那一格里。"""
    _manual(services, real_runs=[])
    report = await build_health(services)
    item = report["items"][0]
    assert item["ever_ran"] is False
    assert item["state"] == "ok", "没跑过不等于坏了，不该报警"
    assert report["never_ran"] == [item["workflow"]]


@pytest.mark.asyncio
async def test_a_workflow_that_ran_is_not_listed(services):  # noqa: F811
    """反向那一条：跑过的不许进这一格，否则"单列"等于全列。"""
    _manual(services, real_runs=["succeeded"] * 2)
    report = await build_health(services)
    assert report["items"][0]["ever_ran"] is True
    assert report["never_ran"] == []


@pytest.mark.asyncio
async def test_quiet_lately_is_not_the_same_as_never(services):  # noqa: F811
    """这条是整件事的要害。

    30 天前跑过、窗口里一片空白——窗口字段（runs / last_run）会和
    "从没跑过"完全一样。只有 ever_ran 分得开。
    把 ever_ran 改成读窗口内那份 stats，这条会红，上面两条不会。
    """
    _manual(services, real_runs=[])
    _insert_old_run(services, days_ago=30)
    report = await build_health(services, days=7)
    item = report["items"][0]
    assert item["runs"] == 0 and item["last_run"] is None, "前提：窗口里确实看不到"
    assert item["ever_ran"] is True, "但它是跑过的，只是最近安静"
    assert report["never_ran"] == []
    assert item["last_run_ever"] is not None, "全历史那份要指得出是什么时候"
