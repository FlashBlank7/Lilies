"""运行指标与失败模式：276 行、两个接口，此前零测试。

真机一探就露了（这就是"量真机"那个镜头）：

  · `/api/v1/applications/{id}/failure-patterns` 对**每一个**应用都答 []，
    包括一个真实失败 70 次、其中 66 次是同一句错误的应用。
    原因是 `list_events(application_id)`——**事件不挂在应用上，挂在运行上**
    （循环里 `run_id = event.stream_id` 那句正说明作者知道）。
    查不到就返回空，于是"找不到"被当成了"没有"。
  · `/api/v1/runs/{id}/metrics` 对每一次运行都报 0 token、$0。
    这个不是本模块的错：真机上工作流运行**从来没记过用量**
    （node.completed 里 usage 是 {}）。但报 0 和报"没记过"是两回事，
    所以加了 usage_recorded 这一格。

第三件顺手的：node_breakdown 截到前 20 个而不说——
"给一页别当全部"在这个仓里已经中过四次了。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_platform.observability import RunAnalyzer
from agent_platform.storage import Storage
from agent_platform.workflow_storage import WorkflowStorage


@pytest.fixture
def storage(tmp_path: Path) -> Storage:
    """applications / workflow_runs 两张表归 WorkflowStorage 建，
    所以两个初始化都要跑——只跑 Storage 那个会得到 "no such table"。"""
    made = Storage(tmp_path / "d")
    store = WorkflowStorage(made)

    async def _init() -> None:
        await made.initialize()
        await store.initialize()

    asyncio.run(_init())
    return made


def _add_failed_run(storage: Storage, *, app_id: str, run_id: str, error: str) -> None:
    with storage._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO applications(id,name,description,requirement,mode,"
            "active_version,created_at,updated_at) "
            "VALUES(?,?,'','','workflow',1,datetime('now'),datetime('now'))",
            (app_id, "被测应用"))
        conn.execute(
            "INSERT INTO workflow_runs(id,application_id,version,draft_revision,status,"
            "state_json,outputs_json,error,created_at,updated_at) "
            "VALUES(?,?,1,NULL,'failed','{}','{}',?,datetime('now'),datetime('now'))",
            (run_id, app_id, error))


class TestFailurePatternsSeeRealFailures:
    def test_repeated_failures_cluster_into_one_pattern(self, storage):
        """真机形状：同一句错误反复出现，该聚成一类、计数对得上。"""
        for index in range(5):
            _add_failed_run(storage, app_id="app-1", run_id=f"r{index}",
                            error="node start failed: missing required input: text")
        patterns = asyncio.run(RunAnalyzer(storage).failure_patterns("app-1"))
        assert len(patterns) == 1, patterns
        assert patterns[0].count == 5
        assert patterns[0].example_run_ids, "至少要给一个例子，不然没法去查"

    def test_different_failures_stay_apart(self, storage):
        _add_failed_run(storage, app_id="app-1", run_id="a",
                        error="node start failed: missing required input: text")
        _add_failed_run(storage, app_id="app-1", run_id="b",
                        error="request timed out after 60s")
        names = {p.pattern_name for p in
                 asyncio.run(RunAnalyzer(storage).failure_patterns("app-1"))}
        assert len(names) == 2, names

    def test_the_biggest_cluster_comes_first(self, storage):
        for index in range(4):
            _add_failed_run(storage, app_id="app-1", run_id=f"m{index}",
                            error="request timed out")
        _add_failed_run(storage, app_id="app-1", run_id="one",
                        error="missing required input: text")
        patterns = asyncio.run(RunAnalyzer(storage).failure_patterns("app-1"))
        assert [p.count for p in patterns] == [4, 1], patterns

    def test_another_applications_failures_do_not_leak_in(self, storage):
        """按应用分开数——混在一起的话这个接口就是在骗人。"""
        _add_failed_run(storage, app_id="app-1", run_id="mine", error="timed out")
        _add_failed_run(storage, app_id="app-2", run_id="theirs", error="timed out")
        patterns = asyncio.run(RunAnalyzer(storage).failure_patterns("app-1"))
        assert sum(p.count for p in patterns) == 1, patterns

    def test_an_application_with_no_failures_gets_nothing(self, storage):
        """反向：真的没失败就该是空的。

        少了这一条，"永远返回空"（原来那个 bug）也能让上面全绿——
        不对，上面几条会红。但少了这条，"永远返回一类"也能过。
        """
        with storage._connect() as conn:
            conn.execute(
                "INSERT INTO applications(id,name,description,requirement,mode,"
                "active_version,created_at,updated_at) "
                "VALUES('quiet','安静的','','','workflow',1,"
                "datetime('now'),datetime('now'))")
        assert asyncio.run(RunAnalyzer(storage).failure_patterns("quiet")) == []

    def test_an_error_stored_only_in_the_state_blob_is_still_seen(self, storage):
        """有些运行的错误只写在 state_json 里，error 列是空的。

        面板那边早就 COALESCE 两处了；这里少一处就会把它们算成 unknown。
        """
        with storage._connect() as conn:
            conn.execute(
                "INSERT INTO applications(id,name,description,requirement,mode,"
                "active_version,created_at,updated_at) "
                "VALUES('app-3','三','','','workflow',1,"
                "datetime('now'),datetime('now'))")
            conn.execute(
                "INSERT INTO workflow_runs(id,application_id,version,draft_revision,"
                "status,state_json,outputs_json,error,created_at,updated_at) "
                "VALUES('r-blob','app-3',1,NULL,'failed',?,'{}',NULL,"
                "datetime('now'),datetime('now'))",
                (json.dumps({"error": "request timed out after 60s"}),))
        [pattern] = asyncio.run(RunAnalyzer(storage).failure_patterns("app-3"))
        assert pattern.pattern_name != "unknown", pattern


class TestZeroIsNotTheSameAsNotRecorded:
    def _run_with_events(self, storage: Storage, *, usage: dict | None) -> None:
        async def write() -> None:
            await storage.append_event("run-1", "workflow.started",
                                       {"application_id": "app-1"})
            await storage.append_event("run-1", "node.started",
                                       {"node_id": "n1", "type": "model_call"})
            await storage.append_event("run-1", "node.completed",
                                       {"node_id": "n1", "usage": usage or {}})
            await storage.append_event("run-1", "workflow.completed", {})
        asyncio.run(write())

    def test_a_run_without_usage_says_so(self, storage):
        """真机上每一次运行都是这一条：usage 是 {}，于是 token 全 0。"""
        self._run_with_events(storage, usage={})
        metrics = asyncio.run(RunAnalyzer(storage).analyze("run-1"))
        assert metrics is not None
        assert metrics.total_input_tokens == 0
        assert metrics.usage_recorded is False, "没记过用量，别装作算过了"

    def test_a_run_with_usage_is_marked_and_counted(self, storage):
        self._run_with_events(storage, usage={"input_tokens": 120,
                                              "output_tokens": 34,
                                              "cost_usd": 0.5})
        metrics = asyncio.run(RunAnalyzer(storage).analyze("run-1"))
        assert metrics.usage_recorded is True
        assert (metrics.total_input_tokens, metrics.total_output_tokens) == (120, 34)
        assert metrics.total_cost_usd == pytest.approx(0.5)

    def test_a_genuine_zero_still_counts_as_recorded(self, storage):
        """真的记了、数就是 0——这和"没记"要分开。"""
        self._run_with_events(storage, usage={"input_tokens": 0, "output_tokens": 0})
        metrics = asyncio.run(RunAnalyzer(storage).analyze("run-1"))
        assert metrics.usage_recorded is True
        assert metrics.total_input_tokens == 0


class TestTheClassifierKnowsThisPlatformsVocabulary:
    """查得到不等于查得对。

    上面那批修好之后，真机 227 次失败里 **208 次（91%）** 归成 unknown——
    一个把九成都答成"不知道"的聚类接口，等于没答。
    照真机话术补了五类之后 unknown 归零。

    这里用的都是**平台源码里写死的话术**（formula.py 的
    "不支持的函数 X（可用：…）"、"变量 X 未绑定"，
    引用解析器的 "workflow reference could not resolve node="），
    不是从这台机器的数据里凑出来的巧合形状。
    """

    @pytest.mark.parametrize("error, expected", [
        ("node aggregator failed: collection expression requires an array",
         "data_shape_mismatch"),
        ("node normalizer failed: record_collection_normalize value must resolve "
         "to an array or object", "data_shape_mismatch"),
        ('node sum_by_store failed: sum_by(记录数组, "分组字段", "数值字段") '
         "需要一个对象数组和两个字符串字段名", "data_shape_mismatch"),
        ("node aggregator failed: workflow reference could not resolve "
         "node='aggregator' path=['by_store']", "workflow_reference_unresolved"),
        ("node aggregator failed: 操作符 $formula 不能和其它键混在同一个对象里",
         "formula_or_expression_error"),
        ("node sum_by_store failed: 公式包含不支持的字符 '$'（位置 7）",
         "formula_or_expression_error"),
        ("node assigner failed: 不支持的函数 values（可用：abs、avg、ceil）",
         "formula_or_expression_error"),
        ("node assigner failed: 变量 amount 未绑定（vars 里没有它）",
         "formula_or_expression_error"),
        ("node render_report failed: 'g'", "template_variable_missing"),
        ("出错的节点是 'render_report'（配置在这个节点上）：'g'",
         "template_variable_missing"),
        ("database is locked", "platform_contention"),
    ])
    def test_a_real_platform_error_gets_a_real_name(self, error, expected):
        from agent_platform.observability import _classify_failure

        assert _classify_failure(error) == expected

    @pytest.mark.parametrize("error, expected", [
        ("node start failed: missing required input: text", "missing_resource"),
        ("request timed out after 60s", "api_timeout_or_rate_limit"),
        ("permission denied", "permission_error"),
    ])
    def test_the_generic_rules_still_work(self, error, expected):
        """平台自己的话术判在前面，别把原有那几类挤掉了。

        "missing required input" 里带 missing，靠的正是下半批的通用规则。
        """
        from agent_platform.observability import _classify_failure

        assert _classify_failure(error) == expected

    def test_an_empty_error_is_unknown(self):
        from agent_platform.observability import _classify_failure

        assert _classify_failure("") == "unknown"

    def test_it_does_not_label_everything(self):
        """反向：认不出来就说 unknown，别硬塞一类。

        少了这条，"一律返回 data_shape_mismatch" 能让上面十一条全绿。
        """
        from agent_platform.observability import _classify_failure

        assert _classify_failure("某种以前没见过的怪事") == "unknown"
