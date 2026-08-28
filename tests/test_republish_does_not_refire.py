"""重新发布一个带定时的工作流，当天不该再跑一次。

真机证据（服务器GPU日报，2026-08-28）：
  00:00:19  按时开火（版本 1）
  05:39:18  发布版本 3
  05:39:24  又开了一炮 ← 发布后 6 秒，而不是次日 08:00

开火去重键原本是 (应用, **版本**, 节点, 本地日期)：一重新发布，
当天的记录就查不到了，而「今天 08:00 已过」仍然成立，于是立刻补一炮。
日报只是多发一份；换成发邮件、写 ERP 的工作流就是真的重复副作用。
"""

import asyncio
from pathlib import Path

import pytest

from agent_platform.config import Settings
from agent_platform.storage import Storage
from agent_platform.workflow_storage import WorkflowStorage

DATE = "2026-08-28"


@pytest.fixture()
def store(tmp_path: Path) -> WorkflowStorage:
    # 真 SQLite，建表走平台自己的初始化路径（同 helpers_overview 的理由）
    settings = Settings(api_token="t", data_dir=tmp_path / "data",
                        workspace_root=tmp_path / "ws")
    settings.prepare()
    storage = Storage(settings.data_dir)
    store = WorkflowStorage(storage)

    async def _init():
        await storage.initialize()
        await store.initialize()

    asyncio.run(_init())
    return store


def _app_row(store: WorkflowStorage, app_id: str) -> None:
    with store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,"
            "created_at,updated_at) VALUES(?,?,?,?,?,datetime('now'),datetime('now'))",
            (app_id, "日报", "", "", "workflow"))


@pytest.mark.asyncio
async def test_republish_does_not_refire_the_same_day(store: WorkflowStorage) -> None:
    _app_row(store, "a1")
    assert await store.claim_schedule_fire("a1", 1, "sched", DATE) is True
    # 同一天、同一个节点，只是版本变了——这正是真机上多开那一炮的形状
    assert await store.claim_schedule_fire("a1", 3, "sched", DATE) is False


@pytest.mark.asyncio
async def test_same_version_twice_is_still_deduped(store: WorkflowStorage) -> None:
    _app_row(store, "a1")
    assert await store.claim_schedule_fire("a1", 1, "sched", DATE) is True
    assert await store.claim_schedule_fire("a1", 1, "sched", DATE) is False


@pytest.mark.asyncio
async def test_next_day_still_fires(store: WorkflowStorage) -> None:
    _app_row(store, "a1")
    assert await store.claim_schedule_fire("a1", 1, "sched", DATE) is True
    assert await store.claim_schedule_fire("a1", 1, "sched", "2026-08-29") is True


@pytest.mark.asyncio
async def test_a_second_schedule_node_is_independent(store: WorkflowStorage) -> None:
    _app_row(store, "a1")
    assert await store.claim_schedule_fire("a1", 1, "sched", DATE) is True
    assert await store.claim_schedule_fire("a1", 1, "sched-2", DATE) is True


@pytest.mark.asyncio
async def test_other_applications_are_unaffected(store: WorkflowStorage) -> None:
    _app_row(store, "a1")
    _app_row(store, "a2")
    assert await store.claim_schedule_fire("a1", 1, "sched", DATE) is True
    assert await store.claim_schedule_fire("a2", 1, "sched", DATE) is True


@pytest.mark.asyncio
async def test_failed_fire_can_still_be_retried_across_a_republish(
        store: WorkflowStorage) -> None:
    """失败重试不能被这次收紧顺手掐掉。"""
    _app_row(store, "a1")
    assert await store.claim_schedule_fire("a1", 1, "sched", DATE) is True
    with store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES(?,?,?,NULL,?,'{}','{}','boom',datetime('now'),datetime('now'))",
            ("run-1", "a1", 1, "failed"))
        conn.execute(
            "UPDATE schedule_fires SET run_id='run-1', last_attempt_at=NULL"
            " WHERE application_id='a1'")
    assert await store.claim_schedule_fire("a1", 3, "sched", DATE) is True
