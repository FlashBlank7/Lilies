"""统计口径：只算发布版真实运行，不算草稿自测。

用**真 SQLite**而不是桩——桩数据正是把"整表 run 都是真实运行"这个错误假设
固化下来的地方（真机 314 条运行里 255 条是搭建期自测，全部混进了统计：
体检因自测失败把工作流判 broken，而 repair_workflow 会拿这个判定自动开
修复构建，这是唯一会自动花钱的路径）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_platform.config import Settings
from agent_platform.overview import build_health, build_overview
from agent_platform.storage import Storage
from agent_platform.workflow_storage import WorkflowStorage


SCHEDULED_SNAPSHOT = json.dumps({
    "name": "定时的", "workflow": {"nodes": [
        {"id": "s", "type": "schedule_trigger",
         "config": {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai"}}]}})
PLAIN_SNAPSHOT = json.dumps({"name": "普通的", "workflow": {"nodes": [{"id": "s", "type": "start"}]}})


@pytest.fixture
def services(tmp_path):
    """真 SQLite：建表走平台自己的初始化路径，schema 与线上一致。"""
    settings = Settings(api_token="scope-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    settings.prepare()
    storage = Storage(settings.data_dir)
    store = WorkflowStorage(storage)
    asyncio.run(_init(storage, store))
    return SimpleNamespace(workflow_store=SimpleNamespace(storage=storage), _store=store)


async def _init(storage, store):
    await storage.initialize()
    await store.initialize()


def _seed(services, *, app_id="app-1", published_version=1,
          real_runs=(), draft_runs=(), version_snapshot=PLAIN_SNAPSHOT,
          draft_snapshot=SCHEDULED_SNAPSHOT):
    """插真行：real_runs/draft_runs 是 status 列表。"""
    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,active_version,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (app_id, "被测工作流", "", "", "workflow", published_version))
        conn.execute(
            "INSERT INTO application_drafts(application_id,revision,snapshot_json,content_hash,"
            "validation_report_json,updated_at) VALUES(?,?,?,?,'{}',datetime('now'))",
            (app_id, 1, draft_snapshot, "h" * 64))
        if published_version is not None:
            conn.execute(
                "INSERT INTO application_versions(application_id,version,snapshot_json,"
                "content_hash,validation_report_json,created_at) "
                "VALUES(?,?,?,?,'{}',datetime('now'))",
                (app_id, published_version, version_snapshot, "h" * 64))
        index = 0
        for status in real_runs:
            index += 1
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
                "state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,?,?,NULL,?,'{}','{}',?,datetime('now'),datetime('now'))",
                (f"real-{index}", app_id, published_version, status,
                 "真实运行的错误" if status == "failed" else None))
        for status in draft_runs:
            index += 1
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
                "state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,?,NULL,?,?,'{}','{}',?,datetime('now'),datetime('now'))",
                (f"draft-{index}", app_id, 3, status,
                 "自测的错误" if status == "failed" else None))


@pytest.mark.asyncio
async def test_draft_selftest_failures_do_not_break_health(services):
    """搭建期自测失败一片，但发布版跑得好好的——不能判 broken。"""
    _seed(services, real_runs=["succeeded"] * 3, draft_runs=["failed"] * 20)
    report = await build_health(services)
    item = report["items"][0]
    assert item["state"] == "ok", item
    assert item["runs"] == 3          # 只数真实运行
    assert item["succeeded"] == 3
    assert report["counts"]["broken"] == 0


@pytest.mark.asyncio
async def test_real_failures_still_break_health(services):
    """真实运行确实全败——照样要报出来，别把过滤当成免死金牌。"""
    _seed(services, real_runs=["failed"] * 4, draft_runs=["succeeded"] * 10)
    report = await build_health(services)
    item = report["items"][0]
    assert item["state"] == "broken"
    assert "真实运行的错误" in item["reason"]   # 原因也来自真实运行


@pytest.mark.asyncio
async def test_fail_streak_window_not_flooded_by_selftests(services):
    """连败窗口先过滤再截断：自测噪音不能把真实运行挤出窗口。"""
    _seed(services, real_runs=["succeeded"], draft_runs=["failed"] * 50)
    report = await build_health(services)
    assert report["items"][0]["fail_streak"] == 0


@pytest.mark.asyncio
async def test_overview_counts_only_real_runs(services):
    """今日统计与七日趋势同一口径。"""
    _seed(services, real_runs=["succeeded", "failed"], draft_runs=["failed"] * 9)
    data = await build_overview(services)
    assert data["runs_today"]["total"] == 2
    assert data["runs_today"]["failed"] == 1
    assert sum(day["ok"] + day["fail"] for day in data["week"]) == 2
    assert data["recent_failures"][0]["error"] == "真实运行的错误"


@pytest.mark.asyncio
async def test_schedules_come_from_published_version(services):
    """定时由发布版快照决定——草稿里加了没发布的定时不该显示。"""
    _seed(services, real_runs=["succeeded"],
          version_snapshot=PLAIN_SNAPSHOT,        # 发布版没有定时节点
          draft_snapshot=SCHEDULED_SNAPSHOT)      # 草稿里有（还没发布）
    data = await build_overview(services)
    assert data["schedules"] == []
    report = await build_health(services)
    assert report["items"][0]["scheduled"] is False


@pytest.mark.asyncio
async def test_published_schedule_is_reported(services):
    _seed(services, real_runs=["succeeded"],
          version_snapshot=SCHEDULED_SNAPSHOT, draft_snapshot=PLAIN_SNAPSHOT)
    data = await build_overview(services)
    assert [s["at"] for s in data["schedules"]] == ["08:00"]
