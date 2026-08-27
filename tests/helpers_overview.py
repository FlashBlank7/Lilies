"""统计/体检类测试的共用真库夹具。

为什么不用桩：桩每次 SQL 变化就碎，而且它固化的假设本身可能是错的——
"整表 run 都是真实运行"就是这么固化进测试的，功能错了测试还全绿
（真机 314 条运行里 255 条是草稿自测）。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent_platform.config import Settings
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
          draft_snapshot=SCHEDULED_SNAPSHOT, fail_error="真实运行的错误"):
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
                # 每条给不同时刻：连败判定按 created_at DESC 取"最近"，
                # 时间戳全同的话 SQLite 的顺序不定，测试会随机飘
                "VALUES(?,?,?,NULL,?,'{}','{}',?,"
                "datetime('now', ?),datetime('now', ?))",
                (f"real-{index}", app_id, published_version, status,
                 fail_error if status == "failed" else None,
                 f"-{len(real_runs) - index} seconds",
                 f"-{len(real_runs) - index} seconds"))
        for status in draft_runs:
            index += 1
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
                "state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,?,NULL,?,?,'{}','{}',?,"
                "datetime('now', ?),datetime('now', ?))",
                (f"draft-{index}", app_id, 3, status,
                 "自测的错误" if status == "failed" else None,
                 f"-{index} seconds", f"-{index} seconds"))
