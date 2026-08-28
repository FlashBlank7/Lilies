"""调度器的两条冷路径：循环别死、重试别成风暴。

实测（2026-08-28 平台审计）：
- _loop 的 except 里裸调 append_event，它自己撞锁时异常从 except 逃出去，
  整个调度线程永久死亡，定时任务全体静默停摆；
- 失败即无条件重认领（DELETE+INSERT），一个必然失败的定时任务能把当天
  刷成上千次运行，而重写 created_at 还让逾期检测恒为 False，风暴期间体检看着正常。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_platform.scheduler import WorkflowScheduler
from agent_platform.workflow_storage import WorkflowStorage
from helpers_overview import _seed, services  # noqa: F401


def _sched(**kwargs) -> WorkflowScheduler:
    stub = SimpleNamespace()
    return WorkflowScheduler(
        storage=stub, workflow_store=stub, blocks=stub, runtime=stub,
        harness=SimpleNamespace(worker_id="w1"), durable_jobs=stub,
        poll_seconds=kwargs.pop("poll_seconds", 0.01), **kwargs)


def test_loop_survives_when_recording_the_error_also_fails():
    """记事失败不能反杀循环——这是"兜底路径本身是坏的"的典型。"""
    sched = _sched()
    ticks = {"n": 0}

    async def tick(now=None):
        ticks["n"] += 1
        if ticks["n"] >= 4:
            raise asyncio.CancelledError
        raise RuntimeError("取数超时")

    async def append_event(*args, **kwargs):
        raise RuntimeError("database is locked")   # 兜底自己也炸

    sched.tick = tick
    sched.storage = SimpleNamespace(append_event=append_event)

    async def drive():
        with pytest.raises(asyncio.CancelledError):
            await sched._loop()

    asyncio.run(drive())
    assert ticks["n"] == 4, "循环被记事失败打死了"
    assert sched.tick_count == 3
    assert "取数超时" in sched.last_error


def test_supervise_restarts_a_dead_loop():
    """循环真的挂了要自己爬起来，并把重启次数报出去。"""
    sched = _sched(poll_seconds=0.01)
    rounds = {"n": 0}

    async def loop():
        rounds["n"] += 1
        if rounds["n"] >= 3:
            raise asyncio.CancelledError
        raise RuntimeError("循环内部崩了")

    sched._loop = loop

    async def drive():
        with pytest.raises(asyncio.CancelledError):
            await sched._supervise()

    asyncio.run(drive())
    assert rounds["n"] == 3
    assert sched.restart_count == 2
    assert "循环异常重启" in sched.last_error
    assert sched.health()["restart_count"] == 2


@pytest.mark.asyncio
async def test_schedule_retry_has_a_ceiling(services):
    """失败重认领要有上限，否则一天能放大成上千次运行。"""
    _seed(services)
    store: WorkflowStorage = services._store
    key = ("app-1", 1, "sched", "2026-08-28")
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO schedule_fires(application_id,version,node_id,local_date,"
            "run_id,created_at,attempts,last_attempt_at) "
            "VALUES(?,?,?,?,'r-1',?,?,NULL)",
            (*key, "2026-08-28T00:00:00+00:00", store.SCHEDULE_MAX_ATTEMPTS))
        assert store._reclaim_schedule_fire_sync(conn, *key) is False


@pytest.mark.asyncio
async def test_schedule_retry_backs_off(services):
    """刚试过就再来一次也要拦——退避窗口内不重认领。"""
    from datetime import datetime, timezone

    _seed(services)
    store: WorkflowStorage = services._store
    key = ("app-1", 1, "sched", "2026-08-28")
    now = datetime.now(timezone.utc).isoformat()
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO schedule_fires(application_id,version,node_id,local_date,"
            "run_id,created_at,attempts,last_attempt_at) VALUES(?,?,?,?,'r-1',?,1,?)",
            (*key, "2026-08-28T00:00:00+00:00", now))
        assert store._reclaim_schedule_fire_sync(conn, *key) is False


@pytest.mark.asyncio
async def test_reclaim_keeps_created_at(services):
    """重认领不能重写 created_at——那会让逾期检测恒为 False，
    风暴期间体检看起来完全正常。"""
    _seed(services)
    store: WorkflowStorage = services._store
    key = ("app-1", 1, "sched", "2026-08-28")
    original = "2026-08-28T00:00:00+00:00"
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO schedule_fires(application_id,version,node_id,local_date,"
            "run_id,created_at,attempts,last_attempt_at) VALUES(?,?,?,?,'r-1',?,1,NULL)",
            (*key, original))
        assert store._reclaim_schedule_fire_sync(conn, *key) is True
        row = conn.execute(
            "SELECT created_at, attempts, run_id FROM schedule_fires "
            "WHERE application_id=? AND version=? AND node_id=? AND local_date=?",
            key).fetchone()
    assert row["created_at"] == original, "created_at 被重写了"
    assert row["attempts"] == 2
    assert row["run_id"] is None      # 重新可认领
