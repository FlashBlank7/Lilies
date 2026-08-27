"""工作流健康度：全败/连败/定时未触发的判定与排序。"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_platform.overview import build_health


class _FakeCursor:
    """execute() 的返回物：只需支持 fetchall() 与直接迭代（真 sqlite3 游标两者都行）。"""

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _FakeConn:
    def __init__(self, apps, runs, recent, drafts, errors=()):
        self._apps, self._runs, self._recent = apps, runs, recent
        self._drafts, self._errors = drafts, errors

    def execute(self, sql, params=()):
        if "snapshot_json" in sql:          # 快照查询里也含 "applications"，先判它
            return _FakeCursor(self._drafts)
        if "FROM applications" in sql:
            return _FakeCursor(self._apps)
        if "GROUP BY application_id" in sql:
            return _FakeCursor(self._runs)
        if "status='failed'" in sql:
            return _FakeCursor(self._errors)
        if "ORDER BY created_at DESC LIMIT" in sql:
            return _FakeCursor(self._recent)
        # 定时口径改为发布版快照后 SQL 从 application_drafts 换成 application_versions；
        # 桩按"取快照"这个语义匹配，别再绑死表名（绑死表名正是上次改口径时炸的原因）
        if "snapshot_json" in sql:
            return _FakeCursor(self._drafts)
        raise AssertionError(sql)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _services(apps, runs, recent, drafts, errors=()):
    storage = SimpleNamespace(
        _connect=lambda: _FakeConn(apps, runs, recent, drafts, errors))
    return SimpleNamespace(workflow_store=SimpleNamespace(storage=storage))


SCHEDULED = json.dumps({"workflow": {"nodes": [{"type": "schedule_trigger"}]}})
PLAIN = json.dumps({"workflow": {"nodes": [{"type": "start"}]}})


@pytest.mark.asyncio
async def test_all_failed_is_broken():
    report = await build_health(_services(
        apps=[{"id": "a1", "name": "全败的"}],
        runs=[{"application_id": "a1", "runs": 6, "succeeded": 0,
               "last_success": None, "last_run": "2026-08-27"}],
        recent=[{"application_id": "a1", "status": "failed"}],
        drafts=[{"application_id": "a1", "snapshot_json": PLAIN}],
    ))
    item = report["items"][0]
    assert item["state"] == "broken"
    assert "全部失败" in item["reason"]
    assert report["counts"]["broken"] == 1


@pytest.mark.asyncio
async def test_fail_streak_is_broken_even_with_past_success():
    report = await build_health(_services(
        apps=[{"id": "a1", "name": "最近连败"}],
        runs=[{"application_id": "a1", "runs": 10, "succeeded": 7,
               "last_success": "2026-08-20", "last_run": "2026-08-27"}],
        recent=[{"application_id": "a1", "status": "failed"}] * 3
               + [{"application_id": "a1", "status": "succeeded"}],
        drafts=[{"application_id": "a1", "snapshot_json": PLAIN}],
    ))
    item = report["items"][0]
    assert item["state"] == "broken"
    assert item["fail_streak"] == 3
    assert "连续失败 3" in item["reason"]


@pytest.mark.asyncio
async def test_scheduled_but_never_ran_is_stale():
    report = await build_health(_services(
        apps=[{"id": "a1", "name": "定时没动"}],
        runs=[],
        recent=[],
        drafts=[{"application_id": "a1", "snapshot_json": SCHEDULED}],
    ))
    item = report["items"][0]
    assert item["state"] == "stale"
    assert item["scheduled"] is True
    assert "一次都没运行" in item["reason"]


@pytest.mark.asyncio
async def test_healthy_and_ordering():
    report = await build_health(_services(
        apps=[{"id": "ok1", "name": "健康"}, {"id": "bad", "name": "坏的"},
              {"id": "sch", "name": "定时没动"}],
        runs=[{"application_id": "ok1", "runs": 5, "succeeded": 5,
               "last_success": "2026-08-27", "last_run": "2026-08-27"},
              {"application_id": "bad", "runs": 4, "succeeded": 0,
               "last_success": None, "last_run": "2026-08-27"}],
        recent=[{"application_id": "bad", "status": "failed"}],
        drafts=[{"application_id": "sch", "snapshot_json": SCHEDULED}],
    ))
    assert [i["state"] for i in report["items"]] == ["broken", "stale", "ok"]
    assert report["counts"] == {"broken": 1, "stale": 1, "ok": 1}


@pytest.mark.asyncio
async def test_broken_snapshot_does_not_crash():
    report = await build_health(_services(
        apps=[{"id": "a1", "name": "坏快照"}],
        runs=[{"application_id": "a1", "runs": 2, "succeeded": 2,
               "last_success": "2026-08-27", "last_run": "2026-08-27"}],
        recent=[],
        drafts=[{"application_id": "a1", "snapshot_json": "{不是JSON"}],
    ))
    assert report["items"][0]["state"] == "ok"
    assert report["items"][0]["scheduled"] is False


@pytest.mark.asyncio
async def test_broken_carries_last_error_summary():
    """体检要说清"为什么坏"：带上最近一次失败的错误摘要，剥掉 node X failed 前缀。"""
    report = await build_health(_services(
        apps=[{"id": "a1", "name": "坏的"}],
        runs=[{"application_id": "a1", "runs": 3, "succeeded": 0,
               "last_success": None, "last_run": "2026-08-27"}],
        recent=[{"application_id": "a1", "status": "failed"}],
        drafts=[],
        errors=[{"application_id": "a1",
                 "error": "node fetch failed: HTTPConnectionPool timeout after 30s"},
                {"application_id": "a1", "error": "更早的错误，不该被采用"}],
    ))
    item = report["items"][0]
    assert item["last_error"] == "HTTPConnectionPool timeout after 30s"
    assert "全部失败：HTTPConnectionPool timeout" in item["reason"]


@pytest.mark.asyncio
async def test_stale_has_no_error_noise():
    """停摆的工作流没跑过，不该硬塞别人的错误。"""
    report = await build_health(_services(
        apps=[{"id": "a1", "name": "定时没动"}],
        runs=[], recent=[],
        drafts=[{"application_id": "a1", "snapshot_json": SCHEDULED}],
        errors=[],
    ))
    item = report["items"][0]
    assert item["state"] == "stale"
    assert item["last_error"] == ""
    assert "：" not in item["reason"]


def test_brief_error_shapes():
    from agent_platform.overview import _brief_error

    assert _brief_error("") == ""
    assert _brief_error("multi\nline\nerror") == "multi line error"
    assert len(_brief_error("x" * 300)) == 110
    # 前缀只在靠前出现时才剥，避免吃掉正文里的 " failed: "
    assert _brief_error("a" * 80 + " failed: tail").startswith("aaa")


def test_brief_error_used_by_overview_failures() -> None:
    """回归：失败原因的权威来源是顶层 error 列。

    WorkflowRunState 模型没有 error 字段（'error' in model_fields == False），
    所以此前 recent_failures 读 state_json.$.error 恒为空——today/网页/桌面通知
    四个消费点全都显示"失败但没有原因"。
    """
    from agent_platform.workflow_models import WorkflowRunState

    assert "error" not in WorkflowRunState.model_fields  # 前提：state 里根本没有它

    sql = Path("platform/backend/src/agent_platform/overview.py").read_text(encoding="utf-8")
    assert "COALESCE(r.error, json_extract(r.state_json,'$.error'), '') AS error" in sql
