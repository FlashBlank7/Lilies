"""收起来的工作流不该继续出现在统计与体检里。

回归背景（2026-08-28 真机）：把 12 个基准实验残留收起来后，
列表只剩 3 个，总览却报「已发布 17」——收拾完数字反而变大了。

体检那条更烦人：收起来之后调度器不再触发它（list_applications 会过滤），
于是它永远满足「有定时却没运行」= 永远被判 stale，一个必然的假警报。
业主收它就是为了别再管它。
"""

import pytest

from agent_platform.overview import build_health, build_overview
from tests.helpers_overview import _seed, services  # noqa: F401 - services 是夹具


def _archive(services, app_id: str) -> None:
    with services.workflow_store.storage._connect() as conn:
        conn.execute("UPDATE applications SET archived_at=datetime('now') WHERE id=?",
                     (app_id,))


@pytest.mark.asyncio
async def test_archived_workflow_leaves_the_published_count(services) -> None:
    # 只给一个种运行记录：_seed 的 run id 是固定的，两次都种会撞主键
    _seed(services, app_id="keep", real_runs=["succeeded"])
    _seed(services, app_id="gone", real_runs=())
    assert (await build_overview(services))["published_workflows"] == 2
    _archive(services, "gone")
    assert (await build_overview(services))["published_workflows"] == 1


@pytest.mark.asyncio
async def test_archived_workflow_is_not_health_checked(services) -> None:
    # 全败 → 本来会被判 broken；收起来之后就不该再点它的名
    _seed(services, app_id="gone", real_runs=["failed"] * 3)
    report = await build_health(services)
    assert [item["application_id"] for item in report["items"]] == ["gone"]
    _archive(services, "gone")
    assert (await build_health(services))["items"] == []


@pytest.mark.asyncio
async def test_archived_failures_leave_the_panel(services) -> None:
    _seed(services, app_id="gone", real_runs=["failed"])
    assert (await build_overview(services))["recent_failures"]
    _archive(services, "gone")
    assert (await build_overview(services))["recent_failures"] == []


@pytest.mark.asyncio
async def test_restoring_brings_it_all_back(services) -> None:
    """收起来是可逆的：拿回来之后统计与体检都要恢复。"""
    _seed(services, app_id="gone", real_runs=["failed"] * 3)
    _archive(services, "gone")
    with services.workflow_store.storage._connect() as conn:
        conn.execute("UPDATE applications SET archived_at=NULL WHERE id='gone'")
    assert (await build_overview(services))["published_workflows"] == 1
    assert [i["application_id"] for i in (await build_health(services))["items"]] == ["gone"]
