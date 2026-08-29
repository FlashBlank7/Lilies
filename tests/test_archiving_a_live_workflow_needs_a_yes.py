"""还在用的工作流，不能凭一句话就收起来。

回归背景（2026-08-29）：tidy_workflows 的说明写的是
「收起废弃草稿（从没发布、从没成功跑过、放了一阵子）」，
但 archive 这一支对**任何**名字都照收。
一次探测里，一句「把词频统计删掉」就把一个已发布、在跑的工作流收走了
（事后用 restore 复原）。

收起来是可逆的，但它**同时会停掉定时**——一份每天早上八点的日报
会从此不再来，而没有人会收到通知。可逆不等于无害。

闸不是"问模型确认"（它会自己替业主答应），而是查**业主原话**里
有没有明确的确认词。和返修那道闸同一个思路：
会造成损失的动作，依据必须落在业主真说过的字上。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_platform.assistant_agent import WorkflowConcierge
from helpers_overview import services  # noqa: F401


def _agent(owner_words: str, *, in_use: bool = True):
    services = MagicMock()
    services.workflow_store.set_archived = AsyncMock(return_value={})
    agent = WorkflowConcierge(services, MagicMock())
    agent._owner_words = owner_words
    agent._resolve_app = AsyncMock(
        return_value={"id": "a1", "name": "服务器GPU日报", "active_version": 3})
    agent._still_in_use = AsyncMock(return_value=in_use)
    return agent, services


async def _archive(agent, name="服务器GPU日报"):
    return await agent._exec("tidy_workflows",
                             {"action": "archive", "name_or_id": name}, {})


@pytest.mark.asyncio
async def test_a_bare_request_is_held_until_the_owner_confirms():
    agent, services = _agent("把服务器GPU日报收起来")
    result = await _archive(agent)
    assert "error" in result, result
    assert "还在用" in result["error"]
    services.workflow_store.set_archived.assert_not_awaited()


@pytest.mark.asyncio
async def test_asking_to_delete_is_not_a_confirmation_either():
    """就是这句话惹的祸——「删掉」听起来更狠，但它同样不是"确认"。"""
    agent, services = _agent("把词频统计删掉")
    result = await _archive(agent)
    assert "error" in result
    services.workflow_store.set_archived.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_explicit_yes_lets_it_through():
    for words in ("确认收起", "确定，收吧", "没错，就这么办", "停了也行，收吧"):
        agent, services = _agent(words)
        result = await _archive(agent)
        assert "error" not in result, f"{words} → {result}"
        services.workflow_store.set_archived.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_disused_draft_needs_no_ceremony():
    """这个工具的正主就是废弃草稿——对它们别加摩擦。"""
    agent, services = _agent("把这些没用的收拾掉", in_use=False)
    result = await _archive(agent)
    assert "error" not in result
    services.workflow_store.set_archived.assert_awaited_once()


@pytest.mark.asyncio
async def test_restoring_is_never_gated():
    """拿回来是纯粹的好事，不该问。"""
    agent, services = _agent("把服务器GPU日报拿回来")
    result = await agent._exec(
        "tidy_workflows", {"action": "restore", "name_or_id": "服务器GPU日报"}, {})
    assert "error" not in result
    services.workflow_store.set_archived.assert_awaited_once()


@pytest.mark.asyncio
async def test_the_message_says_what_收起来_costs():
    """光说"不行"没用——要说清收起来会发生什么，业主才知道怎么答。"""
    agent, _ = _agent("收起来")
    error = (await _archive(agent))["error"]
    assert "定时" in error and "确认" in error


# 下面这几条真跑 _still_in_use 本身（上面的用例把它替换掉了，
# 只验闸的行为；判据对不对得单独验）。


@pytest.mark.asyncio
async def test_a_published_workflow_that_ran_recently_counts_as_in_use(services):  # noqa: F811
    from helpers_overview import _seed

    _seed(services, real_runs=["succeeded"])
    agent = WorkflowConcierge(services, MagicMock())
    assert await agent._still_in_use({"id": "app-1", "active_version": 1}) is True


@pytest.mark.asyncio
async def test_an_unpublished_draft_is_never_in_use(services):  # noqa: F811
    from helpers_overview import _seed

    _seed(services, real_runs=["succeeded"])
    agent = WorkflowConcierge(services, MagicMock())
    assert await agent._still_in_use({"id": "app-1", "active_version": None}) is False


@pytest.mark.asyncio
async def test_a_schedule_alone_counts_even_with_no_recent_runs(services):  # noqa: F811
    """定时还挂着就算在用——哪怕它一次都还没跑成过。

    收起来会把这个定时停掉，而这正是最难被发现的那种损失。
    """
    from helpers_overview import SCHEDULED_SNAPSHOT, _seed

    _seed(services, real_runs=[], version_snapshot=SCHEDULED_SNAPSHOT)
    agent = WorkflowConcierge(services, MagicMock())
    assert await agent._still_in_use({"id": "app-1", "active_version": 1}) is True


@pytest.mark.asyncio
async def test_an_old_published_workflow_with_nothing_going_on_is_not_in_use(services):  # noqa: F811
    """发布过、但很久没跑、也没定时——这种就是该收的。"""
    _seed_old(services)
    agent = WorkflowConcierge(services, MagicMock())
    assert await agent._still_in_use({"id": "app-1", "active_version": 1}) is False


def _seed_old(services):
    from helpers_overview import _seed

    _seed(services, real_runs=[])
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES('old','app-1',1,NULL,'succeeded','{}','{}',NULL,"
            "datetime('now','-90 days'),datetime('now','-90 days'))")
