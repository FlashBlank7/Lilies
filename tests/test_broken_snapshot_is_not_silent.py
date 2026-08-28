"""一个读不出来的流程图，不该让那个工作流的定时无声消失。

回归背景（2026-08-29）：overview 里解析快照取节点，失败就 `except Exception: continue`。
跳过本身是对的——一个坏快照不能拖垮整块统计。问题是**一个字都不说**：

  · 那个工作流的定时从面板上消失了
  · 体检里「有定时却没跑起来」也是基于这张表，于是它同时脱离了监控
  · 而呈现出来的样子是「这个工作流没有定时」，和真的没定时长得一模一样

也就是说一个定时任务可以悄悄脱离监控，没有任何迹象。
现在跳过照旧，但留一行 warning。
（这条日志今天之前也是白写的——后端根本没配 logging，见 cli.configure_logging。）
"""
import json
import logging

import pytest

from agent_platform.overview import build_overview

from helpers_overview import services  # noqa: F401  (pytest fixture)

GOOD = json.dumps({"name": "好的", "workflow": {"nodes": [
    {"id": "s", "type": "schedule_trigger",
     "config": {"hour": 8, "minute": 0, "timezone": "Asia/Shanghai"}}]}})
# 合法 JSON，但没有 workflow.nodes——真机上快照结构变过，这种坏法是真会发生的
BROKEN = json.dumps({"name": "坏的", "nodes": []})


def _seed(services, app_id, snapshot):  # noqa: F811
    storage = services.workflow_store.storage
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,"
            "active_version,created_at,updated_at) "
            "VALUES(?,?,'','','workflow',1,datetime('now'),datetime('now'))",
            (app_id, app_id))
        conn.execute(
            "INSERT INTO application_versions(application_id,version,snapshot_json,"
            "content_hash,validation_report_json,created_at) "
            "VALUES(?,1,?,?,'{}',datetime('now'))",
            (app_id, snapshot, "h" * 64))


@pytest.mark.asyncio
async def test_a_broken_snapshot_does_not_hide_the_others(services):  # noqa: F811
    """坏的那个跳过，好的那个还得在——不能一坏坏一片。"""
    _seed(services, "broken", BROKEN)
    _seed(services, "good", GOOD)
    overview = await build_overview(services)
    names = {row["workflow"] for row in overview["schedules"]}
    assert "good" in names, "一个坏快照把别人的定时也带走了"


@pytest.mark.asyncio
async def test_a_broken_snapshot_is_logged(services, caplog):  # noqa: F811
    """跳过可以，不吭声不行。"""
    _seed(services, "broken", BROKEN)
    with caplog.at_level(logging.WARNING, logger="agent_platform.overview"):
        await build_overview(services)
    assert any("broken" in record.getMessage() for record in caplog.records), \
        f"坏快照被无声跳过了：{[r.getMessage() for r in caplog.records]}"


@pytest.mark.asyncio
async def test_the_log_says_what_the_user_will_notice(services, caplog):  # noqa: F811
    """日志要说清后果，不然看日志的人不知道这条重不重要。"""
    _seed(services, "broken", BROKEN)
    with caplog.at_level(logging.WARNING, logger="agent_platform.overview"):
        await build_overview(services)
    message = " ".join(record.getMessage() for record in caplog.records)
    assert "定时" in message and "面板" in message


@pytest.mark.asyncio
async def test_a_healthy_snapshot_logs_nothing(services, caplog):  # noqa: F811
    """好快照不该刷警告——日志里全是狼来了，真出事就没人看了。"""
    _seed(services, "good", GOOD)
    with caplog.at_level(logging.WARNING, logger="agent_platform.overview"):
        await build_overview(services)
    assert caplog.records == []
