"""事件维护再慢，也不能拦着服务起来。

回归背景（2026-08-28 真机）：重启后端口迟迟不监听。py-spy 打栈看到卡在
storage._archive_events_before_sync 的 DELETE；wchan 是 wait_on_page_bit_common、
syscall 是 pread64——不是死锁，是在 1 GB 的 events 表上全表扫描被磁盘拖住，
按读进度推算还要 83 分钟。而那次要删的行数是 **0**：
319240 条事件里没有一条超过 7 天，扫一个小时删了个寂寞。

于是「重启一下」= 停机一个多小时，且随数据增长只会更久。
维护慢可以忍，服务起不来不能忍。
"""

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.storage import Storage


def test_slow_event_maintenance_does_not_delay_readiness(tmp_path: Path,
                                                         monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()

    async def glacial_archive(self, *, keep_days: int):
        started.set()
        # 模拟真机：慢到近乎无限。必须**远长于**下面那个就绪等待，
        # 否则挂回启动路径时它只是「慢一点」，测试照样会绿——就没有区分力了。
        release.wait(timeout=180)
        return {"removed": 0, "remaining": 0}

    monkeypatch.setattr(Storage, "archive_events_before", glacial_archive)

    app = create_app(Settings(api_token="startup-test",
                              data_dir=tmp_path / "data",
                              workspace_root=tmp_path / "ws",
                              scheduler_poll_seconds=3600))

    ready = threading.Event()
    failure: list[BaseException] = []

    def boot() -> None:
        try:
            with TestClient(app) as client:
                assert client.get("/health").status_code == 200
                ready.set()
        except BaseException as error:  # noqa: BLE001 - 交回主线程报告
            failure.append(error)
            ready.set()

    thread = threading.Thread(target=boot, daemon=True)
    thread.start()
    # 维护还卡着的时候，服务就该已经能应答了。
    # 45 秒不是给维护的，是给建库+建表本身的：这台机器磁盘慢，
    # 整套测试并跑时冷启动一次要十几秒，20 秒会偶发假红。
    assert ready.wait(timeout=45), "维护没跑完，服务就起不来——又挂回启动路径上了"
    release.set()
    thread.join(timeout=20)
    assert not failure, failure


def test_maintenance_failure_does_not_take_the_service_down(tmp_path: Path,
                                                            monkeypatch) -> None:
    async def exploding_archive(self, *, keep_days: int):
        raise RuntimeError("磁盘炸了")

    monkeypatch.setattr(Storage, "archive_events_before", exploding_archive)

    app = create_app(Settings(api_token="startup-test",
                              data_dir=tmp_path / "data",
                              workspace_root=tmp_path / "ws",
                              scheduler_poll_seconds=3600))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        time.sleep(0.2)   # 让后台任务有机会抛出来
        assert client.get("/health").status_code == 200
