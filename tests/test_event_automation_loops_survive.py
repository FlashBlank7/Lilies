"""事件自动化的两个后台循环不能被一条坏数据打死。

2026-08-29 读代码发现，两处都是**整段裸奔**：

· `_timer_loop`：`_dispatch_timer` 里只包住了运行回调，
  它前面的 `json.loads(due_inputs_json)`（数据坏了就抛）和后面的记账
  都在保护之外；`_claim_due_timers_sync` 撞锁也一样。
  任何一处抛出，`create_task` 起的这个任务就地死亡——而且**不响**：
  异常要等任务被回收时才作为 "Task exception was never retrieved" 印出来。
  结果是所有事件定时器永久停摆，界面上什么也看不出来。

· `_subscription_loop`：`get_subscription`（订阅被删就 KeyError）和
  `model_validate`（配置坏了就 ValidationError）在 try **外面**。
  里面那圈保护挡不住外面这两句——和 scheduler.tick() 那次一模一样。

调度器为同一个形状写过 `_supervise`（"循环绝不能就这么没了"）。
同一个教训在这个文件里没落地过。

顺带：这套测试还必须真的**走进** except 分支。第一版兜底里调 logger，
而这个模块压根没 import logging——py_compile 全绿，真出事时
兜底自己抛 NameError，比不兜还糟。所以每条用例都要触发一次异常。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from agent_platform.event_automation import EventAutomationService


def _service(tmp: str) -> EventAutomationService:
    return EventAutomationService(
        Path(tmp) / "events.db",
        harness=SimpleNamespace(worker_id="w1"),
        timer_poll_seconds=0.01,
    )


async def _run_briefly(coro, seconds: float = 0.08) -> None:
    task = asyncio.create_task(coro)
    await asyncio.sleep(seconds)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a_broken_timer_row_does_not_kill_the_loop():
    """一条 due_inputs_json 坏掉的定时器，不能带走整个循环。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        await service.initialize()
        rounds = {"n": 0}

        def claim():
            rounds["n"] += 1
            return [{"timer_key": "坏的", "due_inputs_json": "{不是JSON",
                     "source_event_id": "e1", "due_at": "x",
                     "recovery_count": 0, "application_id": "a",
                     "workspace_path": "/w"}]

        async def never_called(*a, **k):
            raise AssertionError("坏行不该走到运行回调")

        service._claim_due_timers_sync = claim
        service.bind_run_callback(never_called)
        await _run_briefly(service._timer_loop())
        assert rounds["n"] > 1, "第一条坏数据就把循环打死了"


@pytest.mark.asyncio
async def test_a_failing_claim_does_not_kill_the_loop():
    """取数本身炸了（比如数据库锁住）也要接着来。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        await service.initialize()
        rounds = {"n": 0}

        def claim():
            rounds["n"] += 1
            raise RuntimeError("database is locked")

        service._claim_due_timers_sync = claim
        service.bind_run_callback(lambda *a: None)
        await _run_briefly(service._timer_loop())
        assert rounds["n"] > 1


@pytest.mark.asyncio
async def test_a_good_timer_still_fires_after_a_bad_one():
    """坏的跳过，好的照跑——不然"不崩"只是换了种方式失效。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        await service.initialize()
        dispatched: list[str] = []

        def claim():
            return [
                {"timer_key": "坏的", "due_inputs_json": "{不是JSON",
                 "source_event_id": "e1", "due_at": "x", "recovery_count": 0,
                 "application_id": "a", "workspace_path": "/w"},
                {"timer_key": "好的", "due_inputs_json": json.dumps({"n": 1}),
                 "source_event_id": "e2", "due_at": "x", "recovery_count": 0,
                 "application_id": "a", "workspace_path": "/w"},
            ]

        async def run(app_id, inputs, workspace):
            dispatched.append(app_id)
            return {"run_id": "r-1"}

        service._claim_due_timers_sync = claim
        service._record_timer_dispatched_sync = lambda *a: None
        service.bind_run_callback(run)
        await _run_briefly(service._timer_loop())
        assert dispatched, "坏的那条把好的一起拖下水了"


@pytest.mark.asyncio
async def test_a_missing_subscription_does_not_kill_its_loop():
    """订阅被删了（get_subscription 抛 KeyError）不能让任务静默死掉。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        await service.initialize()
        tries = {"n": 0}

        async def gone(subscription_id):
            tries["n"] += 1
            raise KeyError("这个订阅没了")

        service.get_subscription = gone
        await _run_briefly(service._subscription_loop("s1"))
        assert tries["n"] > 1


@pytest.mark.asyncio
async def test_a_broken_subscription_config_does_not_kill_its_loop():
    """配置校验不过也一样——那两句原先就在 try 外面。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        await service.initialize()
        tries = {"n": 0}

        async def broken(subscription_id):
            tries["n"] += 1
            return {"enabled": True, "config": {"name": "!!非法!!"}}

        service.get_subscription = broken
        await _run_briefly(service._subscription_loop("s1"))
        assert tries["n"] > 1


@pytest.mark.asyncio
async def test_a_disabled_subscription_still_stops_cleanly():
    """关掉的订阅要正常退出，别被新加的兜底变成死循环。"""
    with TemporaryDirectory() as tmp:
        service = _service(tmp)
        await service.initialize()

        async def disabled(subscription_id):
            return {"enabled": False, "config": {}}

        service.get_subscription = disabled
        await asyncio.wait_for(service._subscription_loop("s1"), timeout=2)
