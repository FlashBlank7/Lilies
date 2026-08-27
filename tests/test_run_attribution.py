"""运行归因：谁触发的。

多用户下所有运行长得一模一样，出了问题不知道该找谁——空串表示
定时/系统触发或迁移前的老数据。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent_platform.api import create_app
from agent_platform.config import Settings
from agent_platform.workflow_models import WorkflowRunState
from helpers_overview import _seed, services  # noqa: F401


@pytest.mark.asyncio
async def test_create_run_records_triggered_by(services):
    _seed(services)              # 外键要求先有 application
    store = services._store
    state = WorkflowRunState(run_id="r-1", application_id="app-1", snapshot={},
                             inputs={}, workspace_path="/tmp/ws")
    await store.create_run(state, version=1, draft_revision=None, triggered_by="zhaoyang")
    with services.workflow_store.storage._connect() as conn:
        row = conn.execute(
            "SELECT triggered_by FROM workflow_runs WHERE id='r-1'").fetchone()
    assert row["triggered_by"] == "zhaoyang"


@pytest.mark.asyncio
async def test_system_triggered_run_is_blank(services):
    """定时触发没有用户——留空，不要编一个名字出来。"""
    _seed(services)
    store = services._store
    state = WorkflowRunState(run_id="r-2", application_id="app-1", snapshot={},
                             inputs={}, workspace_path="/tmp/ws")
    await store.create_run(state, version=1, draft_revision=None)
    with services.workflow_store.storage._connect() as conn:
        row = conn.execute(
            "SELECT triggered_by FROM workflow_runs WHERE id='r-2'").fetchone()
    assert row["triggered_by"] == ""


def test_run_endpoints_expose_triggered_by(tmp_path) -> None:
    """老数据没有这个字段也要给出空串，客户端不用做存在性判断。"""
    settings = Settings(api_token="attr-test",
                        data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    from pathlib import Path

    source = Path("platform/backend/src/agent_platform/api.py").read_text(encoding="utf-8")
    # 单条 run 与列表两个端点都要补默认值，客户端才不用做存在性判断
    assert source.count('run.setdefault("triggered_by", "")') == 2
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/runs/nope",
                              headers={"Authorization": "Bearer attr-test"})
        assert response.status_code == 404      # 端点仍正常工作
