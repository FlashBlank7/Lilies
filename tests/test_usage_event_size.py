"""用量事件不该把整条任务的用量历史抄一份。

回归背景（2026-08-28 真机）：events 表涨到 1 GB，其中
platform_harness.usage.recorded 26518 行占 859 MB（每行约 33 KB）。
拆开单条最大的 146 KB，task.usage 一项就 154 KB——
这条任务至今的全部用量明细，被原样嵌进了每一条用量事件。

平方级：调 N 次模型写 N 条事件，第 N 条装着前 N 条的内容。
后果不只是占地方——启动时要全表扫这张表，真机上一次要 90 分钟。
"""

import json
from pathlib import Path

import pytest

from agent_platform.config import Settings
from agent_platform.platform_harness import PlatformHarness
from agent_platform.storage import Storage


@pytest.fixture()
def harness(tmp_path: Path):
    settings = Settings(api_token="t", data_dir=tmp_path / "d",
                        workspace_root=tmp_path / "w")
    settings.prepare()
    storage = Storage(settings.data_dir)
    return storage, PlatformHarness(storage=storage)


@pytest.mark.asyncio
async def test_usage_events_do_not_grow_with_history(harness) -> None:
    storage, hub = harness
    await storage.initialize()
    await hub.start_task("t1", kind="builder_build", owner_id="own-1",
                         resource_id="r1", metadata={})

    sizes = []
    for index in range(30):
        await hub.record_usage("t1", "model_call", metadata={"n": index})
        events = await storage.list_events("platform_harness")
        usage_events = [e for e in events
                        if e.type == "platform_harness.usage.recorded"]
        sizes.append(len(json.dumps(usage_events[-1].data, ensure_ascii=False)))

    # 第 30 条不该比第 1 条大出一截——大了就说明历史又被抄进去了
    assert sizes[-1] < sizes[0] * 1.5, (
        f"用量事件随历史增长：第 1 条 {sizes[0]} 字节，第 30 条 {sizes[-1]} 字节")


@pytest.mark.asyncio
async def test_the_aggregate_and_this_usage_are_still_there(harness) -> None:
    """瘦身不能把有用的东西一起扔了。"""
    storage, hub = harness
    await storage.initialize()
    await hub.start_task("t1", kind="builder_build", owner_id="own-1",
                         resource_id="r1", metadata={})
    await hub.record_usage("t1", "model_call", metadata={"model": "x"})

    events = [e for e in await storage.list_events("platform_harness")
              if e.type == "platform_harness.usage.recorded"]
    data = events[-1].data
    assert data["usage_type"] == "model_call"          # 这一次的用量还在
    assert data["metadata"] == {"model": "x"}
    assert data["task"]["usage_counts"]["model_call"] == 1   # 聚合值还在
    assert "usage" not in data["task"]                       # 明细历史不再嵌
