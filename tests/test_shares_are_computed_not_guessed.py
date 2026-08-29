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
