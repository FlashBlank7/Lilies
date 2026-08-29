"""体检判「坏了」的两条判据，都会直接导致自动花钱——所以要钉死。

repair_workflow 在业主没给指示时，靠的就是体检的 broken 判定去自动开构建
（全系统唯一自动花钱的路径）。判据一松，钱就白烧，而且烧在一个
根本没坏的工作流上。

变异验证（2026-08-29）发现两处一松就没人管：

1. **把「调用方传错参数」也算成工作流的错。**
   真机上「文本行数与净字数统计」13 次失败全是
   `missing required input: text`——调用时没给必填输入。
   这类失败一旦计入，它立刻被判 broken，然后被自动开一个修复构建，
   去"修"一个没坏的东西。

2. **连败计数遇到成功不中断。**
   失败散落在成功之间的工作流会被累计成一长串连败 → broken → 自动修。
   连败之所以是"连"，正是因为中间没有成功。
"""

from __future__ import annotations

import pytest

from agent_platform.overview import build_health
from helpers_overview import _seed, services  # noqa: F401

CALLER_ERROR = "node start failed: missing required input: text"


def _state(report, app_id: str = "app-1") -> str:
    item = next(i for i in report["items"] if i["application_id"] == app_id)
    return item["state"]


@pytest.mark.asyncio
async def test_caller_mistakes_do_not_make_a_workflow_broken(services):
    """调用方没给必填输入——那是调用方的事，不是工作流坏了。"""
    _seed(services, real_runs=["failed"] * 13, fail_error=CALLER_ERROR)
    report = await build_health(services)
    assert _state(report) != "broken", report["items"]


@pytest.mark.asyncio
async def test_a_real_failure_still_makes_it_broken(services):
    """别把闸开太大：确实是工作流自己的错，就要判坏。"""
    _seed(services, real_runs=["failed"] * 4,
          fail_error="collection expression requires an array")
    report = await build_health(services)
    assert _state(report) == "broken"


@pytest.mark.asyncio
async def test_caller_mistakes_mixed_with_real_ones_still_count_the_real_ones(services):
    """混着来的时候，真错照样要数——不能因为有调用方的错就整体豁免。"""
    _seed(services, real_runs=["failed"] * 4,
          fail_error="collection expression requires an array")
    with services.workflow_store.storage._connect() as conn:
        conn.executemany(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES(?,'app-1',1,NULL,'failed','{}','{}',?,"
            "datetime('now','-30 seconds'),datetime('now','-30 seconds'))",
            [(f"caller-{i}", CALLER_ERROR) for i in range(5)])
    report = await build_health(services)
    assert _state(report) == "broken"


@pytest.mark.asyncio
async def test_a_success_breaks_the_failure_streak(services):
    """连败之所以是"连"，正是因为中间没有成功。

    _seed 按顺序插入、时间递增，所以最后一条是最新的：
    这里最新的一条是成功，连败应当归零。
    """
    _seed(services, real_runs=["failed", "failed", "failed", "failed", "succeeded"])
    report = await build_health(services)
    assert _state(report) != "broken", report["items"]


@pytest.mark.asyncio
async def test_an_unbroken_streak_of_three_is_still_broken(services):
    """反向：真的连着败三次，就得判坏——别把闸关死。"""
    _seed(services, real_runs=["succeeded", "failed", "failed", "failed"])
    report = await build_health(services)
    assert _state(report) == "broken"


@pytest.mark.asyncio
async def test_two_in_a_row_is_not_yet_broken(services):
    """阈值是 3，不是 1。

    把它降到 1 的话，一次偶发失败就会让工作流被判坏，
    而 repair_workflow 会据此自动开一个付费构建。
    偶发失败在真机上很常见（今天这台机器上 83 次运行里 13 次失败），
    每一次都自动开构建，等于把偶发变成持续花钱。
    """
    _seed(services, real_runs=["succeeded", "failed", "failed"])
    report = await build_health(services)
    assert _state(report) != "broken", report["items"]


@pytest.mark.asyncio
async def test_never_succeeded_is_broken_even_without_a_streak_of_three(services):
    """另一条判据：窗口内跑过、但一次都没成过——两次也算坏。

    这条和上一条不冲突：有成功记录才走连败阈值，从没成过是另一回事。
    """
    _seed(services, real_runs=["failed", "failed"])
    report = await build_health(services)
    assert _state(report) == "broken"
