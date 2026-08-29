"""「没说是哪个工作流」和「说了但没有这个」是两件事。

原来六处工具站点都写成同一句：

    app = await self._resolve_app(str(args.get("name_or_id") or ""))
    if not app:
        return {"error": "找不到该工作流"}

于是模型漏传参数时得到的是"找不到该工作流"——它会照着这句去告诉业主
**"你说的那个工作流不存在"**，而业主什么都没说错，他甚至没提过名字。

平台在别处早就把这两件事分开说了（「链接少了业主码」vs「业主码不对」，
理由写在 _require_use_access 上面）。这里跟上，并且六处共用一个 helper——
分散写六遍的话，下次只会修一处。

顺带把名字报出来：只说"没有这个工作流"，模型没法把话转述准确。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_platform.assistant_agent import WorkflowConcierge
from agent_platform.config import Settings
from agent_platform.storage import Storage
from agent_platform.workflow_storage import WorkflowStorage

# 这些工具都要求业主指名道姓
# 工具名从源码里核过，不是猜的——第一版写了两个不存在的名字
# （acceptance_report / restore_workflow），得到的是"没有这个工具"，
# 断言红了才发现测的是我以为的名字。
NAMED_TOOLS = ["explain_workflow", "recent_runs", "set_schedule",
               "acceptance_check", "repair_workflow"]


@pytest.fixture
def concierge(tmp_path: Path) -> WorkflowConcierge:
    storage = Storage(tmp_path / "d")
    store = WorkflowStorage(storage)

    async def _init() -> None:
        await storage.initialize()
        await store.initialize()

    asyncio.run(_init())
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,"
            "active_version,created_at,updated_at) "
            "VALUES('a1','词频统计','','','workflow',1,datetime('now'),datetime('now'))")
        conn.execute(
            "INSERT INTO application_drafts(application_id,revision,snapshot_json,"
            "content_hash,validation_report_json,updated_at) "
            "VALUES('a1',1,'{}',?,'{}',datetime('now'))", ("h" * 64,))
    services = SimpleNamespace(
        storage=storage, workflow_store=store, _store=store,
        settings=Settings(api_token="t", data_dir=tmp_path / "d",
                          workspace_root=tmp_path / "w"),
        scheduler=SimpleNamespace(health=lambda: {"alive": True}),
    )
    return WorkflowConcierge(services, settings=None)


def _run(concierge, tool, args):
    return asyncio.run(concierge._exec(tool, args, user={}))


@pytest.mark.parametrize("tool", NAMED_TOOLS)
def test_no_name_at_all_says_so(concierge, tool):
    """一个字都没说的时候，别报成"这个工作流不存在"。"""
    error = _run(concierge, tool, {})["error"]
    assert "没说是哪个" in error, error
    assert "list_workflows" in error, "要给下一步，不然模型只能干瞪眼"


@pytest.mark.parametrize("tool", NAMED_TOOLS)
def test_a_wrong_name_is_quoted_back(concierge, tool):
    """说错了名字的时候，把他说的那个名字念回去。"""
    error = _run(concierge, tool, {"name_or_id": "并不存在的东西"})["error"]
    assert "并不存在的东西" in error, error
    assert "没说是哪个" not in error, "他明明说了，别倒打一耙"


def test_a_real_name_still_works(concierge):
    """反向那一条：名字对的时候不许报错，否则上面全绿也没意义。"""
    result = _run(concierge, "recent_runs", {"name_or_id": "词频统计"})
    assert "error" not in result, result


def test_the_archived_lookup_keeps_the_same_two_messages(concierge):
    """「收拾草稿」那一处走的是另一条分支（要连归档的一起找），别漏。"""
    args = {"action": "restore"}
    assert "没说是哪个" in _run(concierge, "tidy_workflows", args)["error"]
    assert "查无此名" in _run(
        concierge, "tidy_workflows", {**args, "name_or_id": "查无此名"})["error"]
