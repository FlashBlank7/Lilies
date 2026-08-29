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


def _stub_scheduler_for_tick(apps, versions, *, fired):
    """给 tick() 搭一副最小骨架：只保留"逐个应用看有没有定时"这条主干。"""
    from types import SimpleNamespace

    async def list_applications():
        return apps

    async def get_version(app_id, version=None):
        got = versions[app_id]
        if isinstance(got, Exception):
            raise got
        return got

    async def claim_schedule_fire(*a, **k):
        return True

    sched = _sched()
    sched.workflow_store = SimpleNamespace(
        list_applications=list_applications, get_version=get_version,
        claim_schedule_fire=claim_schedule_fire)

    async def noop(*a, **k):
        return []

    sched.reconcile_durable_jobs = noop
    sched.run_due_durable_jobs = noop

    async def execute(app_id, **kwargs):
        if isinstance(fired.get("boom"), Exception) and app_id == fired.get("boom_app"):
            raise fired["boom"]
        fired.setdefault("apps", []).append(app_id)
        return {"application_id": app_id}

    sched.execute_claimed_schedule_fire = execute
    return sched


def _version_with_schedule(hour=0, minute=0, timezone_name="UTC"):
    from types import SimpleNamespace

    node = SimpleNamespace(id="s1", type="schedule_trigger",
                           config={"hour": hour, "minute": minute,
                                   "timezone": timezone_name})
    return {"snapshot": SimpleNamespace(
        workflow=SimpleNamespace(nodes=[node]))}


@pytest.mark.asyncio
async def test_a_broken_workflow_does_not_block_the_others():
    """一个工作流的定时配置坏了，别的照样要开火。

    实测缺陷（2026-08-29 读代码发现）：tick() 里
    `ScheduleTriggerConfig.model_validate(node.config)` 和
    `get_version(...)` 都在**没有任何保护**的循环体里。
    任何一个应用抛异常，异常直接掀翻整个 tick——
    排在它后面的工作流这一轮全部不开火。
    而 list_applications 的顺序是稳定的，所以下一轮它还是先炸，
    后面那些**永远**不开火。

    更要命的是这事没有响：_loop 捕获后照样更新 last_tick_at、
    照样 tick_count += 1，health() 于是报「调度器活着」。
    """
    apps = [{"id": "a", "name": "坏的", "active_version": 1},
            {"id": "b", "name": "好的", "active_version": 1}]
    versions = {"a": KeyError("这个版本查不到了"),
                "b": _version_with_schedule()}
    fired: dict = {}
    sched = _stub_scheduler_for_tick(apps, versions, fired=fired)
    await sched.tick()
    assert fired.get("apps") == ["b"], "坏的那个把好的一起拖下水了"


@pytest.mark.asyncio
async def test_a_bad_timezone_does_not_block_the_others():
    """时区名不认识 → 配置校验抛错。同样不能连累别人。

    体检那条路早就防住了（「时区名不认识就按 UTC 算，别整个不判」），
    调度器这条路没有——闸只装在一个出口上。
    """
    apps = [{"id": "a", "name": "坏时区", "active_version": 1},
            {"id": "b", "name": "好的", "active_version": 1}]
    versions = {"a": _version_with_schedule(timezone_name="Mars/Olympus"),
                "b": _version_with_schedule()}
    fired: dict = {}
    sched = _stub_scheduler_for_tick(apps, versions, fired=fired)
    await sched.tick()
    assert fired.get("apps") == ["b"]


@pytest.mark.asyncio
async def test_a_failing_run_does_not_block_the_others():
    """开火本身失败（建运行时炸了）也不能连累后面的。

    execute_claimed_schedule_fire 在异常时会释放认领然后**再抛出去**，
    于是那个异常照样掀翻整个 tick。
    """
    apps = [{"id": "a", "name": "跑不起来", "active_version": 1},
            {"id": "b", "name": "好的", "active_version": 1}]
    versions = {"a": _version_with_schedule(), "b": _version_with_schedule()}
    fired: dict = {"boom": RuntimeError("建运行失败"), "boom_app": "a"}
    sched = _stub_scheduler_for_tick(apps, versions, fired=fired)
    await sched.tick()
    assert fired.get("apps") == ["b"]


@pytest.mark.asyncio
async def test_the_trouble_is_reported_not_swallowed():
    """跳过要留痕：静默跳过等于这个定时任务无声脱离监控。"""
    apps = [{"id": "a", "name": "坏的", "active_version": 1}]
    versions = {"a": KeyError("这个版本查不到了")}
    sched = _stub_scheduler_for_tick(apps, versions, fired={})
    await sched.tick()
    assert "坏的" in sched.last_error or "a" in sched.last_error, sched.last_error


@pytest.mark.asyncio
async def test_a_healthy_tick_leaves_no_stale_complaint():
    """上一轮的抱怨不能赖在 last_error 上——那会让人去查一个已经没有的问题。"""
    apps = [{"id": "b", "name": "好的", "active_version": 1}]
    versions = {"b": _version_with_schedule()}
    sched = _stub_scheduler_for_tick(apps, versions, fired={})
    sched.last_error = "上一轮的旧抱怨"
    await sched.tick()
    assert sched.last_error == ""


@pytest.mark.asyncio
async def test_a_broken_reconcile_does_not_stop_every_schedule():
    """对账排在逐个应用之前——它一炸，一个工作流都不会开火。

    2026-08-29 第一版只给"逐个应用"那段加了保护，而这一段在循环**外面**：
    保护装在里面，外面照样能把整轮带走。数出口的时候，
    别忘了循环外面那两句也是出口。
    """
    apps = [{"id": "b", "name": "好的", "active_version": 1}]
    versions = {"b": _version_with_schedule()}
    fired: dict = {}
    sched = _stub_scheduler_for_tick(apps, versions, fired=fired)

    async def boom(*a, **k):
        raise RuntimeError("对账时数据库锁住了")

    sched.reconcile_durable_jobs = boom
    await sched.tick()
    assert fired.get("apps") == ["b"], "对账炸了就没人开火了"
    assert "对账" in sched.last_error


@pytest.mark.asyncio
async def test_a_broken_durable_sweep_does_not_hide_what_already_fired():
    """收尾那段炸了，前面已经开的火要照样报上来。"""
    apps = [{"id": "b", "name": "好的", "active_version": 1}]
    versions = {"b": _version_with_schedule()}
    fired: dict = {}
    sched = _stub_scheduler_for_tick(apps, versions, fired=fired)

    async def boom(*a, **k):
        raise RuntimeError("取后台任务失败")

    sched.run_due_durable_jobs = boom
    started = await sched.tick()
    assert fired.get("apps") == ["b"]
    assert len(started) == 1, started
    assert "后台任务" in sched.last_error
