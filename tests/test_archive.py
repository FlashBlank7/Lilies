"""归档：废弃草稿要有清理路径，但数据不删、也不替用户做决定。

真机现状（2026-08-28）：71 个应用里 57 个从没发布过，其中 46 个连一次成功
运行都没有——用户用几个月就会被废弃草稿淹没，而平台完全没有归档概念。
"""

from __future__ import annotations

import pytest

from helpers_overview import _seed, services  # noqa: F401


def _add_app(services, app_id: str, name: str, *, published=None, updated="2020-01-01"):
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,active_version,"
            "created_at,updated_at) VALUES(?,?,'','','workflow',?,?,?)",
            (app_id, name, published, updated, updated))
        conn.execute(
            "INSERT INTO application_drafts(application_id,revision,snapshot_json,"
            "content_hash,validation_report_json,updated_at) VALUES(?,1,'{}',?,'{}',?)",
            (app_id, "h" * 64, updated))


@pytest.mark.asyncio
async def test_archived_disappears_from_list_but_data_stays(services):
    store = services._store
    _add_app(services, "a1", "要收起来的")
    before = [a["id"] for a in await store.list_applications()]
    assert "a1" in before

    await store.set_archived("a1", True)
    assert "a1" not in [a["id"] for a in await store.list_applications()]
    with services.workflow_store.storage._connect() as conn:
        row = conn.execute("SELECT name, archived_at FROM applications "
                           "WHERE id='a1'").fetchone()
    assert row["name"] == "要收起来的"      # 数据一条没删
    assert row["archived_at"]

    await store.set_archived("a1", False)
    assert "a1" in [a["id"] for a in await store.list_applications()]


@pytest.mark.asyncio
async def test_archive_unknown_app_raises(services):
    with pytest.raises(KeyError):
        await services._store.set_archived("不存在", True)


@pytest.mark.asyncio
async def test_archivable_only_suggests_truly_abandoned(services):
    """标准是「从没发布 + 从没成功跑过 + 放了一阵」——三个都要满足。"""
    store = services._store
    _add_app(services, "abandoned", "没人管的草稿")
    _add_app(services, "published", "已发布的", published=1)
    _add_app(services, "fresh", "刚建的", updated="2099-01-01")
    _add_app(services, "ran-ok", "跑成功过的")
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES('r1','ran-ok',NULL,1,'succeeded','{}','{}',NULL,"
            "'2020-01-01','2020-01-01')")

    names = {i["name"] for i in await store.list_archivable(days_idle=7)}
    assert names == {"没人管的草稿"}, names


@pytest.mark.asyncio
async def test_archivable_respects_idle_window(services):
    store = services._store
    _add_app(services, "recent", "昨天刚碰过", updated="2099-01-01")
    assert await store.list_archivable(days_idle=7) == []
    # 窗口设为 0 天：连"今天动过的"也算，用来验证参数真的生效
    assert {i["name"] for i in await store.list_archivable(days_idle=0)} == set()


@pytest.mark.asyncio
async def test_archived_list_is_visible(services):
    """收起来的必须看得见——可逆操作没有回头路，用户就不敢按第一下。"""
    store = services._store
    _add_app(services, "a1", "收起来的")
    _add_app(services, "a2", "还在列表里的")
    await store.set_archived("a1", True)

    archived = await store.list_archived()
    assert [i["name"] for i in archived] == ["收起来的"]
    assert archived[0]["archived_at"]
    assert "还在列表里的" not in [i["name"] for i in archived]


@pytest.mark.asyncio
async def test_resolve_finds_archived_when_asked(services):
    """「拿回 X」要能按名字找到已归档的——它们不在常规列表里。"""
    from types import SimpleNamespace

    from agent_platform.assistant_agent import WorkflowConcierge

    store = services._store
    _add_app(services, "a1", "收起来的日报")
    await store.set_archived("a1", True)

    concierge = WorkflowConcierge(
        SimpleNamespace(workflow_store=store), SimpleNamespace())
    assert await concierge._resolve_app("收起来的日报") is None          # 默认看不到
    found = await concierge._resolve_app("收起来的日报", include_archived=True)
    assert found and found["id"] == "a1"


@pytest.mark.asyncio
async def test_loose_name_match(services):
    """用户报的名字常常带点出入（大小写、空格）——别因此说找不到。"""
    from types import SimpleNamespace

    from agent_platform.assistant_agent import WorkflowConcierge

    _add_app(services, "a1", "Mech Smoke Greeting")
    concierge = WorkflowConcierge(
        SimpleNamespace(workflow_store=services._store), SimpleNamespace())
    found = await concierge._resolve_app("mechsmokegreeting")
    assert found and found["id"] == "a1"


@pytest.mark.asyncio
async def test_archiving_a_scheduled_workflow_says_so(services):
    """收起带定时的工作流会连定时一起停——调度器遍历的就是「未归档」这个列表。
    这是隐式副作用，必须说在前面。"""
    import json as _json

    store = services._store
    snap = _json.dumps({"name": "定时的", "workflow": {"nodes": [
        {"id": "s", "type": "schedule_trigger",
         "config": {"hour": 8, "minute": 0, "timezone": "UTC"}}]}})
    _add_app(services, "sched", "定时的", published=1)
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO application_versions(application_id,version,snapshot_json,"
            "content_hash,validation_report_json,created_at) VALUES('sched',1,?,?,'{}','2020-01-01')",
            (snap, "h" * 64))

    result = await store.set_archived("sched", True)
    assert result["was_scheduled"] is True
    assert "定时也一并停了" in result["schedule_effect"]
    assert "sched" not in [a["id"] for a in await store.list_applications()]

    back = await store.set_archived("sched", False)
    assert back["was_scheduled"] is True
    assert "恢复" in back["schedule_effect"]


@pytest.mark.asyncio
async def test_archiving_a_plain_workflow_says_nothing_extra(services):
    """没有定时的就别提定时，免得平白吓人。"""
    _add_app(services, "plain", "没定时的")
    result = await services._store.set_archived("plain", True)
    assert result["was_scheduled"] is False
    assert result["schedule_effect"] == ""
