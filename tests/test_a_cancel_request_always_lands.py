"""业主按了取消，这个任务就必须走到终态——不能永远停在"运行中"。

`reconcile_durable_jobs` 里"请求了取消"那一支原来是这么写的：

    if job.cancel_requested:
        if job.run_id:                       # ← 没有 run_id 就整段跳过
            try:
                self.runtime.cancel(job.run_id)
            except KeyError:
                if run is not None:          # ← 运行记录也没了就什么都不做
                    ...reconcile 成 cancelled...
        continue                             # ← 状态一点没变，下一轮原样再来

两个口子都通向同一个结局：任务一直是 running、cancel_requested 一直是 True，
每一轮对账都走到同一条死路。**业主按了取消，界面上却永远在转。**

对照旁边那一支（租约过期）就看得很清楚：它 cancel 失败也只是 pass，
但紧接着无条件 `recover_expired`——那一支任何情况下都会推进状态。
同一个操作、隔了十几行、一支管到底一支半途而废：
"同一个判据没铺满所有分支"，这周第 N 次。

真机现状：本机 durable_jobs 表 0 条，这条路没被走到过，属于埋着的坑。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_platform.durable_jobs import DurableJobStore
from agent_platform.scheduler import WorkflowScheduler
from agent_platform.storage import Storage


class RuntimeThatLostTheRun:
    """进程重启之后的运行时：它不认识重启前那些 run_id。"""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def cancel(self, run_id: str) -> None:
        self.asked.append(run_id)
        raise KeyError("active turn not found")


async def _store(tmp_path: Path) -> DurableJobStore:
    storage = Storage(tmp_path / "d")
    await storage.initialize()
    jobs = DurableJobStore(storage=storage)
    await jobs.initialize()
    return jobs


async def _running_job(jobs: DurableJobStore, *, key: str):
    job = await jobs.enqueue(
        job_id=f"job-{key}",
        idempotency_key=key,
        max_attempts=1,
        retry_backoff_seconds=0.0,
        application_id="app-1",
        version=1,
        node_id="n1",
        trigger_kind="schedule",
        local_date="2026-08-30",
        payload={"lease_seconds": 60},
    )
    claimed = await jobs.claim_next(worker_id="w1", lease_seconds=60,
                                    application_id="app-1")
    assert claimed and claimed.id == job.id
    return claimed


def _scheduler(jobs: DurableJobStore, runtime, *, run_lookup):
    from types import SimpleNamespace

    async def get_run(run_id: str) -> dict:
        found = run_lookup.get(run_id)
        if found is None:
            raise KeyError(run_id)
        return found

    async def finish_task(*_, **__):
        return None

    return WorkflowScheduler(
        storage=SimpleNamespace(), blocks=SimpleNamespace(),
        workflow_store=SimpleNamespace(get_run=get_run),
        runtime=runtime,
        harness=SimpleNamespace(worker_id="w1", finish_task=finish_task),
        durable_jobs=jobs, poll_seconds=3600,
    )


def _cancelled(job) -> bool:
    return job.status in {"cancelled", "failed"} or not job.cancel_requested


@pytest.mark.asyncio
async def test_a_cancel_lands_even_when_the_run_record_is_gone(tmp_path):
    """运行时不认识它、运行记录也查不到——最狠的那种，原来完全没人管。"""
    jobs = await _store(tmp_path)
    claimed = await _running_job(jobs, key="gone")
    attached = await jobs.attach_run(claimed.id, worker_id="w1",
                                     lease_version=claimed.lease_version,
                                     run_id="run-vanished")
    await jobs.request_cancel(attached.id, expected_revision=attached.revision)

    runtime = RuntimeThatLostTheRun()
    scheduler = _scheduler(jobs, runtime, run_lookup={})
    await scheduler.reconcile_durable_jobs()

    after = await jobs.get(claimed.id)
    assert runtime.asked == ["run-vanished"], "前提：确实试着去取消了"
    assert after.status != "running", f"还卡在 {after.status}"
    assert _cancelled(after), after


@pytest.mark.asyncio
async def test_a_cancel_lands_even_when_there_is_no_run_id(tmp_path):
    """还没来得及挂上 run_id 就被取消——原来连 try 都进不去。"""
    jobs = await _store(tmp_path)
    claimed = await _running_job(jobs, key="no-run")
    assert not claimed.run_id, "前提：这一条确实没有 run_id"
    await jobs.request_cancel(claimed.id, expected_revision=claimed.revision)

    scheduler = _scheduler(jobs, RuntimeThatLostTheRun(), run_lookup={})
    await scheduler.reconcile_durable_jobs()

    after = await jobs.get(claimed.id)
    assert after.status != "running", f"还卡在 {after.status}"
    assert _cancelled(after), after


@pytest.mark.asyncio
async def test_repeated_reconciles_do_not_leave_it_running(tmp_path):
    """连着对账几轮也走不出去，才是"永远在转"的真正含义。"""
    jobs = await _store(tmp_path)
    claimed = await _running_job(jobs, key="loop")
    await jobs.request_cancel(claimed.id, expected_revision=claimed.revision)

    scheduler = _scheduler(jobs, RuntimeThatLostTheRun(), run_lookup={})
    for _ in range(3):
        await scheduler.reconcile_durable_jobs()
    assert (await jobs.get(claimed.id)).status != "running"


@pytest.mark.asyncio
async def test_a_live_run_is_still_cancelled_through_the_runtime(tmp_path):
    """反向那一条：运行时认识它的时候，还是要正经喊停。

    少了这一条，"取消一定落地"可以靠"一律直接标成 cancelled、
    根本不去喊停运行时"实现——那会把真在跑的进程留在那儿空转。
    """
    jobs = await _store(tmp_path)
    claimed = await _running_job(jobs, key="live")
    attached = await jobs.attach_run(claimed.id, worker_id="w1",
                                     lease_version=claimed.lease_version,
                                     run_id="run-live")
    await jobs.request_cancel(attached.id, expected_revision=attached.revision)

    class LiveRuntime:
        def __init__(self) -> None:
            self.asked: list[str] = []

        def cancel(self, run_id: str) -> None:
            self.asked.append(run_id)

    runtime = LiveRuntime()
    scheduler = _scheduler(jobs, runtime,
                           run_lookup={"run-live": {"status": "running"}})
    await scheduler.reconcile_durable_jobs()
    assert runtime.asked == ["run-live"], "该喊停的时候得真喊"

    # 喊停之后**别急着改状态**：那个运行还在收尾，它走到终态之后
    # 下一轮对账会照着真实结果收（可能是 cancelled，也可能它抢先跑完了）。
    # 这里就把状态硬写成 cancelled 的话，账本会和实际发生的事对不上。
    # （少了这一条断言，"喊停之后顺手标 cancelled" 的实现全绿——变异验证抓到的。）
    assert (await jobs.get(claimed.id)).status == "running"

    # 下一轮：运行确实停了，这时才收尾
    scheduler = _scheduler(jobs, runtime,
                           run_lookup={"run-live": {"status": "cancelled"}})
    await scheduler.reconcile_durable_jobs()
    assert (await jobs.get(claimed.id)).status == "cancelled"


@pytest.mark.asyncio
async def test_a_job_nobody_cancelled_is_left_alone(tmp_path):
    """没人按取消的，别自作主张替它停了。"""
    jobs = await _store(tmp_path)
    claimed = await _running_job(tmp_path and jobs, key="untouched")
    scheduler = _scheduler(jobs, RuntimeThatLostTheRun(), run_lookup={})
    await scheduler.reconcile_durable_jobs()
    assert (await jobs.get(claimed.id)).status == "running"


@pytest.mark.asyncio
async def test_an_expired_lease_still_recovers(tmp_path):
    """旁边那一支不能被这次改动带坏——它本来就是always-推进的。"""
    from datetime import datetime, timedelta, timezone

    jobs = await _store(tmp_path)
    claimed = await _running_job(jobs, key="expired")
    later = datetime.now(timezone.utc) + timedelta(seconds=120)
    scheduler = _scheduler(jobs, RuntimeThatLostTheRun(), run_lookup={})
    await scheduler.reconcile_durable_jobs(now=later)
    assert (await jobs.get(claimed.id)).status != "running"


def test_module_imports_cleanly():
    """占位：确认上面那些 asyncio 测试真的被收集到了，不是整文件跳过。"""
    assert asyncio is not None
