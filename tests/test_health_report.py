"""工作流健康度：全败/连败/定时未触发的判定与排序。"""

import json
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
    def __init__(self, apps, runs, recent, drafts):
        self._apps, self._runs, self._recent, self._drafts = apps, runs, recent, drafts

    def execute(self, sql, params=()):
        if "FROM applications" in sql:
            return _FakeCursor(self._apps)
        if "GROUP BY application_id" in sql:
            return _FakeCursor(self._runs)
        if "ORDER BY created_at DESC LIMIT" in sql:
            return _FakeCursor(self._recent)
        if "application_drafts" in sql:
            return _FakeCursor(self._drafts)
        raise AssertionError(sql)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _services(apps, runs, recent, drafts):
    storage = SimpleNamespace(_connect=lambda: _FakeConn(apps, runs, recent, drafts))
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
