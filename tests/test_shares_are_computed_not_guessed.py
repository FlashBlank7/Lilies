"""占比这类要做除法的数，平台自己算好给出去，别留给模型算。

来由：准确性哨兵里「已发布的占比是多少」连着两轮都得重问一遍才答对，
而分子分母（3 和 15）明明就摆在同一份工具返回值里。
这和"别让它数行数"是同一条——真机上它逐行数 published_version，
把 3 个已发布数成 2 个，答「13 个草稿」（真值 12）。
**平台算得出的，别留给模型算。**

写成带 % 的字符串是有意的：0.2 和 20 两种写法之间也会晃一次。
"""

from __future__ import annotations

import pytest

from helpers_overview import PLAIN_SNAPSHOT, services  # noqa: F401


def _add_app(services, app_id: str, name: str, published: bool) -> None:
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,active_version,"
            "archived_at,created_at,updated_at) VALUES(?,?,'','','workflow',?,NULL,"
            "datetime('now'),datetime('now'))",
            (app_id, name, 1 if published else None))
        if published:
            conn.execute(
                "INSERT INTO application_versions(application_id,version,snapshot_json,"
                "content_hash,validation_report_json,created_at) "
                "VALUES(?,1,?,?,'{}',datetime('now','-30 days'))",
                (app_id, PLAIN_SNAPSHOT, app_id.ljust(64, "x")[:64]))
        # 草稿行不能少：list_applications 是 JOIN 草稿的
        conn.execute(
            "INSERT INTO application_drafts(application_id,revision,snapshot_json,"
            "content_hash,validation_report_json,updated_at) "
            "VALUES(?,1,?,?,'{}',datetime('now'))",
            (app_id, PLAIN_SNAPSHOT, app_id.ljust(64, "y")[:64]))


async def _list(services, **args) -> dict:
    from agent_platform.assistant_agent import WorkflowConcierge

    services.workflow_store.list_applications = services._store.list_applications
    return await WorkflowConcierge(services, settings=None)._exec(
        "list_workflows", args, user={})


@pytest.mark.asyncio
async def test_the_share_matches_the_two_counts(services):  # noqa: F811
    """占比必须和同一份返回值里的两个数对得上——自相矛盾比没有更糟。"""
    for index in range(1):
        _add_app(services, f"pub-{index}", f"已发布{index}", published=True)
    for index in range(3):
        _add_app(services, f"draft-{index}", f"草稿{index}", published=False)
    result = await _list(services, only_published=False)
    assert (result["一共几个"], result["已发布几个"]) == (4, 1)
    assert result["已发布占比"] == "25%"


@pytest.mark.asyncio
async def test_a_repeating_fraction_is_not_dumped_raw(services):  # noqa: F811
    """1/3 这种除不尽的，给一位小数就够了，别把 33.33333 印出来。"""
    _add_app(services, "pub-0", "已发布", published=True)
    for index in range(2):
        _add_app(services, f"draft-{index}", f"草稿{index}", published=False)
    assert (await _list(services, only_published=False))["已发布占比"] == "33.3%"


@pytest.mark.asyncio
async def test_the_share_says_what_it_divided_by(services):  # noqa: F811
    """"占比"本身有歧义（分母含不含草稿？含不含收起来的？）——口径要写明。"""
    _add_app(services, "pub-0", "已发布", published=True)
    basis = (await _list(services))["占比是拿什么算的"]
    assert "草稿" in basis and "收起来" in basis


@pytest.mark.asyncio
async def test_an_empty_platform_does_not_divide_by_zero(services):  # noqa: F811
    """一个工作流都没有时，不许崩，也不许给出 0% 这种像是有分母的答案。"""
    result = await _list(services)
    assert result["一共几个"] == 0
    assert result["已发布占比"] == "还没有工作流"


class TestEachWorkflowSaysWhenItLastRan:
    """「哪个工作流最久没跑过」——手里没这个数，它就会自己凑一个。

    真机问出来的（2026-08-29）：答「词频统计——最后一次跑是在 08-26」。
    两处都错：那个工作流最后一次是 08-28 19:14，而最久没动的是
    「文本行数与净字数统计」（08-28 13:51）。
    原因是工具给了各工作流的**次数**、给了按天的**总数**，
    唯独没有"某个工作流最后一次是哪天"。一句 MAX 就有的东西，别留给它凑。
    """

    @staticmethod
    def _add_run(storage, app_id: str, when: str) -> None:
        with storage._connect() as conn:
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,"
                "status,state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,?,1,NULL,'succeeded','{}','{}',NULL,?,?)",
                (f"{app_id}-{when}", app_id, when, when))

    @pytest.mark.asyncio
    async def test_the_last_run_time_is_given(self, services):  # noqa: F811
        _add_app(services, "pub-0", "跑过的", published=True)
        self._add_run(services.workflow_store.storage, "pub-0",
                      "2026-08-28T19:14:40+00:00")
        self._add_run(services.workflow_store.storage, "pub-0",
                      "2026-08-27T01:00:00+00:00")
        [row] = (await _list(services))["workflows"]
        assert row["最后一次是什么时候"] == "2026-08-28 19:14", row

    @pytest.mark.asyncio
    async def test_a_workflow_that_never_ran_says_so(self, services):  # noqa: F811
        """没跑过就说没跑过，别给一个空字符串让它猜。"""
        _add_app(services, "pub-0", "没跑过的", published=True)
        [row] = (await _list(services))["workflows"]
        assert row["最后一次是什么时候"] == "从没跑过"

    @pytest.mark.asyncio
    async def test_the_time_is_readable_not_iso(self, services):  # noqa: F811
        """给人看的时间别留 T、别甩一串微秒。"""
        _add_app(services, "pub-0", "跑过的", published=True)
        self._add_run(services.workflow_store.storage, "pub-0",
                      "2026-08-28T19:14:40.942353+00:00")
        when = (await _list(services))["workflows"][0]["最后一次是什么时候"]
        assert "T" not in when and len(when) == 16, when

    @pytest.mark.asyncio
    async def test_draft_selftests_do_not_count(self, services):  # noqa: F811
        """搭建期自测（version 为空）不算真实运行——口径要和别处一致。"""
        _add_app(services, "pub-0", "跑过的", published=True)
        with services.workflow_store.storage._connect() as conn:
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,"
                "status,state_json,outputs_json,error,created_at,updated_at) "
                "VALUES('draft-run','pub-0',NULL,3,'succeeded','{}','{}',NULL,"
                "'2026-08-29T23:00:00+00:00','2026-08-29T23:00:00+00:00')")
        [row] = (await _list(services))["workflows"]
        assert row["最后一次是什么时候"] == "从没跑过", row
