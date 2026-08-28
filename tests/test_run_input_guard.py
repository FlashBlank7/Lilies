"""别替业主制造注定失败的运行记录。

回归背景（2026-08-28 真机）：线上失败原因里「缺少必填输入」排第三
（text 11 次、sales 4 次）。管家不知道工作流要什么就直接跑：
list_workflows 的说明写着会给「输入声明」却从不返回，
run_workflow 也不做校验。失败记录永久留在历史里，
还会喂给体检和「近期失败」面板，看起来像工作流坏了。
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from agent_platform.assistant_agent import WorkflowConcierge


def _node(node_type, inputs=None):
    node = MagicMock()
    node.type = node_type
    node.config = {"inputs": inputs or []}
    return node


def _agent(inputs, *, app_published=True):
    snapshot = MagicMock()
    snapshot.workflow.nodes = [_node("start", inputs), _node("end")]
    services = MagicMock()
    services.workflow_store.get_version = AsyncMock(
        return_value={"snapshot": snapshot})
    services.workflow_runtime.create_run = AsyncMock(
        return_value={"run_id": "r1"})
    services.workflow_store.get_run = AsyncMock(
        return_value={"status": "succeeded", "outputs": {"n": 1}, "error": None})
    agent = WorkflowConcierge(services, MagicMock())
    agent._resolve_app = AsyncMock(return_value={
        "id": "a1", "name": "统计", "active_version": 1 if app_published else None})
    return agent, services


class RunInputGuardTest(unittest.IsolatedAsyncioTestCase):
    async def test_missing_required_input_does_not_create_a_run(self):
        agent, services = _agent([{"name": "text", "type": "string"}])
        result = await agent._exec("run_workflow", {"name_or_id": "统计"}, {})
        self.assertIn("error", result)
        self.assertIn("text", result["error"])
        services.workflow_runtime.create_run.assert_not_awaited()

    async def test_the_error_says_what_the_workflow_wants(self):
        agent, _ = _agent([{"name": "text", "type": "string"},
                           {"name": "lang", "type": "string", "required": False}])
        result = await agent._exec("run_workflow", {"name_or_id": "统计"}, {})
        names = [f["名字"] for f in result["这个工作流要什么"]]
        self.assertEqual(names, ["text", "lang"])
        self.assertIn("别自己编", result["接下来"])   # 别让它替业主编一个值

    async def test_optional_input_alone_does_not_block(self):
        agent, services = _agent([{"name": "lang", "type": "string",
                                   "required": False}])
        result = await agent._exec("run_workflow", {"name_or_id": "统计"}, {})
        self.assertEqual(result["status"], "succeeded")
        services.workflow_runtime.create_run.assert_awaited_once()

    async def test_blank_string_counts_as_missing(self):
        agent, services = _agent([{"name": "text", "type": "string"}])
        result = await agent._exec(
            "run_workflow", {"name_or_id": "统计", "inputs": {"text": "   "}}, {})
        self.assertIn("error", result)
        services.workflow_runtime.create_run.assert_not_awaited()

    async def test_all_inputs_given_runs_normally(self):
        agent, services = _agent([{"name": "text", "type": "string"}])
        result = await agent._exec(
            "run_workflow", {"name_or_id": "统计", "inputs": {"text": "hi"}}, {})
        self.assertEqual(result["status"], "succeeded")
        services.workflow_runtime.create_run.assert_awaited_once()

    async def test_undeclarable_workflow_is_not_blocked(self):
        """取不到声明时宁可不拦——误拦比漏拦更烦人。"""
        agent, services = _agent([])
        services.workflow_store.get_version = AsyncMock(side_effect=KeyError("no version"))
        result = await agent._exec("run_workflow", {"name_or_id": "统计"}, {})
        self.assertEqual(result["status"], "succeeded")
        services.workflow_runtime.create_run.assert_awaited_once()

    async def test_list_workflows_actually_returns_the_declaration(self):
        # 工具说明一直承诺「输入声明」，以前从不兑现
        agent, services = _agent([{"name": "text", "type": "string"}])
        services.workflow_store.list_applications = AsyncMock(
            return_value=[{"id": "a1", "name": "统计", "active_version": 1}])
        result = await agent._exec("list_workflows", {}, {})
        self.assertEqual(result["workflows"][0]["要给的输入"][0]["名字"], "text")
