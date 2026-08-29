"""工作流体检：分类判定与错误摘要。

历史教训（2026-08-28）：这个文件原本用 SQL 桩，桩把"整表 run 都是真实运行"
这个错误假设固化了下来——功能算错口径，测试却全绿；而且每次改 SQL 桩就碎。
现在分类判定全部走真 SQLite（helpers_overview 的夹具），
只有纯函数与源码契约留在这里做轻量断言。
"""

from __future__ import annotations

import pytest

from agent_platform.overview import build_health
from helpers_overview import PLAIN_SNAPSHOT, SCHEDULED_SNAPSHOT, _seed, services  # noqa: F401


@pytest.mark.asyncio
async def test_all_failed_is_broken(services):
    _seed(services, real_runs=["failed"] * 6)
    item = (await build_health(services))["items"][0]
    assert item["state"] == "broken"
    assert "全部失败" in item["reason"]


@pytest.mark.asyncio
async def test_fail_streak_is_broken_even_with_past_success(services):
    """曾经跑得好好的，最近连败——照样要报。"""
    _seed(services, real_runs=["succeeded"] * 7 + ["failed"] * 3)
    item = (await build_health(services))["items"][0]
    assert item["state"] == "broken"
    assert item["fail_streak"] == 3


@pytest.mark.asyncio
async def test_scheduled_but_never_ran_is_stale(services):
    _seed(services, version_snapshot=SCHEDULED_SNAPSHOT, draft_snapshot=PLAIN_SNAPSHOT)
    item = (await build_health(services))["items"][0]
    assert item["state"] == "stale"
    assert item["scheduled"] is True


@pytest.mark.asyncio
async def test_chinese_node_message_is_not_overwritten(services):
    """节点自己抛的中文说明比任何模板都具体，别拿模板盖掉。"""
    _seed(services, real_runs=["failed"] * 3,
          fail_error="node calc failed: 公式包含不支持的字符 '$'（位置 7）")
    item = (await build_health(services))["items"][0]
    assert item["last_error"] == "公式包含不支持的字符 '$'（位置 7）"


@pytest.mark.asyncio
async def test_healthy_workflow_is_ok(services):
    _seed(services, real_runs=["succeeded"] * 3)
    report = await build_health(services)
    assert report["items"][0]["state"] == "ok"
    assert report["counts"] == {"broken": 0, "stale": 0, "waiting": 0, "ok": 1}


@pytest.mark.asyncio
async def test_broken_carries_last_error_summary(services):
    """体检要说清"为什么坏"：剥掉 node X failed 前缀，并且说人话。"""
    _seed(services, real_runs=["failed"] * 3,
          fail_error="node fetch failed: HTTPConnectionPool timeout after 30s")
    item = (await build_health(services))["items"][0]
    assert "node fetch failed" not in item["last_error"]
    assert "HTTPConnectionPool" not in item["last_error"]   # 英文原文不见业主
    assert item["last_error"] == "连不上外部服务或等待超时"
    # reason 里嵌的是同一句，所以也该是人话
    assert "HTTPConnectionPool" not in item["reason"]
    assert "连不上外部服务" in item["reason"]


@pytest.mark.asyncio
async def test_caller_errors_do_not_mark_broken(services):
    """"你调用方式不对"不等于"工作流坏了"。

    真机上就是被冒烟脚本刷出来的：故意不带参数试跑三次，
    工作流被判 broken，还可能触发自动修复构建（花钱）。
    """
    _seed(services, real_runs=["failed"] * 3,
          fail_error="node start failed: missing required input: sales")
    item = (await build_health(services))["items"][0]
    assert item["state"] == "ok", item
    assert item["last_error"] == ""


@pytest.mark.asyncio
async def test_real_failure_still_breaks(services):
    """调用方错误被跳过，但不能把它当免死金牌——真实失败照报。"""
    _seed(services, real_runs=["failed"] * 3, fail_error="node calc failed: 除以零")
    item = (await build_health(services))["items"][0]
    assert item["state"] == "broken"
    assert "除以零" in item["reason"]


@pytest.mark.asyncio
async def test_broken_snapshot_does_not_crash(services):
    _seed(services, real_runs=["succeeded"] * 2, version_snapshot="{不是JSON")
    item = (await build_health(services))["items"][0]
    assert item["state"] == "ok"
    assert item["scheduled"] is False


def test_brief_error_shapes() -> None:
    from agent_platform.overview import _brief_error

    assert _brief_error("") == ""
    assert _brief_error("multi\nline\nerror") == "multi line error"
    # 截了要说（2026-08-30）：正文仍是 110，省略号加在它之外——
    # 原来干净地截在 110，看的人分不出这是全文还是半截话。
    assert _brief_error("x" * 300) == "x" * 110 + "…"
    # 前缀只在靠前出现时才剥，避免吃掉正文里的 " failed: "
    assert _brief_error("a" * 80 + " failed: tail").startswith("aaa")


def test_caller_error_detection() -> None:
    from agent_platform.overview import _is_caller_error

    assert _is_caller_error("node start failed: missing required input: sales")
    assert _is_caller_error("Input validation failed for 'month'")
    assert not _is_caller_error("node calc failed: 除以零")
    assert not _is_caller_error("")


def test_failure_reason_comes_from_top_level_error_column() -> None:
    """前提：WorkflowRunState 模型没有 error 字段，读 state.error 恒为空。"""
    from pathlib import Path

    from agent_platform.workflow_models import WorkflowRunState

    assert "error" not in WorkflowRunState.model_fields
    sql = Path("platform/backend/src/agent_platform/overview.py").read_text(encoding="utf-8")
    assert "COALESCE(r.error, json_extract(r.state_json,'$.error'), '') AS error" in sql


@pytest.mark.asyncio
async def test_in_flight_run_is_waiting_not_broken(services):
    """还在跑/等人工确认 ≠ 坏了。

    此前 runs 计入 queued/running/paused，一个正在跑的工作流会被判"全部失败"，
    而 repair_workflow 在没给指示时会拿这个判定自动开构建——唯一会花钱的路径。
    """
    _seed(services, real_runs=["running"])
    item = (await build_health(services))["items"][0]
    assert item["state"] == "waiting", item
    assert "尚无终态结果" in item["reason"]


@pytest.mark.asyncio
async def test_paused_run_is_waiting(services):
    _seed(services, real_runs=["paused"])
    assert (await build_health(services))["items"][0]["state"] == "waiting"


@pytest.mark.asyncio
async def test_in_flight_does_not_mask_real_failures(services):
    """有终态失败时照报 broken，别把 waiting 当免死金牌。"""
    _seed(services, real_runs=["failed", "running"])
    item = (await build_health(services))["items"][0]
    assert item["state"] == "broken"


@pytest.mark.asyncio
async def test_platform_congestion_is_not_the_workflows_fault(services):
    """数据库繁忙是平台自身拥塞，不该记在工作流头上。"""
    _seed(services, real_runs=["failed"] * 3,
          fail_error="database is locked")
    assert (await build_health(services))["items"][0]["state"] == "ok"
