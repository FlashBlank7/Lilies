"""工具返回里的话要是**事实**，不能是给工具调用者的指路。

回归背景（2026-08-29 真机）：问「有多少个还没发布的草稿？」，答

    有 12 个未发布的草稿没列出来。

12 是对的，可用户没在看任何列表——"没列出来"这半句没有着落。
它照抄了 list_workflows 的 note：

    另有 12 个未发布的草稿没列出来；要看它们传 only_published=false，
    要收拾用 tidy_workflows

这句话把**事实**（有 12 个草稿）和**指路**（传 only_published=false）
粘在一起了。模型要拿它当答案，就只能连指路一起念。

修法不是嘱咐它"别念"（那是请求不是保证），是**把指路挪走**：
参数怎么传写进 input_schema 的参数说明里——那儿一样是给模型看的，
但不在返回值里，也就没法被当成答案念出去。

同一个道理适用于所有 note：返回值里只放业主听得懂的话。
"""

from __future__ import annotations

import json
import re

import pytest

from helpers_overview import PLAIN_SNAPSHOT, _seed, services  # noqa: F401


def _add_draft_app(services, app_id: str, name: str) -> None:
    with services.workflow_store.storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,active_version,"
            "created_at,updated_at) VALUES(?,?,'','','workflow',NULL,"
            "datetime('now'),datetime('now'))", (app_id, name))
        conn.execute(
            "INSERT INTO application_drafts(application_id,revision,snapshot_json,"
            "content_hash,validation_report_json,updated_at) "
            "VALUES(?,1,?,?,'{}',datetime('now'))",
            (app_id, PLAIN_SNAPSHOT, app_id.ljust(64, "y")[:64]))


async def _list(services, **args):
    from agent_platform.assistant_agent import WorkflowConcierge

    services.workflow_store.list_applications = services._store.list_applications
    return await WorkflowConcierge(services, settings=None)._exec(
        "list_workflows", args, user={})


@pytest.mark.asyncio
async def test_the_draft_count_is_a_plain_fact(services):
    """「有几个草稿」得能直接读出来，不用从一句指路里抠。"""
    _seed(services, real_runs=[])
    for index in range(3):
        _add_draft_app(services, f"draft-{index}", f"草稿{index}")
    result = await _list(services)
    assert result["没发布的草稿有几个"] == 3


@pytest.mark.asyncio
async def test_the_counts_are_there_without_the_filter_too(services):
    """不带过滤那条路也得给计数——不然它只能逐行数，而它数错过。

    真机 2026-08-29：问「有多少个还没发布的草稿」，它传 only_published=false
    把 15 条全拿到手，逐行数 published_version，把 3 个已发布数成 2 个，
    答「13 个草稿」（真值 12）。带过滤那条路早就给了 hidden，
    这条路没有——同一个数只在一个出口给了。
    """
    _seed(services, real_runs=[])
    for index in range(3):
        _add_draft_app(services, f"draft-{index}", f"草稿{index}")
    result = await _list(services, only_published=False)
    assert result["一共几个"] == 4
    assert result["已发布几个"] == 1
    assert result["没发布的草稿有几个"] == 3
    assert len(result["workflows"]) == 4      # 确实拿到了全部，不是被过滤了


@pytest.mark.asyncio
async def test_the_counts_come_before_the_list(services):
    """整份结果超 4000 字会被截，截的是后面那截——数字不能排在长列表后面。"""
    _seed(services, real_runs=[])
    for index in range(3):
        _add_draft_app(services, f"draft-{index}", f"草稿{index}")
    keys = list(await _list(services, only_published=False))
    assert keys.index("已发布几个") < keys.index("workflows")
    assert keys.index("没发布的草稿有几个") < keys.index("workflows")


@pytest.mark.asyncio
async def test_nothing_in_the_payload_reads_like_a_parameter(services):
    """返回值里不该出现 `参数=值` 这种写法——那是给调用者的，不是给业主的。

    这条是机械闸：以后谁再往 note 里塞一句"传 xxx=false"，
    这里就会红。光在这一处改好，下一处照样会犯。
    """
    _seed(services, real_runs=[])
    _add_draft_app(services, "draft-0", "草稿零")
    result = await _list(services)
    blob = json.dumps(result, ensure_ascii=False)
    leaks = re.findall(r"[A-Za-z_]{3,}\s*=\s*(?:true|false|\d+|[\"'])", blob)
    assert not leaks, f"返回值里混进了参数写法：{leaks}"


@pytest.mark.asyncio
async def test_it_still_tells_the_model_drafts_exist(services):
    """不能因为怕泄漏就干脆不说——不说的话它会以为"总共就这些"，
    业主问起草稿时只能答找不到。要的是换个说法，不是闭嘴。
    """
    _seed(services, real_runs=[])
    _add_draft_app(services, "draft-0", "草稿零")
    result = await _list(services)
    assert "草稿" in result["note"]
    assert result["unpublished_hidden"] == 1


@pytest.mark.asyncio
async def test_no_note_when_there_are_no_drafts(services):
    """一个草稿都没有还提草稿，业主会以为自己漏了什么。

    计数字段照给（0 就是 0，那是事实），要没的是那句 note。
    """
    _seed(services, real_runs=[])
    result = await _list(services)
    assert "note" not in result
    assert result["没发布的草稿有几个"] == 0


@pytest.mark.asyncio
async def test_the_parameter_is_documented_where_the_model_can_see_it(services):
    """指路挪走了，但不能挪没——参数说明里必须还在，否则它不知道怎么看草稿。"""
    from agent_platform.assistant_agent import TOOLS

    tool = next(t for t in TOOLS if t.name == "list_workflows")
    said = tool.input_schema["properties"]["only_published"].get("description") or ""
    assert "草稿" in said and "false" in said, said
