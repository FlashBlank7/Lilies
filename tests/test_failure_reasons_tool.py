"""按原因归类失败：数要来自全量 SQL，话要说人话。

来由（真机 2026-08-29）：问「「文本行数与净字数统计」失败的原因归类看是什么？」，
管家去翻 recent_runs，答**「最近有两次失败」**——而那个工作流一共失败 14 次。
同一轮里换个问法（走 run_counts）答的是 13。**翻一页当全部**，
这个仓已经中过五次了；平台一句 SQL 就有的东西，不该让它去翻。

两件事一起钉：
· 数：来自全量分组，不是最近几条；
· 话：分类名是给机器看的英文 slug（missing_resource 这种），
  递给模型它会原样念给业主——真机上出现过「没有跑起来出错的（broken）」
  这种夹带，所以状态词一律先翻。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_platform.assistant_agent import _FAILURE_REASON_WORDS, WorkflowConcierge
from agent_platform.config import Settings
from agent_platform.observability import _classify_failure
from agent_platform.storage import Storage
from agent_platform.workflow_storage import WorkflowStorage


@pytest.fixture
def concierge(tmp_path: Path):
    storage = Storage(tmp_path / "d")
    store = WorkflowStorage(storage)

    async def _init() -> None:
        await storage.initialize()
        await store.initialize()

    asyncio.run(_init())
    with storage._connect() as conn:
        conn.execute(
            "INSERT INTO applications(id,name,description,requirement,mode,"
            "active_version,created_at,updated_at) "
            "VALUES('a1','被测','','','workflow',1,datetime('now'),datetime('now'))")
        conn.execute(
            "INSERT INTO application_drafts(application_id,revision,snapshot_json,"
            "content_hash,validation_report_json,updated_at) "
            "VALUES('a1',1,'{}',?,'{}',datetime('now'))", ("h" * 64,))
        # 20 条同因失败 + 2 条另一因：一页装不下，正是那个 bug 的形状
        for i in range(20):
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,"
                "status,state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,'a1',1,NULL,'failed','{}','{}',?,datetime('now'),datetime('now'))",
                (f"r{i}", "node start failed: missing required input: text"))
        for i in range(2):
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,"
                "status,state_json,outputs_json,error,created_at,updated_at) "
                "VALUES(?,'a1',1,NULL,'failed','{}','{}',?,datetime('now'),datetime('now'))",
                (f"t{i}", "request timed out after 60s"))
    services = SimpleNamespace(
        storage=storage, workflow_store=store, _store=store,
        settings=Settings(api_token="t", data_dir=tmp_path / "d",
                          workspace_root=tmp_path / "w"),
        scheduler=SimpleNamespace(health=lambda: {"alive": True}))
    return WorkflowConcierge(services, settings=None)


def _ask(concierge, **args):
    return asyncio.run(concierge._exec("failure_reasons", args, user={}))


class TestTheCountsComeFromEverything:
    def test_the_total_is_the_real_total(self, concierge):
        """22 条，不是"最近一页"里那几条。"""
        assert _ask(concierge, name_or_id="被测")["一共失败了几次"] == 22

    def test_the_groups_add_up_to_the_total(self, concierge):
        result = _ask(concierge, name_or_id="被测")
        assert sum(g["几次"] for g in result["按原因分"]) == result["一共失败了几次"]

    def test_the_biggest_cause_comes_first(self, concierge):
        groups = _ask(concierge, name_or_id="被测")["按原因分"]
        assert [g["几次"] for g in groups] == [20, 2], groups

    def test_it_says_where_the_numbers_come_from(self, concierge):
        """不写这一句，模型还是可能把它当成"最近几条"转述。"""
        assert "不是最近几条" in _ask(concierge, name_or_id="被测")["这些数是怎么来的"]

    def test_an_example_run_is_given(self, concierge):
        """给个运行号才查得下去；一个数字没法追。"""
        assert _ask(concierge, name_or_id="被测")["按原因分"][0]["例子运行号"]


class TestItSpeaksHumanNotSlugs:
    def test_no_english_slug_reaches_the_model(self, concierge):
        blob = json.dumps(_ask(concierge, name_or_id="被测"), ensure_ascii=False)
        for slug in ("missing_resource", "api_timeout_or_rate_limit",
                     "data_shape_mismatch", "unknown"):
            assert slug not in blob, f"{slug} 原样递出去了"

    def test_every_classifier_output_has_a_translation(self):
        """分类器以后加一类而这里忘了翻，就会漏出 slug——把这件事钉死。

        直接从分类器的实现里取全部可能的返回值，而不是我手抄一份。
        """
        import inspect
        import re

        source = inspect.getsource(_classify_failure)
        produced = set(re.findall(r'return "([a-z_]+)"', source))
        assert produced, "没解析出分类名，这条测试就是空的"
        missing = produced - set(_FAILURE_REASON_WORDS)
        assert not missing, f"这些分类没有中文说法：{sorted(missing)}"


class TestTheUsualTwoMessages:
    def test_no_name_says_so(self, concierge):
        assert "没说是哪个" in _ask(concierge)["error"]

    def test_a_workflow_with_no_failures_says_zero(self, concierge):
        """反向：没失败过就是 0，别报成读不到。"""
        with concierge.services.storage._connect() as conn:
            conn.execute(
                "INSERT INTO applications(id,name,description,requirement,mode,"
                "active_version,created_at,updated_at) "
                "VALUES('a2','干净的','','','workflow',1,"
                "datetime('now'),datetime('now'))")
            conn.execute(
                "INSERT INTO application_drafts(application_id,revision,snapshot_json,"
                "content_hash,validation_report_json,updated_at) "
                "VALUES('a2',1,'{}',?,'{}',datetime('now'))", ("g" * 64,))
        result = _ask(concierge, name_or_id="干净的")
        assert result["一共失败了几次"] == 0
        assert result["按原因分"] == []
