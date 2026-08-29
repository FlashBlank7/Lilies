"""缺必填输入要在入口就挡下，不要建了运行、排上队、跑起来才在节点里炸。

回归背景（2026-08-29 真机测量）：发布版运行的 25 次失败里 18 次是输入问题
（15 次缺必填、3 次类型不对）。全都是这个流程：

    POST 建运行 → 202 已接受 → 排队 → 执行 → node start failed: missing required input: text

后果三重：调用方等了半天只等到一句英文节点报错；库里多一条失败记录，
污染面板、失败清单和体检；而这些本来在收到请求那一刻就能判出来。

只对发布版查。草稿自测是搭建方在故意试错误用例——那条路要照常跑完、
照常失败，它靠这个学。
"""
import unittest

from agent_platform.workflow_runtime import _missing_required_inputs

SNAPSHOT = {"name": "门店日报", "workflow": {"nodes": [
    {"id": "start", "type": "start", "config": {"inputs": [
        {"name": "sales", "type": "array", "required": True},
        {"name": "title", "type": "string", "required": False},
        {"name": "day", "type": "string", "required": True, "default": "今天"},
    ]}},
    {"id": "end", "type": "end", "config": {}},
]}}


class MissingRequiredInputsTest(unittest.TestCase):
    def test_a_missing_required_input_is_named(self):
        self.assertEqual(_missing_required_inputs(SNAPSHOT, {}), ["sales"])

    def test_a_supplied_input_is_not_reported(self):
        self.assertEqual(_missing_required_inputs(SNAPSHOT, {"sales": []}), [])

    def test_an_empty_list_counts_as_supplied(self):
        """空数组是合法的值，不是"没给"——判据是 None，不是真假。"""
        self.assertEqual(_missing_required_inputs(SNAPSHOT, {"sales": []}), [])

    def test_zero_and_empty_string_count_as_supplied(self):
        snapshot = {"workflow": {"nodes": [{"id": "s", "type": "start", "config": {
            "inputs": [{"name": "n", "type": "number", "required": True},
                       {"name": "t", "type": "string", "required": True}]}}]}}
        self.assertEqual(_missing_required_inputs(snapshot, {"n": 0, "t": ""}), [])

    def test_an_optional_input_is_never_reported(self):
        self.assertNotIn("title", _missing_required_inputs(SNAPSHOT, {}))

    def test_a_default_satisfies_a_required_input(self):
        """声明了默认值的必填项，不给也能跑——运行时就是这么取的。"""
        self.assertNotIn("day", _missing_required_inputs(SNAPSHOT, {}))

    def test_several_missing_inputs_are_all_named(self):
        snapshot = {"workflow": {"nodes": [{"id": "s", "type": "start", "config": {
            "inputs": [{"name": "a", "required": True},
                       {"name": "b", "required": True}]}}]}}
        self.assertEqual(_missing_required_inputs(snapshot, {}), ["a", "b"])

    def test_a_schedule_trigger_is_not_checked(self):
        """定时的输入由定时配置自带，不是调用方现填的。"""
        snapshot = {"workflow": {"nodes": [{"id": "s", "type": "schedule_trigger",
                                            "config": {"inputs": [{"name": "x",
                                                                   "required": True}]}}]}}
        self.assertEqual(_missing_required_inputs(snapshot, {}), [])

    def test_an_unreadable_snapshot_does_not_block_the_run(self):
        """解析不了就别挡：宁可让它跑起来再失败，也不能把所有运行关在门外。"""
        for bad in (None, "", 42, {"workflow": None}, {"workflow": {"nodes": None}}):
            self.assertEqual(_missing_required_inputs(bad, {}), [], repr(bad))


class ItIsWiredIntoCreateRunTest(unittest.IsolatedAsyncioTestCase):
    """函数写好了没接在 create_run 上，等于没写。"""

    async def test_a_published_run_is_refused_before_it_is_created(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from agent_platform.workflow_models import WorkflowRunRequest
        from agent_platform.workflow_runtime import WorkflowRuntime

        runtime = WorkflowRuntime.__new__(WorkflowRuntime)
        # create_run 现在还会查一次应用（拦已归档的），桩要跟上
        runtime.workflow_store = SimpleNamespace(
            get_version=AsyncMock(return_value={"snapshot": SNAPSHOT, "version": 1}),
            get_application=AsyncMock(return_value={"id": "a1", "name": "门店日报",
                                                    "archived_at": None}),
            get_draft=AsyncMock())
        with self.assertRaises(ValueError) as caught:
            await runtime.create_run("a1", WorkflowRunRequest(inputs={}))
        self.assertIn("sales", str(caught.exception))
        self.assertIn("必填", str(caught.exception))

    async def test_a_draft_self_test_is_not_refused(self):
        """搭建方靠试错误用例学习——草稿那条路不能挡。"""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from agent_platform.workflow_models import WorkflowRunRequest
        from agent_platform.workflow_runtime import WorkflowRuntime

        runtime = WorkflowRuntime.__new__(WorkflowRuntime)
        runtime.workflow_store = SimpleNamespace(
            get_draft=AsyncMock(return_value={"snapshot": SNAPSHOT, "revision": 1}),
            get_application=AsyncMock(return_value={"id": "a1", "archived_at": None}),
            get_version=AsyncMock())
        try:
            await runtime.create_run("a1", WorkflowRunRequest(inputs={}, use_draft=True))
        except ValueError as error:
            self.assertNotIn("必填", str(error), "草稿自测被必填预检挡住了")
        except Exception:
            pass      # 往下还会因为缺别的桩而报错，那不是这条测试关心的


if __name__ == "__main__":
    unittest.main()
