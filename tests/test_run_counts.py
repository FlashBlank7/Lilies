"""数运行次数：任意时间段，一次问清。

回归背景（2026-08-29 真机探测，两问同一个缺口）：

1. 问「上上周有几次运行」——它连打了 **25 次工具调用**：
   先看面板（只有 7 天），覆盖不到，就开始一个工作流一天一天地
   recent_runs 翻，翻了 24 次。答案对（0 次），但这个代价在
   工作流一多就必然崩：要么超时，要么翻到一半自己下结论。

2. 问「文本行数与净字数统计的成功率是多少」——它拿 7 天窗口里的
   两天算出 84%，而全量是 81.4%（57 成 / 70 次）。
   窗口里的数被当成了全量，而且回答里没说那是个窗口。

平台此前能数的只有：今天、最近 7 天、某个工作流的全部历史、
某个工作流的某一天。**任意时间段跨全部工作流**这一格是空的，
而"某段时间跑了多少次"恰恰是最常问的一类。

一句 GROUP BY 就有的东西，不该让它去翻。
"""

from __future__ import annotations

import pytest

from helpers_overview import PLAIN_SNAPSHOT, _seed, services  # noqa: F401


def _add_app(services, app_id: str, name: str, archived: bool = False) -> None:
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,active_version,"
            "archived_at,created_at,updated_at) VALUES(?,?,'','','workflow',1,?,"
            "datetime('now'),datetime('now'))",
            (app_id, name, "2026-01-01T00:00:00+00:00" if archived else None))
        conn.execute(
            "INSERT INTO application_versions(application_id,version,snapshot_json,"
            "content_hash,validation_report_json,created_at) "
            "VALUES(?,1,?,?,'{}',datetime('now','-30 days'))",
            (app_id, PLAIN_SNAPSHOT, app_id.ljust(64, "x")[:64]))
        # 草稿行不能少：list_applications 是 JOIN 草稿的，
        # 少了这行按名字就找不到它——夹具太瘦造成的假阴性。
        conn.execute(
            "INSERT INTO application_drafts(application_id,revision,snapshot_json,"
            "content_hash,validation_report_json,updated_at) "
            "VALUES(?,1,?,?,'{}',datetime('now'))",
            (app_id, PLAIN_SNAPSHOT, app_id.ljust(64, "y")[:64]))


def _add_run(services, app_id: str, status: str, days_ago: int, run_id: str,
             published: bool = True) -> None:
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES(?,?,?,?,?,'{}','{}',NULL,datetime('now',?),datetime('now',?))",
            (run_id, app_id, 1 if published else None, None if published else 3,
             status, f"-{days_ago} days", f"-{days_ago} days"))


def _day(days_ago: int) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


async def _count(services, **args):
    from agent_platform.assistant_agent import WorkflowConcierge

    # 按名字挑工作流走的是真 WorkflowStorage.list_applications——
    # 夹具的 workflow_store 只带了 storage，这里把真方法接上，
    # 不然"找不到该工作流"会因为夹具太瘦而假阳性。
    services.workflow_store.list_applications = services._store.list_applications
    return await WorkflowConcierge(services, settings=None)._exec(
        "run_counts", args, user={})


@pytest.mark.asyncio
async def test_it_counts_a_range_across_every_workflow(services):
    """一次问清：这才是那 25 次调用要换掉的东西。"""
    _seed(services, real_runs=[])
    _add_app(services, "app-2", "第二个")
    for index in range(5):
        _add_run(services, "app-1", "failed", 3, f"a-{index}")
    for index in range(2):
        _add_run(services, "app-2", "succeeded", 4, f"b-{index}")
    _add_run(services, "app-1", "succeeded", 30, "far")     # 范围外
    result = await _count(services, since=_day(5), until=_day(2))
    assert result["一共跑了几次"] == 7
    assert result["按情况计数"] == {"没跑成": 5, "跑成了": 2}


@pytest.mark.asyncio
async def test_the_range_ends_are_inclusive(services):
    """「8月27号到28号」里的 27 和 28 都得算上——差一天就差一整天的数。"""
    _seed(services, real_runs=[])
    _add_run(services, "app-1", "succeeded", 2, "start-day")
    _add_run(services, "app-1", "succeeded", 1, "end-day")
    result = await _count(services, since=_day(2), until=_day(1))
    assert result["一共跑了几次"] == 2


@pytest.mark.asyncio
async def test_with_no_range_it_counts_everything(services):
    """成功率问的是全量。不给起止就该是全部历史，不是某个窗口。"""
    _seed(services, real_runs=[])
    for index in range(57):
        _add_run(services, "app-1", "succeeded", 40, f"ok-{index}")
    for index in range(13):
        _add_run(services, "app-1", "failed", 40, f"bad-{index}")
    result = await _count(services)
    assert result["一共跑了几次"] == 70
    assert result["按情况计数"]["跑成了"] == 57
    assert result["按情况计数"]["没跑成"] == 13


@pytest.mark.asyncio
async def test_it_splits_by_workflow(services):
    """「哪个跑得最多」也是这一格的问题，别再一个个翻。"""
    _seed(services, real_runs=[])
    _add_app(services, "app-2", "第二个")
    for index in range(4):
        _add_run(services, "app-1", "succeeded", 1, f"a-{index}")
    _add_run(services, "app-2", "failed", 1, "b-0")
    result = await _count(services)
    rows = {row["工作流"]: row for row in result["每个工作流各多少次"]}
    assert rows["被测工作流"]["总次数"] == 4
    assert rows["第二个"]["没跑成"] == 1
    # 最多的排最前面——问「哪个最多」时不该还要它自己比一遍
    assert result["每个工作流各多少次"][0]["工作流"] == "被测工作流"


@pytest.mark.asyncio
async def test_it_splits_by_day(services):
    _seed(services, real_runs=[])
    for index in range(8):
        _add_run(services, "app-1", "failed", 2, f"d2-{index}")
    for index in range(5):
        _add_run(services, "app-1", "failed", 1, f"d1-{index}")
    result = await _count(services)
    rows = {row["日期"]: row for row in result["按天"]}
    assert rows[_day(2)]["没跑成"] == 8
    assert rows[_day(1)]["没跑成"] == 5


@pytest.mark.asyncio
async def test_one_workflow_can_be_singled_out(services):
    _seed(services, real_runs=[])
    _add_app(services, "app-2", "第二个")
    _add_run(services, "app-1", "succeeded", 1, "a-0")
    _add_run(services, "app-2", "succeeded", 1, "b-0")
    result = await _count(services, name_or_id="第二个")
    assert result["一共跑了几次"] == 1
    assert "第二个" in result["问的是哪一段"]


@pytest.mark.asyncio
async def test_an_unknown_workflow_says_so(services):
    _seed(services, real_runs=[])
    result = await _count(services, name_or_id="根本没有这个")
    assert "找不到" in result.get("error", "")


@pytest.mark.asyncio
async def test_draft_selftests_do_not_count(services):
    """口径要和面板、体检一致，不然同一个问题两处两个答案。"""
    _seed(services, real_runs=[])
    _add_run(services, "app-1", "succeeded", 1, "real-x")
    for index in range(20):
        _add_run(services, "app-1", "failed", 1, f"draft-{index}", published=False)
    result = await _count(services)
    assert result["一共跑了几次"] == 1


@pytest.mark.asyncio
async def test_archived_workflows_do_not_count(services):
    _seed(services, real_runs=[])
    _add_app(services, "app-2", "已收起来的", archived=True)
    _add_run(services, "app-1", "succeeded", 1, "a-0")
    for index in range(9):
        _add_run(services, "app-2", "succeeded", 1, f"z-{index}")
    result = await _count(services)
    assert result["一共跑了几次"] == 1


@pytest.mark.asyncio
async def test_an_empty_range_says_it_is_really_zero(services):
    """「查不到」和「真的是零」是两回事，得说清楚——不然它会去翻。

    那 25 次调用就是这么来的：面板覆盖不到，它不确定是"没有"
    还是"没查到"，于是一天一天去证实。
    """
    _seed(services, real_runs=[])
    _add_run(services, "app-1", "succeeded", 1, "a-0")
    result = await _count(services, since=_day(40), until=_day(35))
    assert result["一共跑了几次"] == 0
    assert "真的没跑" in result["这一段确实是零"]


@pytest.mark.asyncio
async def test_a_long_range_says_the_day_list_was_cut(services):
    """按天可能有几百行。截了要说，而且要说清总数没被截。

    这是本周反复出现的那个病：给一页数据、不说这是一页。
    """
    _seed(services, real_runs=[])
    for offset in range(80):
        _add_run(services, "app-1", "succeeded", offset, f"d-{offset}")
    result = await _count(services)
    assert result["一共跑了几次"] == 80          # 总数是整段的
    assert len(result["按天"]) == 62             # 列表截断
    assert "80" in result["按天只列了最近 62 天"]
    assert "没有被截" in result["按天只列了最近 62 天"]


@pytest.mark.asyncio
async def test_the_counts_come_before_the_long_lists(services):
    """字段顺序有讲究：整份结果超过 4000 字会被截，截的是**后面**那截。

    数字排在长列表后面的话，最该看到的那几个数会先被切掉。
    """
    _seed(services, real_runs=[])
    for offset in range(30):
        _add_run(services, "app-1", "succeeded", offset, f"d-{offset}")
    keys = list(await _count(services))
    assert keys.index("一共跑了几次") < keys.index("按天")
    assert keys.index("按情况计数") < keys.index("按天")
    assert keys.index("按情况计数") < keys.index("每个工作流各多少次")


@pytest.mark.asyncio
async def test_statuses_are_in_plain_chinese(services):
    """状态码不能从这条新路漏出去——每开一个出口就得重新数一遍。"""
    _seed(services, real_runs=[])
    _add_run(services, "app-1", "failed", 1, "a-0")
    _add_run(services, "app-1", "running", 1, "a-1")
    situations = (await _count(services))["按情况计数"]
    assert "failed" not in situations and "running" not in situations, situations
