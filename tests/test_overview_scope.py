"""统计口径：只算发布版真实运行，不算草稿自测。

用**真 SQLite**而不是桩——桩数据正是把"整表 run 都是真实运行"这个错误假设
固化下来的地方（真机 314 条运行里 255 条是搭建期自测，全部混进了统计：
体检因自测失败把工作流判 broken，而 repair_workflow 会拿这个判定自动开
修复构建，这是唯一会自动花钱的路径）。
"""

from __future__ import annotations

import pytest

from agent_platform.overview import build_health, build_overview
from helpers_overview import PLAIN_SNAPSHOT, SCHEDULED_SNAPSHOT, _seed, services  # noqa: F401


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


# ── 定时静默失效：跑过、然后悄悄不跑了 ──────────────────────────────

def test_last_expected_fire_respects_timezone():
    from datetime import datetime, timezone as tz

    from agent_platform.overview import _last_expected_fire

    config = {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai"}
    # 14:37 CST：今天 8 点已过，上一次本该开火是今天 08:00 CST = 00:00 UTC
    got = _last_expected_fire(config, datetime(2026, 8, 28, 6, 37, tzinfo=tz.utc))
    assert got == datetime(2026, 8, 28, 0, 0, tzinfo=tz.utc)
    # 06:37 CST：今天还没到点，上一次是昨天
    got = _last_expected_fire(config, datetime(2026, 8, 27, 22, 37, tzinfo=tz.utc))
    assert got == datetime(2026, 8, 27, 0, 0, tzinfo=tz.utc)


def test_overdue_judgements():
    from datetime import datetime, timezone as tz

    from agent_platform.overview import _overdue

    config = {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai"}
    now = datetime(2026, 8, 28, 6, 37, tzinfo=tz.utc)
    assert _overdue(config, "2026-08-28T00:00:10+00:00", now)[0] is False  # 今天开过
    assert _overdue(config, "2026-08-27T00:00:15+00:00", now)[0] is True   # 昨天开过，今天没开
    assert _overdue(config, None, now)[0] is True                          # 从没开过
    assert _overdue({"hour": "x"}, None, now) == (False, "")               # 坏配置不误报


def test_overdue_grace_period():
    """调度器有抖动、任务本身要跑一会儿——卡太紧会天天误报。"""
    from datetime import datetime, timezone as tz

    from agent_platform.overview import _overdue

    config = {"hour": 8, "minute": 0, "timezone": "UTC"}
    now = datetime(2026, 8, 28, 9, 0, tzinfo=tz.utc)
    # 比应开时刻早 30 分钟开的火（提前触发/时钟漂移）仍算按时
    assert _overdue(config, "2026-08-28T07:30:00+00:00", now)[0] is False
    # 早两小时的就算逾期了
    assert _overdue(config, "2026-08-28T06:00:00+00:00", now)[0] is True


@pytest.mark.asyncio
async def test_health_flags_schedule_that_stopped_firing(services):
    """跑过、发布版有定时、但很久没开火——要报出来。"""
    _seed(services, real_runs=["succeeded"] * 2,
          version_snapshot=SCHEDULED_SNAPSHOT, draft_snapshot=PLAIN_SNAPSHOT)
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO schedule_fires(application_id,version,node_id,local_date,run_id,"
            "created_at) VALUES('app-1',1,'s','2020-01-01','r-old','2020-01-01T08:00:00+00:00')")
    report = await build_health(services)
    item = report["items"][0]
    assert item["state"] == "stale", item
    assert item["overdue"] is True
    assert "定时没按时开火" in item["reason"]
    assert "2020-01-01" in item["reason"]      # 上次开火时间要说出来


@pytest.mark.asyncio
async def test_health_ok_when_schedule_fired_recently(services):
    """刚开过火的不能误报。"""
    from datetime import datetime, timezone as tz

    _seed(services, real_runs=["succeeded"],
          version_snapshot=SCHEDULED_SNAPSHOT, draft_snapshot=PLAIN_SNAPSHOT)
    now = datetime.now(tz.utc).isoformat()
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO schedule_fires(application_id,version,node_id,local_date,run_id,"
            "created_at) VALUES('app-1',1,'s','2026-08-28','r-now',?)", (now,))
    report = await build_health(services)
    assert report["items"][0]["state"] == "ok", report["items"][0]
