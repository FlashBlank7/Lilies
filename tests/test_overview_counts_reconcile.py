"""面板上的数字必须对得上账：失败计数 = 失败清单解释得了的次数。

回归背景（2026-08-29 真机测量）：归档过滤加了一半。
失败清单、应用列表、体检都带 `a.archived_at IS NULL`，
而 runs_today 和本周走势**根本没 join applications**，
于是已归档工作流的旧运行照样进总数。

真机数字：本周失败 25 次，清单最多解释 13 次——
另外 12 次属于已退休的工作流，业主看得见数字，查不到出处。
124 条运行里 42 条（34%）属于已归档工作流。

这类 bug 单看任何一个查询都是对的，只有把两个数字摆在一起才露馅，
所以断言写在"两个数字之间"。而且调的是**真的** build_overview——
把 SQL 抄进测试里自己跟自己对照，产品那边掉了过滤照样绿。
"""
import pytest

from agent_platform.overview import build_overview

from helpers_overview import services  # noqa: F401  (pytest fixture)


def _seed(services, *, app_id, archived, statuses, version=1):
    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,"
            "active_version,archived_at,created_at,updated_at) "
            "VALUES(?,?,'','','workflow',1,?,datetime('now'),datetime('now'))",
            (app_id, app_id, "2026-08-20T00:00:00+00:00" if archived else None))
        for index, status in enumerate(statuses):
            # 每条给不同时刻：时间戳全同的话 SQLite 的顺序不定，测试会飘
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,"
                "status,state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,?,?,NULL,?,'{}','{}',?,datetime('now',?),datetime('now',?))",
                (f"{app_id}-{index}", app_id, version, status,
                 "跑挂了" if status == "failed" else None,
                 f"-{index} seconds", f"-{index} seconds"))


def _listed(overview) -> int:
    """失败清单解释得了多少次失败（合并同因后要把 ×N 加回来）。"""
    return sum(row.get("count", 1) for row in overview.get("recent_failures", []))


@pytest.fixture
def seeded(services):  # noqa: F811
    """一个在用、一个已退休，各自有成功和失败的运行。"""
    _seed(services, app_id="live", archived=False, statuses=["succeeded", "failed"])
    _seed(services, app_id="gone", archived=True,
          statuses=["succeeded", "failed", "failed"])
    return services


async def _overview(services):
    return await build_overview(services)


@pytest.mark.asyncio
async def test_today_failed_count_matches_the_list(seeded):
    overview = await _overview(seeded)
    assert overview["runs_today"]["failed"] == _listed(overview), \
        "今天的失败数和清单对不上，业主查不到出处"


@pytest.mark.asyncio
async def test_the_retired_workflows_failures_are_not_counted(seeded):
    """退休那个有 2 次失败，一次都不该算。"""
    assert (await _overview(seeded))["runs_today"]["failed"] == 1


@pytest.mark.asyncio
async def test_the_retired_workflows_successes_are_not_counted_either(seeded):
    """只滤失败不滤成功的话，成功率会算错。"""
    assert (await _overview(seeded))["runs_today"]["succeeded"] == 1


@pytest.mark.asyncio
async def test_a_live_failure_is_still_counted(seeded):
    """过滤不能宽到把在用的也滤掉——那是修出个新 bug。"""
    _seed(seeded, app_id="live2", archived=False, statuses=["failed"])
    overview = await _overview(seeded)
    assert overview["runs_today"]["failed"] == 2
    assert _listed(overview) == 2


@pytest.mark.asyncio
async def test_draft_runs_are_excluded_from_both_sides(seeded):
    """version IS NULL 是草稿试跑，两边口径同样要一致。"""
    _seed(seeded, app_id="drafty", archived=False, statuses=["failed"], version=None)
    overview = await _overview(seeded)
    assert overview["runs_today"]["failed"] == _listed(overview)


@pytest.mark.asyncio
async def test_the_week_series_excludes_retired_workflows_too(seeded):
    """本周走势是第三个口径，同样不能掉队。"""
    overview = await _overview(seeded)
    week = overview["week"]
    failed = sum(day["fail"] for day in week)
    assert failed == _listed(overview), "本周失败总数和清单对不上"
