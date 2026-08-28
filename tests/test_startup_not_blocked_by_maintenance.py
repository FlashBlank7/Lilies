"""事件维护再慢，也不能拦着服务起来。

回归背景（2026-08-28 真机）：重启后端口迟迟不监听。py-spy 打栈看到卡在
storage._archive_events_before_sync 的 DELETE；wchan 是 wait_on_page_bit_common、
syscall 是 pread64——不是死锁，是在 1 GB 的 events 表上全表扫描被磁盘拖住，
按读进度推算还要 83 分钟。而那次要删的行数是 **0**：
319240 条事件里没有一条超过 7 天，扫一个小时删了个寂寞。

于是「重启一下」= 停机一个多小时，且随数据增长只会更久。
维护慢可以忍，服务起不来不能忍。
"""

import asyncio
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.storage import Storage


def _app(tmp_path: Path):
    return create_app(Settings(api_token="startup-test",
                               data_dir=tmp_path / "data",
                               workspace_root=tmp_path / "ws",
                               scheduler_poll_seconds=3600))


def test_slow_event_maintenance_does_not_delay_readiness(tmp_path: Path,
                                                         monkeypatch) -> None:
    async def glacial_archive(self, *, keep_days: int):
        # 必须 await 着睡，不能用 threading 的阻塞等待：后台维护跑在事件循环上，
        # 阻塞式等待会把整个循环连同 /health 一起冻住，
        # 那样测的就不是「有没有挂在启动路径上」了。
        # 这版测试自己踩过这个坑：单跑碰巧绿，整套一起跑就红。
        await asyncio.sleep(600)
        return {"removed": 0, "remaining": 0}

    monkeypatch.setattr(Storage, "archive_events_before", glacial_archive)

    # 维护永远跑不完，服务却必须照常应答：
    # TestClient 的 with 进得去（启动完成）就说明维护没挂在启动路径上。
    # 放线程里跑并设一个截止时间：真挂回启动路径时要「红」，不要「挂住」——
    # 卡死的测试在 CI 上比失败的测试更难查。
    done, failure = threading.Event(), []

    def boot() -> None:
        try:
            with TestClient(_app(tmp_path)) as client:
                assert client.get("/health").status_code == 200
                assert client.get("/health").status_code == 200
        except BaseException as error:  # noqa: BLE001 - 交回主线程报告
            failure.append(error)
        finally:
            done.set()

    thread = threading.Thread(target=boot, daemon=True)
    thread.start()
    # 120 秒是留给建库建表的（这台机器磁盘慢），远小于桩里睡的 600 秒
    assert done.wait(timeout=120), "维护没跑完，服务就起不来——又挂回启动路径上了"
    assert not failure, failure


def test_maintenance_failure_does_not_take_the_service_down(tmp_path: Path,
                                                            monkeypatch) -> None:
    async def exploding_archive(self, *, keep_days: int):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(Storage, "archive_events_before", exploding_archive)

    with TestClient(_app(tmp_path)) as client:
        assert client.get("/health").status_code == 200
        time.sleep(0.2)   # 让后台任务有机会抛出来
        assert client.get("/health").status_code == 200
