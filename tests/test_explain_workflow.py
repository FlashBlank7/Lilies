"""业主要能问「它是怎么做的」。

以前对话里能跑、能验、能修，唯独看不见结构——想看只能导出 JSON 自己读。
对一个「说人话管工作流」的产品，这是让业主只能盲信交付物。
"""
import unittest
from unittest.mock import AsyncMock, MagicMock

from agent_platform.assistant_agent import WorkflowConcierge


def _node(node_type, config=None):
    node = MagicMock()
    node.type = node_type
    node.config = config or {}
    return node


def _agent(nodes, *, published=True):
    snapshot = MagicMock()
    snapshot.workflow.nodes = nodes
    services = MagicMock()
    services.workflow_store.get_version = AsyncMock(return_value={"snapshot": snapshot})
    services.blocks.get = MagicMock(
        side_effect=lambda t: MagicMock(title={"variable_assigner": "变量赋值",
                                               "llm": "大模型"}.get(t, t)))
    agent = WorkflowConcierge(services, MagicMock())
    agent._resolve_app = AsyncMock(return_value={
        "id": "a1", "name": "统计", "active_version": 1 if published else None})
    return agent


class ExplainWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def test_describes_inputs_steps_and_outputs(self):
        agent = _agent([
            _node("start", {"inputs": [{"name": "text", "type": "string"}]}),
            _node("variable_assigner"),
            _node("end", {"outputs": {"line_count": "number"}}),
        ])
        result = await agent._exec("explain_workflow", {"name_or_id": "统计"}, {})
        self.assertIn("text（string）", result["要给它什么"])
        self.assertEqual(result["它做几步"], ["变量赋值"])
        self.assertEqual(result["会得到什么"], ["line_count"])
        self.assertIn("手动", result["定时"])

    async def test_schedule_is_reported_in_plain_words(self):
        agent = _agent([
            _node("schedule_trigger", {"hour": 8, "minute": 0,
                                       "timezone": "Asia/Shanghai"}),
            _node("llm"),
            _node("end", {"outputs": {"report": "string"}}),
        ])
        result = await agent._exec("explain_workflow", {"name_or_id": "统计"}, {})
        self.assertIn("08:00", result["定时"])
        self.assertIn("Asia/Shanghai", result["定时"])

    async def test_unpublished_workflow_says_so(self):
        agent = _agent([], published=False)
        result = await agent._exec("explain_workflow", {"name_or_id": "统计"}, {})
        self.assertIn("还没有发布版", result["error"])

    async def test_unknown_node_type_falls_back_to_its_own_name(self):
        # 目录里没有的节点类型不该让整件事失败
        agent = _agent([_node("start", {"inputs": []}), _node("某个新节点")])
        result = await agent._exec("explain_workflow", {"name_or_id": "统计"}, {})
        self.assertEqual(result["它做几步"], ["某个新节点"])

    async def test_empty_workflow_still_reads_as_a_sentence(self):
        agent = _agent([_node("start", {"inputs": []}), _node("end", {"outputs": {}})])
        result = await agent._exec("explain_workflow", {"name_or_id": "统计"}, {})
        for key in ("要给它什么", "它做几步", "会得到什么"):
            self.assertTrue(result[key], key)   # 不许出现空列表让模型自由发挥
