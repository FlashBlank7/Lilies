"""调度器自身的死活信号。

体检报「定时没按时开火」时，用户的下一步是查调度器——
没有这个信号，那条建议就落不了地（只能干说"去看看调度器还在不在"）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_platform.scheduler import WorkflowScheduler


def _scheduler(poll_seconds: float = 30) -> WorkflowScheduler:
    stub = SimpleNamespace()
    return WorkflowScheduler(
        storage=stub, workflow_store=stub, blocks=stub, runtime=stub,
        harness=SimpleNamespace(worker_id="w1"), durable_jobs=stub,
        poll_seconds=poll_seconds)


def test_never_started_is_not_alive():
    health = _scheduler().health()
    assert health["running"] is False
    assert health["alive"] is False
    assert health["last_tick_at"] is None
    assert health["seconds_since_tick"] is None


def test_recent_tick_with_running_task_is_alive():
    sched = _scheduler()
    sched.task = SimpleNamespace(done=lambda: False)
    sched.last_tick_at = datetime.now(timezone.utc)
    sched.tick_count = 7
    health = sched.health()
    assert health["alive"] is True
    assert health["tick_count"] == 7
    assert health["seconds_since_tick"] < 5


def test_stale_tick_is_not_alive():
    """任务还在但很久没轮询——卡死了，比"没启动"更需要报出来。"""
    sched = _scheduler(poll_seconds=30)
    sched.task = SimpleNamespace(done=lambda: False)
    sched.last_tick_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    health = sched.health()
    assert health["running"] is True
    assert health["alive"] is False
    assert health["seconds_since_tick"] > 500


def test_dead_task_is_not_alive():
    sched = _scheduler()
    sched.task = SimpleNamespace(done=lambda: True)
    sched.last_tick_at = datetime.now(timezone.utc)
    assert sched.health()["alive"] is False


def test_tick_failure_is_reported_but_loop_still_counts():
    """一轮出错不等于调度器死了——循环还活着这件事本身要报。"""
    sched = _scheduler(poll_seconds=0.01)
    calls = {"n": 0}

    async def boom(now=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError
        raise RuntimeError("取数超时")

    async def append_event(*args, **kwargs):
        return None

    sched.tick = boom
    sched.storage = SimpleNamespace(append_event=append_event)

    async def drive():
        with pytest.raises(asyncio.CancelledError):
            await sched._loop()

    asyncio.run(drive())
    assert sched.tick_count == 1            # 出错那轮也计数
    assert "取数超时" in sched.last_error
    assert sched.last_tick_at is not None
